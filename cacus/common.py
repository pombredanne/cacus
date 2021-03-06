#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import time
import yaml
import uuid
import gnupg
import pymongo
import hashlib
import requests
import logging
import apt_pkg
import logging.handlers
from binascii import hexlify
from threading import Event
from itertools import chain, repeat
from tornado.ioloop import IOLoop

import plugin


class _ColorFormatter(logging.Formatter):

    def format(self, record):
        if record.levelname == "DEBUG":
            record.levelname = "\033[0;35m[DEBUG]\033[0m"
        elif record.levelname == "INFO":
            record.levelname = "\033[0;36m[INFO]\033[0m"
        elif record.levelname == "WARNING":
            record.levelname = "\033[0;33m[WARN]\033[0m"
        elif record.levelname == "ERROR":
            record.levelname = "\033[0;31m[ERRO]\033[0m"
        elif record.levelname == "CRITICAL":
            record.levelname = "\033[0;41m[CRIT]\033[0m"

        # highlight own classes
        if 'cacus' in record.name:
            record.name = "\033[0;01m{}\033[0m".format(record.name)

        return super(_ColorFormatter, self).format(record)


class CacusError(Exception):
    http_code = 500


class FatalError(CacusError):
    http_code = 500


class TemporaryError(CacusError):
    http_code = 409


class Timeout(CacusError):
    http_code = 504


class NotFound(CacusError):
    http_code = 404


class Conflict(CacusError):
    http_code = 409


class DistroLockTimeout(CacusError):
    http_code = 409


class DebVersion(object):
    """ Just wrapper around apt_pkg.version_compare() """

    def __init__(self, version):
        apt_pkg.init_system()
        self.version = version

    def __eq__(self, x):
        return apt_pkg.version_compare(self.version, x.version) == 0

    def __ne__(self, x):
        return not self == x

    def __lt__(self, x):
        return apt_pkg.version_compare(self.version, x.version) < 0

    def __gt__(self, x):
        return apt_pkg.version_compare(self.version, x.version) > 0

    def __ge__(self, x):
        return self == x or self > x

    def __le__(self, x):
        return self == x or self < x


class Cacus(object):
    default_arches = ['all', 'amd64', 'i386']

    def __init__(self, config_file=None, config=None, mongo=None):
        os.environ['PATH'] += ':/usr/local/bin'
        if not config:
            if not config_file:
                if os.path.isfile('/etc/cacus.yml'):
                    config_file = '/etc/cacus.yml'
                else:
                    config_file = '/etc/cacus-default.yml'
            with open(config_file) as cfg:
                self.config = yaml.load(cfg)
        else:
            self.config = config

        # logging
        handlers = _setup_log_handlers(self.config['logging']['app'])

        class __AccessLogFilter(logging.Filter):

            def filter(self, record):
                return record.name != 'tornado.access'

        self._rootLogger = logging.getLogger('')

        levels = {
            'debug': logging.DEBUG, 'info': logging.INFO, 'warning': logging.WARNING,
            'error': logging.ERROR, 'critical': logging.CRITICAL
        }
        print self.config
        self._rootLogger.setLevel(levels[self.config['logging']['level']])
        for handler in handlers:
            # repo_daemon will setup own logger for his access logs,
            # so filter out 'tornado.access' entries from app log
            handler.addFilter(__AccessLogFilter())
            self._rootLogger.addHandler(handler)

        self.log = logging.getLogger('cacus.{}'.format(type(self).__name__))

        # GPG
        try:
            self.gpg = gnupg.GPG(homedir=self.config['gpg']['home'])
            # gnupg is a little bit too talkative
            logging.getLogger('gnupg').setLevel(logging.WARNING)
            if not self._check_key(self.config['gpg']['sign_key']):
                raise Exception("Cannot find secret key with ID {}".format(self.config['gpg']['sign_key']))
        except Exception as e:
            self.log.critical("GPG initialization error: %s", e)
            sys.exit(1)

        # mongo
        if not mongo:
            self.config['db']['connect'] = False
            self.db = pymongo.MongoClient(**(self.config['db']))
        else:
            self.db = mongo

        # plugins
        self._plugins = plugin.load_plugins(self.config)
        self.storage = self._plugins['storage'].plugin_object

        # misc
        self.config['repo_daemon']['repo_base'] = self.config['repo_daemon']['repo_base'].rstrip('/')
        self.config['repo_daemon']['storage_subdir'] = self.config['repo_daemon']['storage_subdir'].rstrip('/').lstrip('/')

    def create_cacus_indexes(self):
        self.log.info("Creating indexes for cacus.distros...")
        self.db.cacus.distros.create_index('distro', unique=True)
        self.db.cacus.distros.create_index('snapshot')

        self.log.info("Creating indexes for cacus.components...")
        self.db.cacus.components.create_index(
            [('distro', pymongo.DESCENDING),
             ('component', pymongo.DESCENDING)],
            unique=True)
        self.db.cacus.components.create_index('snapshot')

        self.log.info("Creating indexes for cacus.repos...")
        self.db.cacus.repos.create_index(
            [('distro', pymongo.DESCENDING),
             ('component', pymongo.DESCENDING),
             ('architecture', pymongo.DESCENDING)],
            unique=True)

        self.log.info("Creating indexes for cacus.locks...")
        self.db.cacus.locks.create_index([
            ('distro', pymongo.DESCENDING),
            ('comp', pymongo.DESCENDING)],
            unique=True)
        self.db.cacus.locks.create_index('modified', expireAfterSeconds=self.config['lock_cleanup_timeout'])

    def create_packages_indexes(self, distros=None):
        if not distros:
            distros = self.db.packages.collection_names()

        for distro in distros:
            self.log.info("Creating indexes for packages.%s...", distro)
            self.db.packages[distro].create_index(
                [('Package', pymongo.DESCENDING),
                 ('Version', pymongo.DESCENDING),
                 ('Architecture', pymongo.DESCENDING)],
                unique=True)
            self.db.packages[distro].create_index(
                [('components', pymongo.DESCENDING),
                 ('Architecture', pymongo.DESCENDING)])
            self.db.packages[distro].create_index('source')
            self.db.packages[distro].create_index([('meta.Description', pymongo.TEXT)])

            self.db.sources[distro].create_index(
                [('Package', pymongo.DESCENDING),
                 ('Version', pymongo.DESCENDING)],
                unique=True)
            self.db.sources[distro].create_index('components')

    @staticmethod
    def get_hashes(file=None, filename=None):
        # XXX: This is pretty fat function, but I have no idea how to optimize it - my tests shows that
        # it's almost as fast as "openssl [md5,sha1,sha256,sha256]", despite it's pretty straightforward approach
        if filename:
            file = open(filename)

        md5 = hashlib.md5()
        sha1 = hashlib.sha1()
        sha256 = hashlib.sha256()
        sha512 = hashlib.sha512()

        fpos = file.tell()
        file.seek(0)

        # 128 KiB is default readahead for big files
        for chunk in iter(lambda: file.read(1024*128), b''):
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
            sha512.update(chunk)

        file.seek(fpos)

        if filename:
            file.close()

        return {'md5': md5, 'sha1': sha1, 'sha256': sha256, 'sha512': sha512}

    def download_file(self, url, filename=None, md5=None, sha1=None, sha256=None):
        log = logging.getLogger("cacus.downloader")
        if not filename:
            filename = os.path.join(self.config['duploader_daemon']['incoming_root'], "cacus_tmp_" + str(uuid.uuid1()))

        try:
            total_bytes = 0
            log.debug("Downloading %s to %s", url, filename)
            r = requests.get(url, stream=True)
            if r.status_code == 200:
                _md5 = hashlib.md5()
                _sha1 = hashlib.sha1()
                _sha256 = hashlib.sha256()
                with open(filename, 'w') as f:
                    for chunk in r.iter_content(4*1024*1024):
                        total_bytes += len(chunk)
                        f.write(chunk)
                        _md5.update(chunk)
                        _sha1.update(chunk)
                        _sha256.update(chunk)
                    _md5 = _md5.digest()
                    _sha1 = _sha1.digest()
                    _sha256 = _sha256.digest()
                if sha256 and sha256 != _sha256:
                    raise TemporaryError("SHA256 mismatch for {}: got {} instead of {}".format(url, hexlify(_sha256), hexlify(sha256)))
                if sha1 and sha1 != _sha1:
                    raise TemporaryError("SHA1 mismatch for {}: got {} instead of {}".format(url, hexlify(_sha1), hexlify(sha1)))
                if md5 and md5 != _md5:
                    raise TemporaryError("MD5 mismatch for {}: got {} instead of {}".format(url, hexlify(_md5), hexlify(md5)))
            elif r.status_code == 404:
                r.close()
                raise NotFound("{} returned {} {}".format(url, r.status_code, r.reason))
            else:
                r.close()
                raise TemporaryError("{} returned {} {}".format(url, r.status_code, r.reason))
            log.debug("GET %s %s %s bytes %s sec", url, r.status_code, total_bytes, r.elapsed.total_seconds())
            r.close()
        except (requests.ConnectionError, requests.HTTPError) as e:
            raise TemporaryError("Cannot fetch {}: {}".format(url, e))
        except requests.Timeout as e:
            raise Timeout("Cannot fetch {}: {}".format(url, e))
        except IOError as e:
            raise FatalError("Cannot fetch {} to {}: {}".format(url, filename, e))

        return filename

    def gpg_sign(self, data, key_id=None):
        if not key_id:
            key_id = self.config['gpg']['sign_key']
        signature = self.gpg.sign(data, default_key=key_id, detach=True, clearsign=False)
        return signature.data

    def _check_key(self, key_id):
        public_keys = [x for x in self.gpg.list_keys(secret=False) if key_id in x['keyid']]
        secret_keys = [x for x in self.gpg.list_keys(secret=True) if key_id in x['keyid']]
        return len(public_keys) > 0 and len(secret_keys) > 0


class ProxyStream(object):
    """ stream-like object for streaming result of blocking function to
        client of Tornado server
    """

    def __init__(self, handler, headers=[]):
        self._handler = handler
        self._headers = headers
        self._headers_set = False

    def sync_write(self, data, event):
        self._handler.write(data)
        self._handler.flush(callback=lambda: event.set())

    def write(self, data):
        if not self._handler.dead:
            if not self._headers_set:
                # send headers once we got first chunk of data (i.e storage is responding and found requested key)
                for header in self._headers:
                    self._handler.set_header(*header)
                self._headers_set = True

            event = Event()
            # write() and sync() should be called from thread where ioloop is running
            # so schedule write & flush for next iteration
            IOLoop.current().add_callback(self.sync_write, data, event)
            event.wait()
            return 0    # len(data)
        else:
            raise IOError("Client has closed connection")


class DistroLock(object):
    """ Poor man's implementation of distributed lock in mongodb.
    Ostrich algorithm used for dealing with deadlocks. You can always add some retries if returning 409 is not an option
    """

    def __init__(self, db, distro, comps=None, timeout=30, already_locked=False):
        self.db = db
        self.distro = distro
        # do nothing if already_locked == True, intended to use if caller do knows that he is already under lock
        self.already_locked = already_locked
        if not comps:
            self.comps = [x['component'] for x in self.db.cacus.components.find({'distro': distro}, {'component': 1})]
        else:
            self.comps = comps
        self.timeout = timeout
        self.log = logging.getLogger("cacus.RepoLock")

    def _unlock(self, comps):
        for comp in comps:
            try:
                self.db.cacus.locks.find_one_and_update(
                    {'distro': self.distro, 'comp': comp, 'locked': 1},
                    {
                        '$set': {'distro': self.distro, 'comp': comp, 'locked': 0},
                        '$currentDate': {'modified': {'$type': 'date'}}
                    },
                    upsert=True)
                self.log.debug("%s/%s unlocked", self.distro, comp)
            except pymongo.errors.DuplicateKeyError:
                pass
            except:
                self.log.error("Error while unlocking %s/%s: %s", self.distro, comp, sys.exc_info())

    def __enter__(self):
        if self.already_locked:
            self.log.debug("%s/%s is already locked", self.distro, self.comps)
            return

        self.log.debug("Trying to lock %s/%s", self.distro, self.comps)
        while True:
            locked = []
            for comp in self.comps:
                try:
                    self.db.cacus.locks.find_one_and_update(
                        {'distro': self.distro, 'comp': comp, 'locked': 0},
                        {
                            '$set': {'distro': self.distro, 'comp': comp, 'locked': 1},
                            '$currentDate': {'modified': {'$type': 'date'}}
                        },
                        upsert=True)
                    self.log.debug("%s/%s locked", self.distro, comp)
                    locked.append(comp)
                except pymongo.errors.DuplicateKeyError:
                    self._unlock(locked)
                    time.sleep(1)
                    self.timeout -= 1
                    if self.timeout > 0:
                        break   # try to lock all comps once again
                    else:
                        raise DistroLockTimeout("Timeout while trying to lock distro {0}/{1}".format(self.distro, comp))
                except:
                    self.log.error("Error while locking %s/%s: %s", self.distro, comp, sys.exc_info())
                    self._unlock(locked)
                    raise FatalError("Error while locking {}/{}: {}", self.distro, comp, sys.exc_info())
            else:
                break       # good, we just locked all comps

    def __exit__(self, exc_type, exc_value, traceback):
        if not self.already_locked:
            self._unlock(self.comps)


def _setup_log_handlers(config):
    handlers = []
    if config['file']:
        h = logging.handlers.WatchedFileHandler(config['file'])
        h.setFormatter(__logFormatter)
        handlers.append(h)
    if config['syslog']:
        h = logging.handlers.SysLogHandler(facility=config['syslog'])
        h.setFormatter(logging.Formatter("[%(levelname)-4.4s] %(name)s: %(message)s"))
        handlers.append(h)
    # keep console formatter at the end of the line, since it adds terminal escape sequences to log entry attributes
    if config['console']:
        h = logging.StreamHandler(stream=sys.stdout)
        h.setFormatter(__colorFormatter)
        handlers.append(h)

    return handlers


def with_retries(attempts, delays, fun, *args, **kwargs):
    # repeat last delay infinitely
    delays = chain(delays[:-1], repeat(delays[-1]))
    exc = Exception("Don't blink!")
    for attempt in xrange(attempts):
        try:
            result = fun(*args, **kwargs)
        except (Timeout, TemporaryError, DistroLockTimeout) as e:
            exc = e
            pass
        except (FatalError, NotFound, Exception):
            raise
        else:
            break
        time.sleep(delays.next())
    else:
        raise exc
    return result


__logFormatter = logging.Formatter("%(asctime)s [%(process)d] [%(levelname)s] %(name)s: %(message)s")
__colorFormatter = _ColorFormatter("\033[0;33m%(asctime)s\033[0m \033[0;32m[%(process)d]\033[0m %(levelname)s %(name)s: %(message)s")
