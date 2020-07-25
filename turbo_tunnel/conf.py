# -*- coding: utf-8 -*-
'''Tunnel configuration
'''

import asyncio
import fnmatch
import os
import socket

import yaml

from . import utils


class Tunnel(object):
    def __init__(self, tunnel):
        self._id = tunnel['id']
        self._url = utils.Url(tunnel['url'])
        self._is_default = tunnel.get('default', False)
        self._dependency = tunnel.get('dependency', None)

    def __str__(self):
        return '<Tunnel object id=%s url=%s at 0x%.x>' % (self._id, self._url,
                                                          id(self))

    @property
    def id(self):
        return self._id

    @property
    def url(self):
        return self._url

    @property
    def urls(self):
        url_list = [self._url]
        dependency = self._dependency
        while dependency:
            url_list.insert(0, dependency.url)
            dependency = dependency.dependency
        return url_list

    @property
    def dependency(self):
        return self._dependency

    @dependency.setter
    def dependency(self, value):
        self._dependency = value

    def is_default(self):
        return self._is_default

    def is_blocked(self):
        return self._url.protocol == 'block'


class TunnelRule(object):
    def __init__(self, rule):
        self._id = rule['id']
        self._priority = rule.get('priority', 0)
        self._addr_list = rule.get('addr', '*').split(';')
        self._port_list = rule.get('port', '1-65535')
        if isinstance(self._port_list, int):
            self._port_list = str(self._port_list)
        self._port_list = self._port_list.split(';')
        self._tunnel = rule['tunnel']

    def __str__(self):
        return '<TunnelRule object id=%s priority=%d at 0x%x>' % (self._id, self._priority, id(self))

    @property
    def id(self):
        return self._id

    @property
    def priority(self):
        return self._priority

    @property
    def tunnel(self):
        return self._tunnel

    async def is_hit(self, address):
        host, port = address
        host_match = False
        for tmpl in self._addr_list:
            if fnmatch.fnmatch(host, tmpl.strip()):
                host_match = True
                break

        if not host_match and not utils.is_ip_address(host):
            addr, port = await utils.resolve_address(address)
            if addr:
                for tmpl in self._addr_list:
                    if fnmatch.fnmatch(addr, tmpl.strip()):
                        host_match = True
                        break
        if not host_match:
            return False

        for port_str in self._port_list:
            port_str = port_str.strip()
            if port_str.isdigit() and int(port_str) == port:
                return True
            elif '-' in port_str:
                port_start, port_end = port_str.split('-')
                port_start = int(port_start)
                port_end = int(port_end)
                if port >= port_start and port <= port_end:
                    return True
        return False


class TunnelConfiguration(object):
    '''Tunnel Configuration
    '''
    reload_interval = 1

    def __init__(self, conf_file, auto_reload=False):
        self._conf_file = conf_file
        if not os.path.exists(self._conf_file):
            raise RuntimeError('Configuration file %s not exist' %
                               self._conf_file)
        self._auto_reload = auto_reload
        self._last_modified = None
        self.load()
        if self._auto_reload:
            utils.AsyncTaskManager().start_task(self.reload_task())

    async def reload_task(self):
        while True:
            await asyncio.sleep(self.reload_interval)
            self.load()

    def parse(self):
        with open(self._conf_file) as fp:
            text = fp.read()
            try:
                return yaml.safe_load(text)
            except:
                utils.logger.warn('[%s] Ignore invalid config file' %
                                  self.__class__.__name__)
                return None

    def load(self):
        if not os.path.exists(self._conf_file):
            utils.logger.warn('[%s] Config file not exist' %
                              self.__class__.__name__)
            return
        last_modified = os.path.getmtime(self._conf_file)
        if not self._last_modified or last_modified > self._last_modified:
            if self._last_modified:
                utils.logger.info('[%s] Reload config file' %
                                  self.__class__.__name__)
            config = self.parse()
            if not config:
                return
            self._conf_obj = config
            if not self._conf_obj.get('version'):
                raise utils.ConfigError('Field `version` not found')
            elif not self._conf_obj.get('listen'):
                raise utils.ConfigError('Field `listen` not found')

            self._tunnels = []
            for tunnel in self._conf_obj.get('tunnels', []):
                self._tunnels.append(Tunnel(tunnel))
            self._rules = []
            for rule in self._conf_obj.get('rules', []):
                self._rules.append(TunnelRule(rule))
            self._last_modified = last_modified

    @property
    def version(self):
        return self._conf_obj['version']

    @property
    def listen_url(self):
        return self._conf_obj['listen']

    @property
    def rules(self):
        rules = self._rules[:]
        rules.sort(key=lambda it: it.priority, reverse=True)
        return rules

    @property
    def default_tunnel(self):
        for tunnel in self._tunnels:
            if tunnel.is_default():
                return tunnel
        return None

    def get_tunnel(self, id):
        for tunnel in self._tunnels:
            if tunnel.id == id:
                if tunnel.dependency and isinstance(tunnel.dependency, str):
                    depend_tunnel = self.get_tunnel(tunnel.dependency)
                    tunnel.dependency = depend_tunnel
                return tunnel
        else:
            raise RuntimeError('Tunnel %s not found' % id)
