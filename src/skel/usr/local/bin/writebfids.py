#!/usr/bin/env python
# coding=utf-8

import os
import sys
import time
import errno
import signal
from zipfile import ZipFile, BadZipfile
from pymongo import MongoClient, errors
import ConfigParser as parser
import logging
import logging.handlers

running = True

def sigint_handler(signum, frame):
    global running
    print("Caught signal %d." % signum)
    running = False

def main(configfile = '/etc/dcache/container.conf'):
    global running

    # initialize logging
    logger = logging.getLogger()
    log_handler = None

    try:
        while running:
            configuration = parser.RawConfigParser(defaults = { 'scriptId': 'pack',  'mongoUri': 'mongodb://localhost/', 'mongoDb': 'smallfiles', 'loopDelay': 5, 'logLevel': 'ERROR' })
            configuration.read(configfile)

            global archiveUser
            global archiveMode
            global mountPoint
            global dataRoot
            global mongoUri
            global mongoDb

            scriptId = configuration.get('DEFAULT', 'scriptId')

            logLevelStr = configuration.get('DEFAULT', 'logLevel')
            logLevel = getattr(logging, logLevelStr.upper(), None)
            logger.setLevel(logLevel)

            if log_handler is not None:
                log_handler.close()
                logger.removeHandler(log_handler)

            log_handler = logging.handlers.WatchedFileHandler('/var/log/dcache/writebfids-%s.log' % scriptId)
            formatter = logging.Formatter('%(asctime)s %(name)-80s %(levelname)-8s %(message)s')
            log_handler.setFormatter(formatter)
            logger.addHandler(log_handler)

            archiveUser = configuration.get('DEFAULT', 'archiveUser')
            archiveMode = configuration.get('DEFAULT', 'archiveMode')
            mountPoint = configuration.get('DEFAULT', 'mountPoint')
            dataRoot = configuration.get('DEFAULT', 'dataRoot')
            mongoUri = configuration.get('DEFAULT', 'mongoUri')
            mongoDb  = configuration.get('DEFAULT', 'mongodb')

            loopDelay = configuration.getint('DEFAULT', 'loopDelay')

            logging.info('Successfully read configuration from file %s.' % configfile)

            try:
                client = MongoClient(mongoUri)
                db = client[mongoDb]
                logging.info("Established db connection")

                with db.archives.find() as archives:
                    for archive in archives:
                        if not running:
                            logging.info("Exiting.")
                            sys.exit(1)
                        try:
                            localpath = archive['path'].replace(dataRoot, mountPoint, 1)
                            archivePnfsid = archive['pnfsid']
                            zf = ZipFile(localpath, mode='r', allowZip64 = True)
                            for f in zf.filelist:
                                logging.debug("Entering bfid into record for file %s" % f.filename)
                                filerecord = db.files.find_one( { 'pnfsid': f.filename, 'state': 'archived: %s' % archive['path'] }, await_data=True )
                                if filerecord:
                                    url = "dcache://dcache/?store=%s&group=%s&bfid=%s:%s" % (filerecord['store'], filerecord['group'], f.filename, archivePnfsid)
                                    filerecord['archiveUrl'] = url
                                    filerecord['state'] = 'verified: %s' % archive['path']
                                    db.files.save(filerecord)
                                    logging.debug("Updated record with URL %s in archive %s" % (url,archive['path']))
                                else:
                                    logging.warn("File %s in archive %s has no entry in DB. This could be caused by a previous forced interrupt. Creating failure entry." % (f.filename, archive['path']) )
                                    db.failures.insert( { 'archiveId': archivePnfsid, 'pnfsid': f.filename } )

                            logging.debug("stat(%s): %s" % (localpath, os.stat(localpath)))

                            db.archives.remove( { 'pnfsid': archive['pnfsid'] } )
                            logging.debug("Removed entry for archive %s[%s]" % ( archive['path'], archive['pnfsid'] ) )

                            pnfsname = os.path.join(os.path.dirname(localpath), archive['pnfsid'])
                            logging.debug("Renaming archive %s to %s" % (localpath, pnfsname) )
                            os.rename(localpath, pnfsname)

                        except BadZipfile as e:
                            logging.warn("Archive %s is not yet ready. Will try later." % localpath)

                        except IOError as e:
                            if e.errno != errno.EINTR:
                                logging.error("IOError: %s" % e.strerror)
                            else:
                                logging.info("User interrupt.")

                client.close()

            except errors.ConnectionFailure as e:
                logging.warn("Connection to DB failed: %s" % e.message)
            except errors.OperationFailure as e:
                logging.warn("Could not create cursor: %s" % e.message)
            except Exception as e:
                logging.error("Unexpected error: %s" % e.message)

            logging.info("Processed all archive entries. Sleeping 60 seconds.")
            time.sleep(60)

    except parser.NoOptionError as e:
        print("Missing option: %s" % e.message)
        logging.error("Missing option: %s" % e.message)
    except parser.Error as e:
        print("Error reading configfile %s: %s" % (configfile, e.message))
        logging.error("Error reading configfile %s: %s" % (configfile, e.message))
        sys.exit(2)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, sigint_handler)
    if not os.getuid() == 0:
        print("writebfsids.py must run as root!")
        sys.exit(2)

    if len(sys.argv) == 1:
        main()
    elif len(sys.argv) == 2:
        main(sys.argv[1])
    else:
        print("Usage: writebfids.py <configfile>")
        sys.exit(2)

