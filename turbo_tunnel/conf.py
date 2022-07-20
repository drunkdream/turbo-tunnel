# -*- coding: utf-8 -*-
"""Tunnel configuration
"""

import asyncio
import fnmatch
import os
import tempfile

import yaml

from . import utils


class Tunnel(object):
    def __init__(self, tunnel):
        self._id = tunnel["id"]
        self._url = utils.Url(tunnel["url"])
        self._is_default = tunnel.get("default", False)
        self._dependency = tunnel.get("dependency", None)

    def __eq__(self, other):
        if not other or not isinstance(other, Tunnel):
            return False
        else:
            return self._id == other.id

    def __str__(self):
        return "<Tunnel object id=%s url=%s at 0x%.x>" % (self._id, self._url, id(self))

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
        return self._url.protocol == "block"


class TunnelRule(object):
    def __init__(self, rule):
        self._id = rule["id"]
        self._priority = rule.get("priority", 0)
        self._addr_list = rule.get("addr", "*").split(";")
        self._port_list = rule.get("port", "1-65535")
        if isinstance(self._port_list, int):
            self._port_list = str(self._port_list)
        self._port_list = self._port_list.split(";")
        self._tunnel = rule["tunnel"]

    def __str__(self):
        return "<TunnelRule object id=%s priority=%d at 0x%x>" % (
            self._id,
            self._priority,
            id(self),
        )

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

        if not host_match:
            return False

        for port_str in self._port_list:
            port_str = port_str.strip()
            if port_str.isdigit() and int(port_str) == port:
                return True
            elif "-" in port_str:
                port_start, port_end = port_str.split("-")
                port_start = int(port_start)
                port_end = int(port_end)
                if port >= port_start and port <= port_end:
                    return True
        return False


class TunnelConfiguration(object):
    """Tunnel Configuration"""

    reload_interval = 1

    @staticmethod
    def create(conf_addr, root=None, auto_reload=False):
        if conf_addr.startswith("http://") or conf_addr.startswith("https://"):
            return TunnelHTTPConfiguration(
                conf_addr, root=root, auto_reload=auto_reload
            )
        elif os.path.isfile(conf_addr):
            return TunnelConfiguration(conf_addr, root=root, auto_reload=auto_reload)
        else:
            raise ValueError("Unsupported config file %s" % conf_addr)

    def __init__(self, conf_file, root=None, auto_reload=False):
        self._conf_file = conf_file
        self._auto_reload = auto_reload
        self._conf_obj = None
        self._listen_urls = []
        self._tunnels = []
        self._rules = []
        self._last_modified = None
        self._root = root
        if self._auto_reload:
            utils.AsyncTaskManager().start_task(self.reload_task())

    async def reload_task(self):
        while True:
            await asyncio.sleep(self.reload_interval)
            await self.load()

    def parse(self):
        with open(self._conf_file, encoding="utf-8") as fp:
            text = fp.read()
            try:
                return yaml.safe_load(text)
            except:
                utils.logger.exception(
                    "[%s] Ignore invalid config file" % self.__class__.__name__
                )
                return None

    async def load(self):
        if not os.path.exists(self._conf_file):
            utils.logger.warn(
                "[%s] Config file %s not exist"
                % (self.__class__.__name__, self._conf_file)
            )
            return False
        last_modified = os.path.getmtime(self._conf_file)
        if not self._last_modified or last_modified > self._last_modified:
            if self._last_modified:
                utils.logger.info("[%s] Reload config file" % self.__class__.__name__)
            config = self.parse()
            if not config:
                return False
            self._conf_obj = config
            if not self._conf_obj.get("version"):
                raise utils.ConfigError("Field `version` not found")
            self._listen_urls = []
            if self._conf_obj.get("listen"):
                listen_urls = self._conf_obj.get("listen")
                if isinstance(listen_urls, list):
                    self._listen_urls.extend(listen_urls)
                else:
                    self._listen_urls.append(listen_urls)

            self._tunnels = []
            for tunnel in self._conf_obj.get("tunnels", []):
                self._tunnels.append(Tunnel(tunnel))
            self._rules = []
            for rule in self._conf_obj.get("rules", []):
                self._rules.append(TunnelRule(rule))
            for plugin in self._conf_obj.get("plugins", []):
                for module in ("turbo_tunnel.plugins.%s" % plugin, plugin):
                    try:
                        __import__(module)
                    except ImportError:
                        pass
                    else:
                        break
                else:
                    utils.logger.error(
                        "[%s] Load plugin %s failed" % (self.__class__.__name__, plugin)
                    )
            for conf_addr in self.external_confs:
                config = TunnelConfiguration.create(
                    conf_addr, root=self, auto_reload=self._auto_reload
                )
                if not await config.load():
                    utils.logger.warn(
                        "[%s] Load external config file %s failed"
                        % (self.__class__.__name__, conf_addr)
                    )
                    continue
                self._listen_urls.extend(config.external_confs)
                for tunnel in config.tunnels:
                    if not tunnel in self._tunnels:
                        self._tunnels.append(tunnel)
                self._rules.extend(config.rules)

            if not self._root and not self._listen_urls:
                raise utils.ConfigError("Field `listen` not found")
            self._last_modified = last_modified
        return True

    @property
    def version(self):
        return self._conf_obj["version"]

    @property
    def listen_urls(self):
        return self._listen_urls

    @property
    def external_confs(self):
        confs = self._conf_obj.get("include", [])
        if confs and not isinstance(confs, list):
            confs = [confs]
        return confs

    @property
    def tunnels(self):
        return self._tunnels

    @property
    def rules(self):
        rules = self._rules[:]
        rules.sort(key=lambda it: it.priority, reverse=True)
        return rules

    @property
    def hosts(self):
        hosts = {}
        for it in self._conf_obj.get("hosts", []):
            hosts[it["domain"]] = it["ip"]
        return hosts

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
            raise RuntimeError("Tunnel %s not found" % id)


class TunnelHTTPConfiguration(TunnelConfiguration):
    """Tunnel HTTP Configuration"""

    def __init__(self, conf_url, root=None, auto_reload=False):
        self._conf_url = conf_url
        super(TunnelHTTPConfiguration, self).__init__(
            tempfile.mkstemp(".yaml")[1], root, auto_reload
        )

    async def load(self):
        content = await utils.fetch(self._conf_url)
        if not content:
            utils.logger.warn(
                "[%s] Fetch config file url %s failed"
                % (self.__class__.__name__, self._conf_url)
            )
            return False
        with open(self._conf_file, "wb") as fp:
            fp.write(content)
        return await super(TunnelHTTPConfiguration, self).load()
