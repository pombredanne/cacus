#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import logging
from yapsy.PluginManager import PluginManager
from yapsy.IPlugin import IPlugin

log = logging.getLogger('cacus.loader')
yapsy_log = logging.getLogger('yapsy')


class IStoragePlugin(IPlugin):

    def configure(self, config):
        raise NotImplementedError

    def put(self, key, filename):
        raise NotImplementedError

    def get(self, key):
        raise NotImplementedError


class PluginInitException(Exception):
    pass


def load_plugins(config):
    loaded_plugins = {}
    manager = PluginManager()

    cwd = os.path.abspath(os.path.dirname(__file__))
    plugin_dirs = config.get('plugin_path', [])
    plugin_dirs.append(os.path.join(cwd, 'plugins'))
    log.debug("Searching plugins in %s", plugin_dirs)
    manager.setPluginPlaces(plugin_dirs)
    manager.setPluginInfoExtension('plugin')

    manager.setCategoriesFilter({'storage': IStoragePlugin})
    manager.collectPlugins()

    for category in ('storage',):
        try:
            cfg = config[category]
            for p in manager.getPluginsOfCategory(category):
                log.info("Found plugin %s", p.name)
                if p.name == cfg['type']:
                    manager.activatePluginByName(p.name)
                    log.info("Activating storage plugin %s", p.name)
                    p.plugin_object.configure(cfg)
                    loaded_plugins[category] = p
                    break
        except:
            log.exception('Unable to load plugin category %s', category)

    return loaded_plugins
