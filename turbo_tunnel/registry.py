# -*- coding: utf-8 -*-

"""Registry"""

import inspect
from typing import Any, Dict, List, Optional, Type, TYPE_CHECKING

from . import utils

if TYPE_CHECKING:
    from . import server
    from . import tunnel
    from .plugins import Plugin


class ServerRegistry(object):
    """Registry for tunnel server classes"""

    _server_list: Dict[str, Type["server.TunnelServer"]]

    def __init__(self) -> None:
        self._server_list = {}

    def register(
        self, protocol: str, server_class: Type["server.TunnelServer"]
    ) -> None:
        """Register a server class for a protocol.

        Args:
            protocol: The protocol name (e.g., 'tcp', 'http', 'https')
            server_class: The server class to register
        """
        self._server_list[protocol] = server_class

    def __getitem__(self, index: str) -> Optional[Type["server.TunnelServer"]]:
        """Get server class by protocol name.

        Args:
            index: The protocol name

        Returns:
            The server class or None if not found
        """
        return self._server_list.get(index)


class TunnelRegistry(object):
    """Registry for tunnel classes"""

    _tunnel_list: Dict[str, Type["tunnel.Tunnel"]]

    def __init__(self) -> None:
        self._tunnel_list = {}

    def register(self, protocol: str, tunnel_class: Type["tunnel.Tunnel"]) -> None:
        """Register a tunnel class for a protocol.

        Args:
            protocol: The protocol name (e.g., 'tcp', 'http', 'https', 'ws')
            tunnel_class: The tunnel class to register
        """
        self._tunnel_list[protocol] = tunnel_class

    def __getitem__(self, index: str) -> Optional[Type["tunnel.Tunnel"]]:
        """Get tunnel class by protocol name.

        Args:
            index: The protocol name

        Returns:
            The tunnel class or None if not found
        """
        return self._tunnel_list.get(index)


class PluginRegistry(object):
    """Registry for plugins"""

    _plugin_list: List["Plugin"]
    _enabled: bool

    def __init__(self) -> None:
        self._plugin_list = []
        self._enabled = False

    def enable(self) -> None:
        """Enable plugin registration"""
        self._enabled = True

    def register(self, plugin_cls: Type["Plugin"]) -> None:
        """Register and initialize a plugin.

        Args:
            plugin_cls: The plugin class to register

        Raises:
            Exception: If plugin initialization fails
        """
        self.register_with_args(plugin_cls, {})

    def register_with_args(
        self, plugin_cls: Type["Plugin"], kwargs: Dict[str, Any]
    ) -> None:
        """Set arguments for the next plugin registration.

        Args:
            plugin_cls: The plugin class to register
            kwargs: Keyword arguments to pass to the plugin constructor
        """
        if not self._enabled:
            return
        utils.logger.info(
            "[%s] Register plugin: %s with args: %s"
            % (self.__class__.__name__, plugin_cls.__name__, kwargs)
        )
        plugin = plugin_cls(**kwargs)
        self._plugin_list.append(plugin)
        try:
            plugin.on_load()
        except Exception as ex:
            plugin.on_unload()
            raise ex

    def get_plugins(self) -> List["Plugin"]:
        """Get all registered plugins.

        Returns:
            List of plugin instances
        """
        return self._plugin_list

    def notify(
        self, event: str, *args: Any, **kwargs: Any
    ) -> None:  # pyright: ignore[reportExplicitAny, reportAny]
        """Notify all plugins of an event.

        Args:
            event: The event name (without 'on_' prefix)
            *args: Positional arguments to pass to event handler
            **kwargs: Keyword arguments to pass to event handler
        """
        for plugin in self._plugin_list:
            callback = getattr(plugin, "on_" + event, None)
            if not callback:
                continue
            try:
                callback(*args, **kwargs)
            except Exception:
                utils.logger.exception(
                    "[%s] Call %s.%s failed"
                    % (
                        self.__class__.__name__,
                        plugin.__class__.__name__,
                        callback.__name__,
                    )  # pyright: ignore[reportAny]
                )


server_registry = ServerRegistry()
tunnel_registry = TunnelRegistry()
plugin_registry = PluginRegistry()
