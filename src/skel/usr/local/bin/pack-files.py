#!/usr/bin/env python3
# coding=utf-8

import os
import sys
import signal
import time
import logging
import logging.handlers
from datetime import datetime
import re
import configparser as parser
import uuid
import traceback
from zipfile import ZipFile
from pymongo import MongoClient, errors, ASCENDING
from pwd import getpwnam
from dcap import Dcap

running = True

scriptId = 'pack'
archiveUser = 'root'
archiveMode = '0644'
mongoUri = "mongodb://localhost/"
mongoDb = "smallfiles"
mountPoint = ""
dataRoot = ""
dcapUrl = ""


def sigint_handler(signum, frame):
    global running
    logging.info(f"Caught signal {signum}.")
    print(f"Caught signal {signum}.")
    running = False


def uncaught_handler(*exc_info):
    err_text = "".join(traceback.format_exception(*exc_info))
    logging.critical(err_text)
    sys.stderr.write(err_text)


class Container:

    def __init__(self, localtargetdir, dcap):
        self.filename = str(uuid.uuid1())
        self.localfilepath = os.path.join(localtargetdir, self.filename)
        pnfstargetdir = localtargetdir.replace(mountPoint, dataRoot, 1)
        self.pnfsfilepath = os.path.join(pnfstargetdir, self.filename)

        self.logger = logging.getLogger(name=f"Container[{self.pnfsfilepath}]")
        self.logger.debug("Initializing")

        self.arcfile = None
        self.dcaparc = None

        # workaround for IOError
        try:
            self.dcaparc = dcap.open_file(self.pnfsfilepath, 'w')
            self.arcfile = ZipFile(self.dcaparc, 'w')
        except IOError as ioe:
            self.logger.error(f"Caught IOError while opening Container: {ioe}")
            self.logger.info("Trying to clean up")
            if self.arcfile:
                self.arcfile.close()
            if self.dcaparc:
                self.dcaparc.close()
            if os.path.isfile(self.pnfsfilepath) and os.path.getsize(self.pnfsfilepath) == 0:
                self.logger.info(f"Removing {self.pnfsfilepath}")
                os.remove(self.pnfsfilepath)
            logging.critical(f"Restarting script due to IOError")
            os.execv(sys.executable, ['python'] + sys.argv)
            # raise ioe
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
        self.logger.debug(f"Added file {filepath} with pnfsid {pnfsid}")

    def get_filelist(self):
        return self.arcfile.filelist

    def verify_filelist(self):
        return len(self.arcfile.filelist) == self.filecount

    def verify_checksum(self, chksum):
        self.logger.warning("Checksum verification not implemented, yet")
        return True


class UserInterruptException(Exception):
    def __init__(self, arcfile):
        self.arcfile = arcfile

    def __str__(self):
        return repr(self.arcfile)


class GroupPackager:

    def __init__(self, path, file_pattern, s_group, store_name, archive_path, archive_size, min_age, max_age, verify):
        self.path = path
        self.pathPattern = re.compile(os.path.join(path, file_pattern))
        self.sGroup = re.compile(s_group)
        self.storeName = re.compile(store_name)
        self.archivePath = os.path.join(mountPoint, archive_path)
        if not os.path.exists(self.archivePath):
            os.makedirs(self.archivePath, mode=0o777)
            os.chmod(self.archivePath, 0o777)
        self.archiveSize = int(archive_size.replace('G', '000000000').replace('M', '000000').replace('K', '000'))
        self.minAge = int(min_age)
        self.maxAge = int(max_age)
        self.verify = verify
        self.client = MongoClient(mongoUri)
        self.db = self.client[mongoDb]
        self.logger = logging.getLogger(name=f"GroupPackager[{self.pathPattern.pattern}]")

    def __del__(self):
        pass

    def verify_container(self, container):
        if self.verify == 'filelist':
            verified = container.verify_filelist()
        elif self.verify == 'chksum':
            verified = container.verify_checksum(0)
        elif self.verify == 'off':
            verified = True
        else:
            self.logger.warning(f"Unknown verification method {self.verify}. Assuming failure!")
            verified = False

        return verified

    def create_archive_entry(self, container):
        container_local_path = container.localfilepath
        container_chimera_path = container.pnfsfilepath
        try:
            container_pnfsid = read_dotfile(container_local_path, 'id')

            self.db.archives.insert_one({'pnfsid': container_pnfsid, 'path': container_chimera_path})
        except IOError:
            self.logger.critical(
                f"Could not find archive file {container_chimera_path}, referred to by file entries in database! "
                f"This needs immediate attention or you will lose data!")

    def write_status(self, arcfile, current_size, next_file):
        global scriptId
        with open(f"/var/log/dcache/pack-files-{scriptId}.status", 'w') as statusFile:
            statusFile.write(f"Container: {arcfile}\n")
            statusFile.write(f"Size: {current_size}/{self.archiveSize}\n")
            statusFile.write(f"Next: {next_file.encode('ascii', 'ignore')}\n")

    def run(self):
        global scriptId
        global running
        global dcapUrl
        dcap = Dcap(dcapUrl)
        try:
            now = int(datetime.now().strftime("%s"))
            ctime_threshold = (now - self.minAge * 60)
            self.logger.debug(f"Looking for files matching {{ "
                              f"path: {self.pathPattern.pattern}, "
                              f"group: {self.sGroup.pattern}, "
                              f"store: {self.storeName.pattern}, "
                              f"ctime: {{ $lt: {ctime_threshold} }} }}")
            with self.db.files.find(
                    {'state': 'new', 'path': self.pathPattern, 'group': self.sGroup, 'store': self.storeName,
                     'ctime': {'$lt': ctime_threshold}}, no_cursor_timeout=False).batch_size(512) as cursor:
                cursor.sort('ctime', ASCENDING)
                sumsize = 0
                old_file_mode = False
                ctime_oldfile_threshold = (now - self.maxAge * 60)
                for f in cursor:
                    if f['ctime'] < ctime_oldfile_threshold:
                        old_file_mode = True
                    sumsize += f['size']

                filecount = self.db.files.count_documents({'state': 'new', 'path': self.pathPattern,
                                                           'group': self.sGroup, 'store': self.storeName,
                                                           'ctime': {'$lt': ctime_threshold}})

                self.logger.info(f"found {filecount} files with a combined size of {sumsize} bytes")
                if old_file_mode:
                    self.logger.debug(f"containing old files: ctime < {ctime_oldfile_threshold}")
                else:
                    self.logger.debug(f"containing no old files: ctime < {ctime_oldfile_threshold}")

                if old_file_mode:
                    if sumsize < self.archiveSize:
                        self.logger.info(
                            "combined size of old files not big enough for a regular archive, packing in old-file-mode")

                    else:
                        old_file_mode = False
                        self.logger.info(
                            "combined size of old files big enough for regular archive, packing in normal mode")
                elif sumsize < self.archiveSize:
                    self.logger.info(
                        f"no old files found and {self.archiveSize - sumsize} bytes missing to create regular archive "
                        f"of size {self.archiveSize}, leaving packager")
                    return

                cursor.rewind()
                container = None
                container_chimera_path = None
                try:
                    for f in cursor:
                        if filecount <= 0 or sumsize <= 0:
                            self.logger.info("Actual number of files exceeds precalculated number, will collect "
                                             "new files in next run.")
                            break

                        self.logger.debug(
                            f"Next file {f['path']} [{f['pnfsid']}], remaining {filecount} [{sumsize} bytes]")
                        if not running:
                            if container:
                                raise UserInterruptException(container.localfilepath)
                            else:
                                raise UserInterruptException(None)

                        if container is None:
                            if sumsize >= self.archiveSize or old_file_mode:
                                container = Container(self.archivePath, dcap)
                                self.logger.info(f"Creating new container {container.pnfsfilepath} . {filecount} "
                                                 f"files [{sumsize} bytes] remaining.")
                            else:
                                self.logger.info(
                                    f"remaining combined size {sumsize} < {self.archiveSize}, leaving packager")
                                return

                        if old_file_mode:
                            self.logger.debug(f"{sumsize} bytes remaining for this archive")
                            self.write_status(container.pnfsfilepath, sumsize, f"{f['path']} [{f['pnfsid']}]")
                        else:
                            self.logger.debug(
                                f"{self.archiveSize - container.size} bytes remaining for this archive")
                            self.write_status(container.pnfsfilepath, self.archiveSize - container.size,
                                              f"{f['path']} [{f['pnfsid']}]")

                        try:
                            localfile = f['path'].replace(dataRoot, mountPoint, 1)
                            self.logger.debug(f"before container.add({f['path']}[{f['pnfsid']}], {f['size']})")
                            container.add(f['pnfsid'], f['path'], localfile, f['size'])
                            self.logger.debug("before collection.save")
                            f['state'] = f"added: {container.pnfsfilepath}"
                            f['lock'] = scriptId
                            cursor.collection.replace_one({'state': 'new', 'path': self.pathPattern,
                                                           'group': self.sGroup, 'store': self.storeName,
                                                           'ctime': {'$lt': ctime_threshold}}, f)
                            self.logger.debug(f"Added file {f['path']} [{f['pnfsid']}]")
                        except (IOError, OSError) as e:
                            if type(e) == type(IOError):
                                self.logger.exception(f"IOError while adding file {f['path']} to archive "
                                                      f"{container.pnfsfilepath} [{f['pnfsid']}], {e}")
                            else:
                                self.logger.exception(f"OSError while adding file {f['path']} to archive "
                                                      f"{f['pnfsid']} [{container.pnfsfilepath}], {e}")
                            self.logger.debug(f"Removing entry for file {f['pnfsid']}")
                            self.db.files.delete_one({'pnfsid': f['pnfsid']})
                        except errors.OperationFailure as e:
                            self.logger.error(
                                f"Removing container {container.localfilepath} due to OperationalFailure. "
                                f"See below for details.")
                            container.close()
                            os.remove(container.localfilepath)
                            raise e
                        except errors.ConnectionFailure as e:
                            self.logger.error(
                                f"Removing container {container.localfilepath} due to ConnectionFailure. "
                                f"See below for details.")
                            container.close()
                            os.remove(container.localfilepath)
                            raise e

                        sumsize -= f['size']
                        filecount -= 1

                        if container.size >= self.archiveSize:
                            self.logger.debug(f"Closing full container {container.pnfsfilepath}")
                            container_chimera_path = container.pnfsfilepath
                            container.close()

                            if self.verify_container(container):
                                self.logger.info(f"Container {container.pnfsfilepath} successfully stored")
                                self.db.files.update_many({'state': f'added: {container_chimera_path}'},
                                                     {'$set': {'state': f'archived: {container_chimera_path}'},
                                                      '$unset': {'lock': ""}})
                                self.create_archive_entry(container)
                            else:
                                self.logger.warning(
                                    f"Removing container {container.localfilepath} due to verification error")
                                self.db.files.update_many({'state': f'added: {container_chimera_path}'},
                                                     {'$set': {'state': 'new'}, '$unset': {'lock': ""}})
                                os.remove(container.localfilepath)

                            container = None

                    if container:
                        if not old_file_mode:
                            self.logger.warning(
                                f"Removing unful container {container.localfilepath} . Maybe a file was deleted "
                                f"during packaging.")
                            container.close()
                            os.remove(container.localfilepath)
                            return

                        self.logger.debug(f"Closing container {container.pnfsfilepath} containing remaining old files")
                        container_chimera_path = container.pnfsfilepath
                        container.close()

                        if self.verify_container(container):
                            self.logger.info(f"Container {container.pnfsfilepath} with old files successfully stored")
                            self.db.files.update_many({'state': f'added: {container_chimera_path}'},
                                                 {'$set': {'state': f'archived: {container_chimera_path}'},
                                                  '$unset': {'lock': ""}})
                            self.create_archive_entry(container)
                        else:
                            self.logger.warning(
                                f"Removing container {container.localfilepath} with old files due to "
                                f"verification error")
                            self.db.files.update_many({'state': f'added: {container_chimera_path}'},
                                                 {'$set': {'state': 'new'}, '$unset': {'lock': ""}})
                            os.remove(container.localfilepath)

                except IOError as e:
                    self.logger.error(
                        f"{e.strerror} closing file {container_chimera_path}. Trying to clean up files in state: "
                        f"'added'. This might need additional manual fixing!")
                    self.db.files.update_many({'state': f'added: {container_chimera_path}'},
                                         {'$set': {'state': 'new'}, '$unset': {'lock': ""}})
                except errors.OperationFailure as e:
                    self.logger.error(
                        f"Operation Exception in database communication while creating container "
                        f"{container_chimera_path} . Please check!")
                    self.logger.error(f'{e}')
                    os.remove(container.localfilepath)
                except errors.ConnectionFailure as e:
                    self.logger.error(
                        f"Connection Exception in database communication. Removing incomplete "
                        f"container {container_chimera_path}.")
                    self.logger.error(f'{e}')
                    os.remove(container.localfilepath)

        finally:
            dcap.close()


def read_dotfile(filepath, tag):
    with open(os.path.join(os.path.dirname(filepath), f".({tag})({os.path.basename(filepath)})"),
              mode='r') as dotfile:
        result = dotfile.readline().strip()
    return result


def main(configfile='/etc/dcache/container.conf'):
    global running

    # initialize logging
    logger = logging.getLogger()
    log_handler = None

    while running:
        global scriptId
        global archiveUser
        global archiveMode
        global mountPoint
        global dataRoot
        global mongoUri
        global mongoDb
        global dcapUrl

        try:
            configuration = parser.RawConfigParser(
                defaults={'scriptId': 'pack', 'archiveUser': 'root', 'archiveMode': '0644',
                          'mongoUri': 'mongodb://localhost/', 'mongoDb': 'smallfiles', 'loopDelay': 5,
                          'logLevel': 'ERROR'})
            configuration.read(configfile)

            scriptId = configuration.get('DEFAULT', 'scriptId')

            log_level_str = configuration.get('DEFAULT', 'logLevel')
            log_level = getattr(logging, log_level_str.upper(), None)
            logger.setLevel(log_level)

            if log_handler is not None:
                log_handler.close()
                logger.removeHandler(log_handler)

            log_handler = logging.handlers.WatchedFileHandler(f'/var/log/dcache/pack-files-{scriptId}.log')
            formatter = logging.Formatter('%(asctime)s %(name)-10s %(levelname)-8s %(message)s')
            log_handler.setFormatter(formatter)
            logger.addHandler(log_handler)

            archiveUser = configuration.get('DEFAULT', 'archiveUser')
            archiveMode = configuration.get('DEFAULT', 'archiveMode')
            mountPoint = configuration.get('DEFAULT', 'mountPoint')
            dataRoot = configuration.get('DEFAULT', 'dataRoot')
            mongoUri = configuration.get('DEFAULT', 'mongoUri')
            mongoDb = configuration.get('DEFAULT', 'mongodb')
            dcapUrl = configuration.get('DEFAULT', 'dcapUrl')

            loop_delay = configuration.getint('DEFAULT', 'loopDelay')

            logging.info(f'Successfully read configuration from file {configfile}.')
            logging.debug(f'scriptId = {scriptId}')
            logging.debug(f'archiveUser = {archiveUser}')
            logging.debug(f'archiveMode = {archiveMode}')
            logging.debug(f'mountPoint = {mountPoint}')
            logging.debug(f'dataRoot = {dataRoot}')
            logging.debug(f'mongoUri = {mongoUri}')
            logging.debug(f'mongoDb = {mongoDb}')
            logging.debug(f'dcapUrl = {dcapUrl}')
            logging.debug(f'logLevel = {log_level}')
            logging.debug(f'loopDelay = {loop_delay}')

            try:
                client = MongoClient(mongoUri)
                db = client[mongoDb]
                logging.info("Established db connection")

                logging.info("Sanitizing database")
                db.files.update_many({'lock': scriptId}, {'$set': {'state': 'new'}, '$unset': {'lock': ""}})

                logging.info("Creating group packagers")
                groups = configuration.sections()
                group_packagers = []
                for group in groups:
                    logging.debug(f"Group: {group}")
                    file_pattern = configuration.get(group, 'fileExpression')
                    logging.debug(f"filePattern: {file_pattern}")
                    s_group = configuration.get(group, 'sGroup')
                    logging.debug(f"sGroup: {s_group}")
                    store_name = configuration.get(group, 'storeName')
                    logging.debug(f"storeName: {store_name}")
                    archive_path = configuration.get(group, 'archivePath')
                    logging.debug(f"archivePath: {archive_path}")
                    archive_size = configuration.get(group, 'archiveSize')
                    logging.debug(f"archiveSize: {archive_size}")
                    min_age = configuration.get(group, 'minAge')
                    logging.debug(f"minAge: {min_age}")
                    max_age = configuration.get(group, 'maxAge')
                    logging.debug(f"maxAge: {max_age}")
                    verify = configuration.get(group, 'verify')
                    logging.debug(f"verify: {verify}")
                    pathre = re.compile(configuration.get(group, 'pathExpression'))
                    logging.debug(f"pathExpression: {pathre.pattern}")
                    paths = db.files.find({'parent': pathre}).distinct('parent')
                    pathset = set()
                    for path in paths:
                        pathmatch = re.match(f"(?P<sfpath>{pathre.pattern})", path).group('sfpath')
                        pathset.add(pathmatch)

                    logging.debug(f"Creating a packager for each path in: {pathset}")
                    for path in pathset:
                        packager = GroupPackager(
                            path,
                            file_pattern,
                            s_group,
                            store_name,
                            archive_path,
                            archive_size,
                            min_age,
                            max_age,
                            verify)
                        group_packagers.append(packager)
                        logging.info(f"Added packager {group} for paths matching {packager.path}")

                logging.info("Running packagers")
                for packager in group_packagers:
                    packager.run()

                client.close()

            except errors.ConnectionFailure as e:
                logging.error(f"Connection to DB failed:{e}")

            logging.info(f"Sleeping for {loop_delay} seconds")
            time.sleep(loop_delay)

        except UserInterruptException as e:
            if e.arcfile:
                logging.info(f"Cleaning up unfinished container {e.arcfile}.")
                os.remove(e.arcfile)
                logging.info("Cleaning up modified file entries.")
                container_chimera_path = e.arcfile.replace(mountPoint, dataRoot, 1)
                db.files.update_many({'state': f'added: {container_chimera_path}'}, {'$set': {'state': 'new'}})

            logging.info("Finished cleaning up. Exiting.")
            sys.exit(1)
        except parser.NoOptionError as e:
            print(f"Missing option: {e}")
            logging.error(f"Missing option: {e}")
        except parser.Error as e:
            print(f"Error reading configfile {configfile}: {e}")
            logging.error(f"Error reading configfile {configfile}: {e}")
            sys.exit(2)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, sigint_handler)
    sys.excepthook = uncaught_handler
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
