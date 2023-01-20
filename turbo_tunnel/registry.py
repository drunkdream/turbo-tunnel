# -*- coding: utf-8 -*-

"""Registry
"""

from . import utils


class ServerRegistry(object):
    def __init__(self):
        self._server_list = {}

    def register(self, protocol, server_class):
        self._server_list[protocol] = server_class

    def __getitem__(self, index):
        return self._server_list.get(index)


class TunnelRegistry(object):
    def __init__(self):
        self._tunnel_list = {}

    def register(self, protocol, tunnel_class):
        self._tunnel_list[protocol] = tunnel_class

    def __getitem__(self, index):
        return self._tunnel_list.get(index)


class PluginRegistry(object):
    def __init__(self):
        self._plugin_list = []
        self._enabled = False

    def enable(self):
        self._enabled = True

    def register(self, plugin_cls):
        if not self._enabled:
            return
        plugin = plugin_cls()
        self._plugin_list.append(plugin)
        try:
            plugin.on_load()
        except Exception as ex:
            plugin.on_unload()
            raise ex

    def get_plugins(self):
        return self._plugin_list

    def notify(self, event, *args, **kwargs):
        for plugin in self._plugin_list:
            callback = getattr(plugin, "on_" + event)
            if not callback:
                continue
            try:
                callback(*args, **kwargs)
            except:
                utils.logger.exception(
                    "Call %s.%s failed" % (plugin.__class__.__name__, callback.__name__)
                )


server_registry = ServerRegistry()
tunnel_registry = TunnelRegistry()
plugin_registry = PluginRegistry()
