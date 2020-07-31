#!/usr/bin/env python
# coding=utf-8

import os
import sys
import stat
import signal
import time
import logging
import logging.handlers
from datetime import datetime
import re
import ConfigParser as Parser
import uuid
import zlib
from zipfile import ZipFile, ZipInfo, ZIP64_LIMIT, ZIP_DEFLATED
from pymongo import MongoClient, errors, ASCENDING
from pwd import getpwnam
from dcap import Dcap

running = True

def sigint_handler(signum, frame):
    global running
    logging.info("Caught signal %d." % signum)
    print("Caught signal %d." % signum)
    running = False

scriptId = 'pack'
archiveUser = 'root'
archiveMode = '0644'
mongo_uri = "mongodb://localhost/"
mongo_db  = "smallfiles"
mount_point = ""
data_root = ""
dcapUrl = ""

class StoreZipFile(ZipFile):

    def __init__(self, file):
        ZipFile.__init__(self, file, mode='w', allowZip64=True)

    def write(self, filename, arcname=None, compress_type=None):
        """Put the bytes from filename into the archive under the name
        arcname."""
        if not self.fp:
            raise RuntimeError(
                "Attempt to write to ZIP archive that was already closed")

        st = os.stat(filename)
        isdir = stat.S_ISDIR(st.st_mode)
        mtime = time.localtime(st.st_mtime)
        date_time = mtime[0:6]
        # Create ZipInfo instance to store file information
        if arcname is None:
            arcname = filename
        arcname = os.path.normpath(os.path.splitdrive(arcname)[1])
        while arcname[0] in (os.sep, os.altsep):
            arcname = arcname[1:]
        if isdir:
            arcname += '/'
        zinfo = ZipInfo(arcname, date_time)
        zinfo.external_attr = (st[0] & 0xFFFF) << 16L      # Unix attributes
        if compress_type is None:
            zinfo.compress_type = self.compression
        else:
            zinfo.compress_type = compress_type

        zinfo.file_size = st.st_size
        zinfo.flag_bits = 0x00
        zinfo.header_offset = self.fp.tell()    # Start of header bytes

        self._writecheck(zinfo)
        self._didModify = True

        if isdir:
            zinfo.file_size = 0
            zinfo.compress_size = 0
            zinfo.CRC = 0
            self.filelist.append(zinfo)
            self.NameToInfo[zinfo.filename] = zinfo
            self.fp.write(zinfo.FileHeader(False))
            return

        with open(filename, "rb") as fp:
            # Must overwrite CRC and sizes with correct data later
            zinfo.CRC = CRC = 0
            zinfo.compress_size = compress_size = 0
            # Compressed size can be larger than uncompressed size
            zip64 = self._allowZip64 and \
                    zinfo.file_size * 1.05 > ZIP64_LIMIT
            self.fp.write(zinfo.FileHeader(zip64))
            if zinfo.compress_type == ZIP_DEFLATED:
                cmpr = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION,
                                        zlib.DEFLATED, -15)
            else:
                cmpr = None
            file_size = 0
            while 1:
                buf = fp.read(1024 * 1024)
                if not buf:
                    break
                file_size = file_size + len(buf)
                CRC = zlib.crc32(buf, CRC) & 0xffffffff
                if cmpr:
                    buf = cmpr.compress(buf)
                    compress_size = compress_size + len(buf)
                self.fp.write(buf)
        if cmpr:
            buf = cmpr.flush()
            compress_size = compress_size + len(buf)
            self.fp.write(buf)
            zinfo.compress_size = compress_size
        else:
            zinfo.compress_size = file_size
        zinfo.CRC = CRC
        zinfo.file_size = file_size
        if not zip64 and self._allowZip64:
            if file_size > ZIP64_LIMIT:
                raise RuntimeError('File size has increased during compressing')
            if compress_size > ZIP64_LIMIT:
                raise RuntimeError('Compressed size larger than uncompressed size')
        # Seek backwards and write file header (which will now include
        # correct CRC and file sizes)
        position = self.fp.tell()       # Preserve current position in file
        self.fp.seek(zinfo.header_offset, 0)
        self.fp.write(zinfo.FileHeader(zip64))
        self.fp.seek(position, 0)
        self.filelist.append(zinfo)
        self.NameToInfo[zinfo.filename] = zinfo


class Container:

    def __init__(self, localtargetdir, dcap):
        self.filename = str(uuid.uuid1())
        self.localfilepath = os.path.join(localtargetdir, self.filename)
        pnfstargetdir = localtargetdir.replace(mount_point, data_root, 1)
        self.pnfsfilepath = os.path.join(pnfstargetdir, self.filename)

        self.logger = logging.getLogger(name="Container[%s]" % self.pnfsfilepath)
        self.logger.debug("Initializing")

        self.dcaparc = dcap.open_file(self.pnfsfilepath, 'w')
        self.arcfile = StoreZipFile(self.dcaparc)
        global archiveUser
        global archiveMode
        self.archiveUid = getpwnam(archiveUser).pw_uid
        self.archiveMod = int(archiveMode, 8)
        self.size = 0
        self.filecount = 0

    def close(self):
        self.logger.debug("Closing")
        self.arcfile.close()
        self.dcaparc.close()
        os.chown(self.localfilepath, self.archiveUid, os.getgid())
        os.chmod(self.localfilepath, self.archiveMod)

    def add(self, pnfsid, filepath, localpath, size):
        self.arcfile.write(localpath, arcname=pnfsid)
        self.size += size
        self.filecount += 1
        self.logger.debug("Added file %s with pnfsid %s" % (filepath, pnfsid))

    def getFilelist(self):
        return self.arcfile.filelist

    def verifyFilelist(self):
        return len(self.arcfile.filelist) == self.filecount

    def verifyChecksum(self, chksum):
        self.logger.warn("Checksum verification not implemented, yet")
        return True

class UserInterruptException(Exception):
    def __init__(self, arcfile):
        self.arcfile = arcfile

    def __str__(self):
        return repr(self.arcfile)

class GroupPackager:

    def __init__(self, path, filePattern, sGroup, storeName, archivePath, archiveSize, minAge, maxAge, verify):
        self.path = path
        self.pathPattern = re.compile(os.path.join(path, filePattern))
        self.sGroup = re.compile(sGroup)
        self.storeName = re.compile(storeName)
        self.archivePath = os.path.join(mount_point, archivePath)
        if not os.path.exists(self.archivePath):
            os.makedirs(self.archivePath, mode=0777)
            os.chmod(self.archivePath, 0777)
        self.archiveSize = int(archiveSize.replace('G', '000000000').replace('M', '000000').replace('K', '000'))
        self.minAge = int(minAge)
        self.maxAge = int(maxAge)
        self.verify = verify
        self.client = MongoClient(mongo_uri)
        self.db = self.client[mongo_db]
        self.logger = logging.getLogger(name="GroupPackager[%s]" % self.pathPattern.pattern)

    def __del__(self):
        pass

    def verifyContainer(self, container):
        verified = False
        if self.verify == 'filelist':
            verified = container.verifyFilelist()
        elif self.verify == 'chksum':
            verified = container.verifyChecksum(0)
        elif self.verify == 'off':
            verified = True
        else:
            self.logger.warn("Unknown verification method %s. Assuming failure!" % self.verify)
            verified = False

        return verified

    def createArchiveEntry(self, container):
        try:
            containerLocalPath = container.localfilepath
            containerChimeraPath = container.pnfsfilepath
            containerPnfsid = dotfile(containerLocalPath, 'id')

            self.db.archives.insert( { 'pnfsid': containerPnfsid, 'path': containerChimeraPath } )
        except IOError as e:
            self.logger.critical("Could not find archive file %s, referred to by file entries in database! This needs immediate attention or you will lose data!" % containerChimeraPath)

    def writeStatus(self, arcfile, currentSize, nextFile):
        global scriptId
        with open("/var/log/dcache/pack-files-%s.status" % scriptId, 'w') as statusFile:
            statusFile.write("Container: %s\n" % arcfile)
            statusFile.write("Size: %d/%d\n" % ( currentSize, self.archiveSize ))
            statusFile.write("Next: %s\n" % nextFile.encode('ascii', 'ignore'))

    def run(self):
        global scriptId
        global running
        global dcapUrl
        dcap = Dcap(dcapUrl)
        try:
            now = int(datetime.now().strftime("%s"))
            ctime_threshold = (now - self.minAge*60)
            self.logger.debug("Looking for files matching { path: %s, group: %s, store: %s, ctime: { $lt: %d } }" % (self.pathPattern.pattern, self.sGroup.pattern, self.storeName.pattern, ctime_threshold) )
            with self.db.files.find( { 'state': 'new', 'path': self.pathPattern, 'group': self.sGroup, 'store': self.storeName, 'ctime': { '$lt': ctime_threshold } }, timeout=False).batch_size(512) as cursor:
                cursor.sort('ctime', ASCENDING)
                sumsize = 0
                old_file_mode = False
                ctime_oldfile_threshold = (now - self.maxAge*60)
                for f in cursor:
                    if f['ctime'] < ctime_oldfile_threshold:
                        old_file_mode = True
                    sumsize += f['size']

                filecount = cursor.count()

                self.logger.info("found %d files with a combined size of %d bytes" % (filecount, sumsize))
                if old_file_mode:
                    self.logger.debug("containing old files: ctime < %d" % ctime_oldfile_threshold)
                else:
                    self.logger.debug("containing no old files: ctime < %d" % ctime_oldfile_threshold)

                if old_file_mode:
                    if sumsize < self.archiveSize:
                        self.logger.info("combined size of old files not big enough for a regular archive, packing in old-file-mode")

                    else:
                        old_file_mode = False
                        self.logger.info("combined size of old files big enough for regular archive, packing in normal mode")
                elif sumsize < self.archiveSize:
                    self.logger.info("no old files found and %d bytes missing to create regular archive of size %d, leaving packager" % (self.archiveSize-sumsize, self.archiveSize))
                    return

                cursor.rewind()
                container = None
                containerChimeraPath = None
                try:
                    for f in cursor:
                        if filecount <= 0 or sumsize <= 0:
                            self.logger.info("Actual number of files exceeds precalculated number, will collect new files in next run.")
                            break

                        self.logger.debug("Next file %s [%s], remaining %d [%d bytes]" % (f['path'], f['pnfsid'], filecount, sumsize) )
                        if not running:
                            if container:
                                raise UserInterruptException(container.localfilepath)
                            else:
                                raise UserInterruptException(None)

                        if container is None:
                            if sumsize >= self.archiveSize or old_file_mode:
                                container = Container(self.archivePath, dcap)
                                self.logger.info("Creating new container %s . %d files [%d bytes] remaining." % (container.pnfsfilepath, filecount, sumsize))
                            else:
                                self.logger.info("remaining combined size %d < %d, leaving packager" % (sumsize, self.archiveSize))
                                return

                        if old_file_mode:
                            self.logger.debug("%d bytes remaining for this archive" % sumsize)
                            self.writeStatus(container.pnfsfilepath, sumsize, "%s [%s]" % ( f['path'], f['pnfsid'] ))
                        else:
                            self.logger.debug("%d bytes remaining for this archive" % (self.archiveSize-container.size))
                            self.writeStatus(container.pnfsfilepath, self.archiveSize-container.size, "%s [%s]" % ( f['path'], f['pnfsid'] ))

                        try:
                            localfile = f['path'].replace(data_root, mount_point, 1)
                            self.logger.debug("before container.add(%s[%s], %s)" % (f['path'], f['pnfsid'], f['size']))
                            container.add(f['pnfsid'], f['path'], localfile, f['size'])
                            self.logger.debug("before collection.save")
                            f['state'] = "added: %s" % container.pnfsfilepath
                            f['lock'] = scriptId
                            cursor.collection.save(f)
                            self.logger.debug("Added file %s [%s]" % (f['path'], f['pnfsid']))
                        except IOError as e:
                            self.logger.exception("IOError while adding file %s to archive %s [%s], %s" % (f['path'], container.pnfsfilepath, f['pnfsid'], e.message))
                            self.logger.debug("Removing entry for file %s" % f['pnfsid'])
                            self.db.files.remove( { 'pnfsid': f['pnfsid'] } )
                        except OSError as e:
                            self.logger.exception("OSError while adding file %s to archive %s [%s], %s" % (f['path'], f['pnfsid'], container.pnfsfilepath, e.message))
                            self.logger.debug("Removing entry for file %s" % f['pnfsid'])
                            self.db.files.remove( { 'pnfsid': f['pnfsid'] } )
                        except errors.OperationFailure as e:
                            self.logger.error("Removing container %s due to OperationalFailure. See below for details." % container.localfilepath)
                            container.close()
                            os.remove(container.localfilepath)
                            raise e
                        except errors.ConnectionFailure as e:
                            self.logger.error("Removing container %s due to ConnectionFailure. See below for details." % container.localfilepath)
                            container.close()
                            os.remove(container.localfilepath)
                            raise e

                        sumsize -= f['size']
                        filecount -= 1

                        if container.size >= self.archiveSize:
                            self.logger.debug("Closing full container %s" % container.pnfsfilepath)
                            containerChimeraPath = container.pnfsfilepath
                            container.close()

                            if self.verifyContainer(container):
                                self.logger.info("Container %s successfully stored" % container.pnfsfilepath)
                                self.db.files.update( { 'state': 'added: %s' % containerChimeraPath }, { '$set': { 'state': 'archived: %s' % containerChimeraPath }, '$unset': { 'lock': "" } }, multi=True )
                                self.createArchiveEntry(container)
                            else:
                                self.logger.warn("Removing container %s due to verification error" % container.localfilepath)
                                self.db.files.update( { 'state': 'added: %s' % containerChimeraPath }, { '$set': { 'state': 'new' }, '$unset': { 'lock': "" } }, multi=True )
                                os.remove(container.localfilepath)

                            container = None

                    if container:
                        if not old_file_mode:
                            self.logger.warn("Removing unful container %s . Maybe a file was deleted during packaging." % container.localfilepath)
                            container.close()
                            os.remove(container.localfilepath)
                            return

                        self.logger.debug("Closing container %s containing remaining old files", container.pnfsfilepath)
                        containerChimeraPath = container.pnfsfilepath
                        container.close()

                        if self.verifyContainer(container):
                            self.logger.info("Container %s with old files successfully stored" % container.pnfsfilepath)
                            self.db.files.update( { 'state': 'added: %s' % containerChimeraPath }, { '$set': { 'state': 'archived: %s' % containerChimeraPath }, '$unset': { 'lock': "" } }, multi=True )
                            self.createArchiveEntry(container)
                        else:
                            self.logger.warn("Removing container %s with old files due to verification error" % container.localfilepath)
                            self.db.files.update( { 'state': 'added: %s' % containerChimeraPath }, { '$set': { 'state': 'new' }, '$unset': { 'lock': "" } }, multi=True )
                            os.remove(container.localfilepath)

                except IOError as e:
                    self.logger.error("%s closing file %s. Trying to clean up files in state: 'added'. This might need additional manual fixing!" % (e.strerror, containerChimeraPath))
                    self.db.files.update( { 'state': 'added: %s' % containerChimeraPath }, { '$set': { 'state': 'new' }, '$unset': { 'lock': "" } }, multi=True )
                except errors.OperationFailure as e:
                    self.logger.error("Operation Exception in database communication while creating container %s . Please check!" % containerChimeraPath )
                    self.logger.error('%s' % e.message)
                    os.remove(container.localfilepath)
                except errors.ConnectionFailure as e:
                    self.logger.error("Connection Exception in database communication. Removing incomplete container %s ." % containerChimeraPath)
                    self.logger.error('%s' % e.message)
                    os.remove(container.localfilepath)

        finally:
            dcap.close()

def dotfile(filepath, tag):
    with open(os.path.join(os.path.dirname(filepath), ".(%s)(%s)" % (tag, os.path.basename(filepath))), mode='r') as dotfile:
       result = dotfile.readline().strip()
    return result


def main(configfile = '/etc/dcache/container.conf'):
    global running

    # initialize logging
    logger = logging.getLogger()
    log_handler = None

    while running:
        try:
            configuration = Parser.RawConfigParser(defaults = { 'scriptId': 'pack', 'archiveUser': 'root', 'archiveMode': '0644', 'mongoUri': 'mongodb://localhost/', 'mongoDb': 'smallfiles', 'loopDelay': 5, 'logLevel': 'ERROR' })
            configuration.read(configfile)

            global scriptId
            global archiveUser
            global archiveMode
            global mount_point
            global data_root
            global mongo_uri
            global mongo_db
            global dcapUrl
            scriptId = configuration.get('DEFAULT', 'scriptId')
            
            logLevelStr = configuration.get('DEFAULT', 'logLevel')
            logLevel = getattr(logging, logLevelStr.upper(), None)
            logger.setLevel(logLevel)

            if log_handler is not None:
                log_handler.close()
                logger.removeHandler(log_handler)

            log_handler = logging.handlers.WatchedFileHandler('/var/log/dcache/pack-files-%s.log' % scriptId)
            formatter = logging.Formatter('%(asctime)s %(name)-80s %(levelname)-8s %(message)s')
            log_handler.setFormatter(formatter)
            logger.addHandler(log_handler)

            archiveUser = configuration.get('DEFAULT', 'archiveUser')
            archiveMode = configuration.get('DEFAULT', 'archiveMode')
            mountPoint = configuration.get('DEFAULT', 'mountPoint')
            dataRoot = configuration.get('DEFAULT', 'dataRoot')
            mongoUri = configuration.get('DEFAULT', 'mongoUri')
            mongoDb  = configuration.get('DEFAULT', 'mongodb')
            dcapUrl = configuration.get('DEFAULT', 'dcapUrl')

            loopDelay = configuration.getint('DEFAULT', 'loopDelay')


            logging.info('Successfully read configuration from file %s.' % configfile)
            logging.debug('scriptId = %s' % scriptId)
            logging.debug('archiveUser = %s' % archiveUser)
            logging.debug('archiveMode = %s' % archiveMode)
            logging.debug('mountPoint = %s' % mountPoint)
            logging.debug('dataRoot = %s' % dataRoot)
            logging.debug('mongoUri = %s' % mongoUri)
            logging.debug('mongoDb = %s' % mongoDb)
            logging.debug('dcapUrl = %s' % dcapUrl)
            logging.debug('logLevel = %s' % logLevel)
            logging.debug('loopDelay = %s' % loopDelay)

            try:
                client = MongoClient(mongoUri)
                db = client[mongoDb]
                logging.info("Established db connection")
                
                logging.info("Sanitizing database")
                db.files.update( { 'lock': scriptId }, { '$set': { 'state': 'new' }, '$unset': { 'lock': "" } }, multi=True )

                logging.info("Creating group packagers")
                groups = configuration.sections()
                groupPackagers = []
                for group in groups:
                    logging.debug("Group: %s" % group)
                    filePattern = configuration.get(group, 'fileExpression')
                    logging.debug("filePattern: %s" % filePattern)
                    sGroup = configuration.get(group, 'sGroup')
                    logging.debug("sGroup: %s" % sGroup)
                    storeName = configuration.get(group, 'storeName')
                    logging.debug("storeName: %s" % storeName)
                    archivePath = configuration.get(group, 'archivePath')
                    logging.debug("archivePath: %s" % archivePath)
                    archiveSize = configuration.get(group, 'archiveSize')
                    logging.debug("archiveSize: %s" % archiveSize)
                    minAge = configuration.get(group, 'minAge')
                    logging.debug("minAge: %s" % minAge)
                    maxAge = configuration.get(group, 'maxAge')
                    logging.debug("maxAge: %s" % maxAge)
                    verify = configuration.get(group, 'verify')
                    logging.debug("verify: %s" % verify)
                    pathre = re.compile(configuration.get(group, 'pathExpression'))
                    logging.debug("pathExpression: %s" % pathre.pattern)
                    paths = db.files.find( { 'parent': pathre } ).distinct('parent')
                    pathset = set()
                    for path in paths:
                        pathmatch = re.match("(?P<sfpath>%s)" % pathre.pattern, path).group('sfpath')
                        pathset.add(pathmatch)

                    logging.debug("Creating a packager for each path in: %s" % pathset)
                    for path in pathset:
                        packager = GroupPackager(
                            path,
                            filePattern,
                            sGroup,
                            storeName,
                            archivePath,
                            archiveSize,
                            minAge,
                            maxAge,
                            verify)
                        groupPackagers.append(packager)
                        logging.info("Added packager %s for paths matching %s" % (group, (packager.path)))

                logging.info("Running packagers")
                for packager in groupPackagers:
                    packager.run()

                client.close()

            except errors.ConnectionFailure as e:
                logging.error("Connection to DB failed: %s" % e.message)

            logging.info("Sleeping for %d seconds" % loopDelay)
            time.sleep(loopDelay)

        except UserInterruptException as e:
            if e.arcfile:
                logging.info("Cleaning up unfinished container %s." % e.arcfile)
                os.remove(e.arcfile)
                logging.info("Cleaning up modified file entries.")
                containerChimeraPath = e.arcfile.replace(mountPoint, dataRoot, 1)
                db.files.update( { 'state': 'added: %s' % containerChimeraPath }, { '$set': { 'state': 'new' } }, multi=True )

            logging.info("Finished cleaning up. Exiting.")
            sys.exit(1)
        except Parser.NoOptionError as e:
            print("Missing option: %s" % e.message)
            logging.error("Missing option: %s" % e.message)
        except Parser.Error as e:
            print("Error reading configfile %s: %s" % (configfile, e.message))
            logging.error("Error reading configfile %s: %s" % (configfile, e.message))
            sys.exit(2)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, sigint_handler)
    if not os.getuid() == 0:
        print("pack-files must run as root!")
        sys.exit(2)

    if len(sys.argv) == 1:
        main()
    elif len(sys.argv) == 2:
        main(sys.argv[1])
    else:
        print("Usage: pack-files.py <configfile>")
        sys.exit(1)

