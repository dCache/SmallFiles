#!/usr/bin/env python3
# coding=utf-8

import os
import sys
import time
import errno
import signal
from zipfile import ZipFile, BadZipfile
from pymongo import MongoClient, errors
import configparser as parser
import logging
import logging.handlers
import traceback

running = True

archiveUser = ""
archiveMode = ""
mountPoint = ""
dataRoot = ""
mongoUri = ""
mongoDb = ""


def sigint_handler(signum, frame):
    global running
    logging.info(f"Caught signal {signum}.")
    print(f"Caught signal {signum}.")
    running = False


def uncaught_handler(*exc_info):
    err_text = "".join(traceback.format_exception(*exc_info))
    logging.critical(err_text)
    sys.stderr.write(err_text)


def main(configfile='/etc/dcache/container.conf'):
    global running

    # initialize logging
    logger = logging.getLogger()
    log_handler = None

    try:
        while running:
            configuration = parser.RawConfigParser(defaults={'scriptId': 'pack', 'mongoUri': 'mongodb://localhost/',
                                                             'mongoDb': 'smallfiles', 'logLevel': 'ERROR'})
            configuration.read(configfile)

            global archiveUser
            global archiveMode
            global mountPoint
            global dataRoot
            global mongoUri
            global mongoDb

            script_id = configuration.get('DEFAULT', 'scriptId')

            log_level_str = configuration.get('DEFAULT', 'logLevel')
            log_level = getattr(logging, log_level_str.upper(), None)
            logger.setLevel(log_level)

            if log_handler is not None:
                log_handler.close()
                logger.removeHandler(log_handler)

            log_handler = logging.handlers.WatchedFileHandler(f'/var/log/dcache/writebfids-{script_id}.log')
            formatter = logging.Formatter('%(asctime)s %(name)-10s %(levelname)-8s %(message)s')
            log_handler.setFormatter(formatter)
            logger.addHandler(log_handler)

            archiveUser = configuration.get('DEFAULT', 'archiveUser')
            archiveMode = configuration.get('DEFAULT', 'archiveMode')
            mountPoint = configuration.get('DEFAULT', 'mountPoint')
            dataRoot = configuration.get('DEFAULT', 'dataRoot')
            mongoUri = configuration.get('DEFAULT', 'mongoUri')
            mongoDb = configuration.get('DEFAULT', 'mongodb')

            logging.info(f'Successfully read configuration from file {configfile}.')

            try:
                client = MongoClient(mongoUri)
                db = client[mongoDb]
                logging.info("Established db connection")

                with db.archives.find() as archives:
                    logger.info(f"found {archives.count()} archive entries")
                    for archive in archives:
                        if not running:
                            logging.info("Exiting.")
                            sys.exit(1)
                        try:
                            localpath = archive['path'].replace(dataRoot, mountPoint, 1)
                            archive_pnfsid = archive['pnfsid']
                            zf = ZipFile(localpath, mode='r', allowZip64=True)
                            for f in zf.filelist:
                                logging.debug(f"Entering bfid into record for file {f.filename}")
                                filerecord = db.files.find_one(
                                    {'pnfsid': f.filename, 'state': f"archived: {archive['path']}"}, await_data=True)
                                if filerecord:
                                    url = f"dcache://dcache/?store={filerecord['store']}&group={filerecord['group']}" \
                                          f"&bfid={f.filename}:{archive_pnfsid}"
                                    filerecord['archiveUrl'] = url
                                    filerecord['state'] = f"verified: {archive['path']}"
                                    db.files.save(filerecord)
                                    logging.debug(f"Updated record with URL {url} in archive {archive['path']}")
                                else:
                                    logging.warning(
                                        f"File {f.filename} in archive {archive['path']} has no entry in DB. "
                                        f"This could be caused by a previous forced interrupt. Creating failure entry.")
                                    db.failures.insert({'archiveId': archive_pnfsid, 'pnfsid': f.filename})

                            logging.debug(f"stat({localpath}): {os.stat(localpath)}")
                            zf.close()

                            db.archives.remove({'pnfsid': archive['pnfsid']})
                            logging.debug(f"Removed entry for archive {archive['path']}[{archive['pnfsid']}]")

                            pnfsname = os.path.join(os.path.dirname(localpath), archive['pnfsid'])
                            logging.debug(f"Renaming archive {localpath} to {pnfsname}")
                            os.rename(localpath, pnfsname)

                        except BadZipfile:
                            logging.warning(f"Archive {localpath} is not yet ready. Will try later.")
                            zf.close()

                        except IOError as e:
                            if e.errno != errno.EINTR:
                                logging.error(f"IOError: {e.strerror}")
                            else:
                                logging.info("User interrupt.")
                            zf.close()

                client.close()

            except errors.ConnectionFailure as e:
                logging.warning(f"Connection to DB failed: {e}")
            except errors.OperationFailure as e:
                logging.warning(f"Could not create cursor: {e}")
            except Exception as e:
                logging.error(f"Unexpected error: {e}")

            logging.info("Processed all archive entries. Sleeping 60 seconds.")
            time.sleep(60)

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
        print("writebfsids.py must run as root!")
        sys.exit(2)

    if len(sys.argv) == 1:
        main()
    elif len(sys.argv) == 2:
        main(sys.argv[1])
    else:
        print("Usage: writebfids.py <configfile>")
        sys.exit(2)
