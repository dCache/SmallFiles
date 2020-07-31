#!/usr/bin/env python3
# coding=utf-8

import os
import sys
import time
import signal
import configparser as parser
from pymongo import MongoClient, errors
import logging
import logging.handlers

running = True

mongoUri = ""
mongoDb = ""
mountPoint = ""
dataRoot = ""


def sigint_handler(signum, frame):
    global running
    logging.info(f"Caught signal {signum}.")
    print(f"Caught signal {signum}.")
    running = False


def get_dotfile(filepath, tag):
    with open(os.path.join(os.path.dirname(filepath), f".({tag})({os.path.basename(filepath)})"),
              mode='r') as dotfile:
        result = dotfile.readline().strip()
    return result


def main(configfile='/etc/dcache/container.conf'):
    global running
    global mountPoint
    global dataRoot
    global mongoUri
    global mongoDb

    # initialize logging
    logger = logging.getLogger()
    log_handler = None

    try:
        while running:
            configuration = parser.RawConfigParser(
                defaults={'scriptId': 'pack', 'mongoUri': 'mongodb://localhost/', 'mongoDb': 'smallfiles',
                          'loopDelay': 5, 'logLevel': 'ERROR'})
            configuration.read(configfile)

            script_id = configuration.get('DEFAULT', 'scriptId')

            log_level_str = configuration.get('DEFAULT', 'logLevel')
            log_level = getattr(logging, log_level_str.upper(), None)
            logger.setLevel(log_level)

            if log_handler is not None:
                log_handler.close()
                logger.removeHandler(log_handler)

            log_handler = logging.handlers.WatchedFileHandler(f'/var/log/dcache/fillmetadata-{script_id}.log')
            formatter = logging.Formatter('%(asctime)s %(name)-10s %(levelname)-8s %(message)s')
            log_handler.setFormatter(formatter)
            logger.addHandler(log_handler)

            mountPoint = configuration.get('DEFAULT', 'mountPoint')
            dataRoot = configuration.get('DEFAULT', 'dataRoot')
            mongoUri = configuration.get('DEFAULT', 'mongoUri')
            mongoDb = configuration.get('DEFAULT', 'mongodb')
            loop_delay = configuration.getint('DEFAULT', 'loopDelay')

            logger.info(f'Successfully read configuration from file {configfile}.')

            try:
                client = MongoClient(mongoUri)
                db = client[mongoDb]

                with db.files.find({'state': {'$exists': False}}, snapshot=True) as new_files_cursor:
                    logger.info(f"found {new_files_cursor.count()} new files")
                    for record in new_files_cursor:
                        if not running:
                            sys.exit(1)
                        try:
                            pathof = get_dotfile(os.path.join(mountPoint, record['pnfsid']), 'pathof')
                            localpath = pathof.replace(dataRoot, mountPoint, 1)
                            stats = os.stat(localpath)

                            record['path'] = pathof
                            record['parent'] = os.path.dirname(pathof)
                            record['size'] = stats.st_size
                            record['ctime'] = stats.st_ctime
                            record['state'] = 'new'

                            new_files_cursor.collection.save(record)
                            logger.debug(f"Updated record: {str(record)}")
                        except KeyError as e:
                            logger.warning(f"KeyError: {str(record)}: {e}")
                        except (IOError, OSError) as e:
                            if type(e) == type(IOError):
                                logger.warning(f"IOError: {str(record)}: {e}")
                            else:
                                logger.warning(f"OSError: {str(record)}: {e}")
                            logger.exception(e)
                            logger.info(f"Removing entry for file {record['pnfsid']}")
                            db.files.remove({'pnfsid': record['pnfsid']})

                client.close()

            except errors.ConnectionFailure as e:
                logger.warning(f"Connection failure: {e}")
            except errors.OperationFailure as e:
                logger.warning(f"Could not create cursor: {e}")

            logger.info("Sleeping for 60 seconds")
            time.sleep(60)

    except parser.NoOptionError as e:
        print(f"Missing option: {e}")
        logger.error(f"Missing option: {e}")
        sys.exit(2)
    except parser.Error as e:
        print(f"Error reading configfile {configfile}: {e}")
        logger.error(f"Error reading configfile {configfile}: {e}")
        sys.exit(2)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, sigint_handler)
    if not os.getuid() == 0:
        print("fillmetadata.py must run as root!")
        sys.exit(2)

    if len(sys.argv) == 1:
        main()
    elif len(sys.argv) == 2:
        main(sys.argv[1])
    else:
        print("Usage: fillmetadata.py <configfile>")
        sys.exit(1)
