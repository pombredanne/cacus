#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import urllib2
import mmap
import logging
import xml.etree.ElementTree as ET

from yapsy.IPlugin import IPlugin
import plugins

class MDSStorage(plugins.IStoragePlugin):
    def configure(self, config):
        self.base_url = config['base_url']
        self.auth_header = config['auth_header']

    def put(self, key, filename):
        with open(filename, 'rb') as f:
            file =  mmap.mmap(f.fileno(), 0, access = mmap.ACCESS_READ)
            url = "{0}{1}".format(self.base_url, key)
            print("HTTP POST {0}".format(url))

            request = urllib2.Request(url, file)
            request.add_header(self.auth_header[0], self.auth_header[1])

            try:
                response_fp = urllib2.urlopen(request)
                response = ET.fromstring(response_fp.read())
                file.close()
            except urllib2.URLError as e:
                print "Error requesting {0}: {1}".format(url, e)
                return None
            except urllib2.HTTPError as e:
                print "Error requesting {0}: {1}".format(url ,e)
                return None


            try:
                storage_key = response.attrib['key']
            except KeyError:
                print "Wrong return from server"
                return None
            print "Got storage key {0}".format(storage_key)

        return storage_key

    def get(self, key):
        return os.path.join(self.root, key)