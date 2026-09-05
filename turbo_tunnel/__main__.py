# -*- coding: utf-8 -*-
""" """

import argparse
import asyncio
import importlib
import logging
import logging.handlers
import os
import re
import shlex
import sys
import traceback
from typing import Any, Dict, List, Optional, Tuple

import tornado.ioloop

from . import BANNER
from . import VERSION
from . import conf
from . import plugins
from . import registry
from . import route
from . import server
from . import utils

# How often the Windows event loop is woken up so that Ctrl+C can be delivered.
# See the comment in main() for why this is needed.
CONTROL_C_POLL_INTERVAL = 0.5


class HighlightFormatter(logging.Formatter):

    reset = "\x1b[0m"
    red = "\x1b[0;31m"
    green = "\x1b[0;32m"
    yellow = "\x1b[0;33m"
    blue = "\x1b[0;34m"
    purple = "\x1b[0;35m"
    cyan = "\x1b[0;36m"
    white = "\x1b[0;37m"
    grey = "\x1b[0;38m"

    light_red = "\x1b[0;91m"
    light_green = "\x1b[0;92m"
    light_yellow = "\x1b[0;93m"
    light_blue = "\x1b[0;94m"
    light_purple = "\x1b[0;95m"
    light_cyan = "\x1b[0;96m"
    light_white = "\x1b[0;97m"
    light_grey = "\x1b[0;98m"

    bold_red = "\x1b[31;1m"

    def __init__(self, format: str) -> None:
        super(HighlightFormatter, self).__init__(format)
        self._format: str = format.replace(
            "%(asctime)s", self.grey + "%(asctime)s" + self.reset
        )
        self.FORMATS: Dict[int, str] = {
            logging.DEBUG: self.cyan,
            logging.INFO: self.green,
            logging.WARNING: self.yellow,
            logging.ERROR: self.red,
            logging.CRITICAL: self.bold_red,
        }

    def format(
        self, record: logging.LogRecord
    ) -> str:  # pyright: ignore[reportImplicitOverride]
        log_fmt = self._format.replace(
            "%(levelname)s",
            self.FORMATS.get(record.levelno, "") + "%(levelname)s" + self.reset,
        )
        record.msg = re.sub(
            r"\[([\w\.-]+):(\d+)\]\[([\w\.-]+):(\d+)\]",
            r"[%(source_address)s\1%(reset)s:%(source_port)s\2%(reset)s][%(dest_address)s\3%(reset)s:%(dest_port)s\4%(reset)s]"
            % {
                "source_address": self.green,
                "source_port": self.light_purple,
                "dest_address": self.yellow,
                "dest_port": self.light_red,
                "reset": self.reset,
            },
            record.msg,
        )
        record.msg = re.sub(
            r"\[([\d\.]+)\]",
            r"[%(seconds)s\1%(reset)s]" % {"seconds": self.cyan, "reset": self.reset},
            record.msg,
        )
        record.msg = re.sub(
            r"\s+\[([\w|-|_]+)\]\s+",
            r" [%(tunnel)s\1%(reset)s] " % {"tunnel": self.purple, "reset": self.reset},
            record.msg,
        )

        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)


def _load_plugin(module_path: str) -> Optional[type]:
    """Load plugin class from module path.

    Args:
        module_path: Module path to import (e.g., 'dashboard' or 'my.custom.plugin')

    Returns:
        Plugin class that inherits from Plugin base class, or None if not found
    """
    # Try to import from turbo_tunnel.plugins first, then as standalone module
    for full_module_path in (f"turbo_tunnel.plugins.{module_path}", module_path):
        try:
            module = importlib.import_module(full_module_path)
        except ImportError:
            continue

        # Iterate through all members of the module
        for name in dir(module):
            obj = getattr(module, name)
            # Check if it's a class and inherits from Plugin (but not Plugin itself)
            if (
                isinstance(obj, type)
                and issubclass(obj, plugins.Plugin)
                and obj is not plugins.Plugin
            ):
                utils.logger.info(
                    "[PluginLoader] Found plugin class: %s from module %s"
                    % (obj.__name__, full_module_path)
                )
                return obj

        # Module imported but no plugin class found
        utils.logger.warning(
            "[PluginLoader] No Plugin subclass found in module %s" % full_module_path
        )
        return None

    # Module not found
    return None


def _parse_plugin_spec(plugin_spec: str) -> Tuple[str, Dict[str, Any]]:
    """Parse plugin specification string.

    Args:
        plugin_spec: Plugin specification in format 'plugin_name [--arg1 value1 --arg2 value2]'
                    Example: 'dashboard --port 8080 --host 0.0.0.0'

    Returns:
        Tuple of (plugin_name, kwargs_dict)
    """
    try:
        # Use shlex to properly split the command line
        parts: List[str] = shlex.split(plugin_spec)
    except ValueError as e:
        utils.logger.error(
            "[PluginLoader] Failed to parse plugin spec '%s': %s" % (plugin_spec, e)
        )
        return plugin_spec, {}

    if not parts:
        return "", {}

    plugin_name: str = parts[0]
    kwargs: Dict[str, Any] = {}

    # Parse arguments
    i: int = 1
    while i < len(parts):
        arg = parts[i]
        if arg.startswith("--"):
            key: str = arg[2:]  # Remove '--' prefix
            if i + 1 < len(parts) and not parts[i + 1].startswith("--"):
                value: str = parts[i + 1]
                # Try to convert to appropriate type
                try:
                    # Try int first
                    kwargs[key] = int(value)
                except ValueError:
                    try:
                        # Try float
                        kwargs[key] = float(value)
                    except ValueError:
                        # Keep as string
                        if value.lower() in ("true", "false"):
                            kwargs[key] = value.lower() == "true"
                        else:
                            kwargs[key] = value
                i += 2
            else:
                # Flag without value, treat as True
                kwargs[key] = True
                i += 1
        else:
            i += 1

    return plugin_name, kwargs


def handle_args(args: argparse.Namespace) -> Optional[int]:
    """Handle command line arguments

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code or None if successful
    """
    log_file: Optional[str] = None
    if args.log_file:
        log_file = os.path.abspath(args.log_file)

    handler: logging.StreamHandler[Any] = logging.StreamHandler(sys.stdout)
    fmt: str = "[%(asctime)s][%(levelname)s]%(message)s"
    enable_color_output: bool = utils.should_colorize(args.no_color)
    utils.set_color_enabled(enable_color_output)
    formatter: logging.Formatter
    if not enable_color_output:
        formatter = logging.Formatter(fmt)
    else:
        formatter = HighlightFormatter(fmt)
    handler.setFormatter(formatter)

    if args.log_level == "verbose":
        utils.logger.setLevel(5)
    elif args.log_level == "debug":
        utils.logger.setLevel(logging.DEBUG)
    elif args.log_level == "info":
        utils.logger.setLevel(logging.INFO)
    elif args.log_level == "warn":
        utils.logger.setLevel(logging.WARN)
    elif args.log_level == "error":
        utils.logger.setLevel(logging.ERROR)

    utils.logger.propagate = False
    utils.logger.addHandler(handler)

    if log_file:
        file_handler: logging.handlers.RotatingFileHandler = (
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=4
            )
        )
        file_formatter: logging.Formatter = logging.Formatter(
            "[%(asctime)s][%(levelname)s][%(filename)s][%(lineno)d]%(message)s"
        )
        file_handler.setFormatter(file_formatter)
        utils.logger.addHandler(file_handler)

    if args.plugin:
        registry.plugin_registry.enable()
        for plugin_spec in args.plugin:
            plugin_name, plugin_kwargs = _parse_plugin_spec(plugin_spec)
            plugin_cls = _load_plugin(plugin_name)
            if plugin_cls:
                if plugin_kwargs:
                    # Register plugin with parsed arguments
                    registry.plugin_registry.register_with_args(plugin_cls, plugin_kwargs)
                else:
                    registry.plugin_registry.register(plugin_cls)
            else:
                utils.logger.error("[PluginLoader] Load plugin %s failed" % plugin_name)

    # Python 3.14+: ensure an asyncio event loop exists before Tornado touches IOLoop.current()
    utils.get_or_create_event_loop()

    tunnel_servers: list[server.TunnelServer] = []
    if args.config:
        if not os.path.exists(args.config):
            print("Config file %s not exist" % args.config, file=sys.stderr)
            return -1
        config: conf.TunnelConfiguration = conf.TunnelConfiguration(
            args.config, auto_reload=args.auto_reload
        )
        loop: asyncio.AbstractEventLoop = utils.get_or_create_event_loop()
        loop.run_until_complete(config.load())
        router: route.TunnelRouter = route.TunnelRouter(config)
        for listen_url in config.listen_urls:
            tunnel_server: server.TunnelServer = server.TunnelServer(listen_url, router)
            tunnel_servers.append(tunnel_server)
    elif args.listen:
        tunnel_list: List[str] = args.tunnel if args.tunnel else ["tcp://"]
        tunnel_server = server.TunnelServer(args.listen, tunnel_list)  # type: ignore[arg-type]
        tunnel_servers.append(tunnel_server)
    else:
        print("Argument --listen not specified", file=sys.stderr)
        return -1

    if args.retry:
        server.TunnelServer.retry_count = args.retry

    # NOTE: Do NOT force a SelectorEventLoop here. On Windows the default
    # ProactorEventLoop is the only one that supports both ICMP raw sockets
    # and asyncio subprocesses (verified on this machine). Creating a second
    # event loop instance and set_event_loop() here would also split the
    # process across two loops and trigger "attached to a different loop"
    # errors once the SSH server starts.

    for tunnel_server in tunnel_servers:
        tunnel_server.start()

    return None


def main() -> int:
    """Main entry point for TurboTunnel.

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    # The banner is printed before argparse runs, so --no-color is detected from
    # argv directly. The resulting decision is then shared with the rest of the
    # program through utils.color_enabled().
    utils.set_color_enabled(utils.should_colorize("--no-color" in sys.argv[1:]))
    if utils.color_enabled():
        print("\x1b[0;36m%s \x1b[0;32m v%s\x1b[0m\n" % (BANNER.rstrip(), VERSION))
    else:
        print("%s v%s\n" % (BANNER.rstrip(), VERSION))
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="turbo-tunnel", description="TurboTunnel cmdline tool v%s" % VERSION
    )
    parser.add_argument("-c", "--config", help="config yaml file path")
    parser.add_argument("-l", "--listen", help="listen url")
    parser.add_argument("-t", "--tunnel", action="append", help="tunnel url")
    parser.add_argument(
        "--log-level",
        help="log level, default is info",
        choices=("verbose", "debug", "info", "warn", "error"),
        default="info",
    )
    parser.add_argument("--log-file", help="log file save path")
    parser.add_argument("--retry", type=int, help="retry connect count", default=0)
    parser.add_argument(
        "--auto-reload",
        help="auto reload config file",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "-d", "--daemon", help="run as daemon", action="store_true", default=False
    )
    parser.add_argument(
        "--no-color", help="disable color output", action="store_true", default=False
    )
    parser.add_argument(
        "--stop-on-error",
        help="stop on error occured",
        action="store_true",
        default=False,
    )
    parser.add_argument("-p", "--plugin", help="load plugin", action="append")
    parser.add_argument(
        "-V",
        "--version",
        help="show current version",
        action="store_true",
        default=False,
    )

    raw_args: list[str] = sys.argv[1:]
    if not raw_args:
        parser.print_help()
        return 0

    args: argparse.Namespace = parser.parse_args(raw_args)

    if args.version:
        print("v%s" % VERSION)
        return 0

    if sys.platform != "win32" and args.daemon:
        import daemon

        # fork must be called before create event loop
        daemon.DaemonContext(stderr=open("turbo-tunnel.error.log", "w")).open()
    elif args.daemon:
        utils.win32_daemon()
        return 0

    result: Optional[int] = handle_args(args)
    if result is not None:
        return result

    def handle_exception(
        loop: asyncio.AbstractEventLoop, context: Dict[str, Any]
    ) -> None:
        """Handle exceptions in the event loop.

        Args:
            loop: The event loop
            context: Exception context dictionary
        """
        registry.plugin_registry.notify("unload")
        print("Exception caught:\n", file=sys.stderr)
        message: str = context["message"]
        exp: Optional[BaseException] = context.get("exception")
        if exp:
            try:
                message = "".join(
                    traceback.format_exception(type(exp), exp, exp.__traceback__)
                )
            except TypeError:
                # Python 3.10+ renamed traceback.format_exception to take a
                # single exception instance as the first positional argument.
                message = "".join(traceback.format_exception(exp))
        print(message, file=sys.stderr)
        if args.stop_on_error:
            loop.stop()

    loop: asyncio.AbstractEventLoop = utils.get_or_create_event_loop()
    loop.set_exception_handler(handle_exception)

    if sys.platform == "win32":
        # On Windows the default loop is ProactorEventLoop. When there is nothing
        # to do it blocks inside GetQueuedCompletionStatus with no timeout, and
        # Windows only delivers SIGINT once the main thread gets back to Python
        # bytecode -- so Ctrl+C looks like it does nothing. A periodic tick caps
        # that block and lets the interpreter run the signal handler.
        #
        # Do NOT "fix" this by switching to SelectorEventLoop: on Windows it
        # cannot select() on raw sockets (WSAEINVAL -> breaks the ICMP tunnel)
        # and it does not support asyncio subprocesses (NotImplementedError ->
        # breaks ssh). Both were verified on this machine.
        def _keep_loop_alive() -> None:
            loop.call_later(CONTROL_C_POLL_INTERVAL, _keep_loop_alive)

        loop.call_soon(_keep_loop_alive)

    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        registry.plugin_registry.notify("unload")
        tasks: list[asyncio.Task[Any]] = utils.AsyncTaskManager().running_tasks
        for task in tasks:
            print("Task %s can't auto exit" % task, file=sys.stderr)
        print("Process exit warmly.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
