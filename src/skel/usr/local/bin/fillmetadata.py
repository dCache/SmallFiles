#!/usr/bin/env python
# coding=utf-8

import os
import sys
import time
import signal
import ConfigParser as parser
from pymongo import MongoClient, Connection, errors
import logging
import logging.handlers

running = True

def sigint_handler(signum, frame):
    global running
    logging.info("Caught signal %d." % signum)
    print("Caught signal %d.'" % signum)
    running = False

mongoUri = "mongodb://localhost/"
mongoDb  = "smallfiles"
mountPoint = ""
dataRoot = ""

def dotfile(filepath, tag):
    with open(os.path.join(os.path.dirname(filepath), ".(%s)(%s)" % (tag, os.path.basename(filepath))), mode='r') as dotfile:
       result = dotfile.readline().strip()
    return result

def main(configfile = '/etc/dcache/container.conf'):
    global running

    #initialize logging
    logger = logging.getLogger()
    log_handler = None

    try:
        while running:
            configuration = parser.RawConfigParser(defaults = { 'scriptId': 'pack', 'mongoUri': 'mongodb://localhost/', 'mongoDb': 'smallfiles', 'loopDelay': 5, 'logLevel': 'ERROR' })
            configuration.read(configfile)

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

            log_handler = logging.handlers.WatchedFileHandler('/var/log/dcache/fillmetadata-%s.log' % scriptId)
            formatter = logging.Formatter('%(asctime)s %(name)-80s %(levelname)-8s %(message)s')
            log_handler.setFormatter(formatter)
            logger.addHandler(log_handler)

            mountPoint = configuration.get('DEFAULT', 'mountPoint')
            dataRoot = configuration.get('DEFAULT', 'dataRoot')
            mongoUri = configuration.get('DEFAULT', 'mongoUri')
            mongoDb  = configuration.get('DEFAULT', 'mongodb')
            loopDelay = configuration.getint('DEFAULT', 'loopDelay')
            
            logging.info('Successfully read configuration from file %s.' % configfile)

            try:
                client = MongoClient(mongoUri)
                db = client[mongoDb]

                with db.files.find( { 'state': { '$exists': False } }, snaphot=True ) as newFilesCursor:
                    logging.info("found %d new files" % (newFilesCursor.count()))
                    for record in newFilesCursor:
                        if not running:
                            sys.exit(1)
                        try:
                            pathof = dotfile(os.path.join(mountPoint, record['pnfsid']), 'pathof')
                            localpath = pathof.replace(dataRoot, mountPoint, 1)
                            stats = os.stat(localpath)

                            record['path'] = pathof
                            record['parent'] = os.path.dirname(pathof)
                            record['size'] = stats.st_size
                            record['ctime'] = stats.st_ctime
                            record['state'] = 'new'

                            newFilesCursor.collection.save(record)
                            logging.debug("Updated record: %s" % str(record))
                        except KeyError as e:
                            logging.warn("KeyError: %s: %s" % (str(record), e.message))
                        except IOError as e:
                            logging.warn("IOError: %s: %s" % (str(record), e.message))
                            logging.info("Removing entry for file %s" % record['pnfsid'])
                            db.files.remove( { 'pnfsid': record['pnfsid'] } )
                        except OSError as e:
                            logging.warn("OSError: %s: %s" % (str(record), e.message))
                            logging.exception(e)
                            logging.info("Removing entry for file %s" % record['pnfsid'])
                            db.files.remove( { 'pnfsid': record['pnfsid'] } )

                client.close()

            except errors.ConnectionFailure as e:
                logging.warn("Connection failure: %s" % e.message)
            except errors.OperationFailure as e:
                logging.warn("Could not create cursor: %s" % e.message)

            logging.info("Sleeping for 60 seconds")
            time.sleep(60)

    except parser.NoOptionError as e:
        print("Missing option: %s" % e.message)
        logging.ERROR("Missing option: %s" % e.message)
        sys.exit(2)
    except parser.Error as e:
        print("Error reading configfile %s: %s" % (configfile, e.message))
        logging.ERROR("Error reading configfile %s" % (configfile, e.message))
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

