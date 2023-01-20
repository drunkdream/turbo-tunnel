# -*- coding: utf-8 -*-
"""
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import re
import sys
import traceback

import tornado.ioloop

from . import BANNER
from . import VERSION
from . import conf
from . import registry
from . import route
from . import server
from . import utils


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

    def __init__(self, format):
        super(HighlightFormatter, self).__init__(format)
        self._format = format.replace(
            "%(asctime)s", self.grey + "%(asctime)s" + self.reset
        )
        self.FORMATS = {
            logging.DEBUG: self.cyan,
            logging.INFO: self.green,
            logging.WARNING: self.yellow,
            logging.ERROR: self.red,
            logging.CRITICAL: self.bold_red,
        }

    def format(self, record):
        log_fmt = self._format.replace(
            "%(levelname)s",
            self.FORMATS.get(record.levelno) + "%(levelname)s" + self.reset,
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


def handle_args(args):

    if args.plugin:
        registry.plugin_registry.enable()
        for plugin in args.plugin:
            for module in ("turbo_tunnel.plugins.%s" % plugin, plugin):
                try:
                    __import__(module)
                except ImportError:
                    pass
                else:
                    break
            else:
                utils.logger.error("Load plugin %s failed" % plugin)

    tunnel_servers = []
    if args.config:
        if not os.path.exists(args.config):
            print("Config file %s not exist" % args.config, file=sys.stderr)
            return -1
        config = conf.TunnelConfiguration(args.config, auto_reload=args.auto_reload)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(config.load())
        router = route.TunnelRouter(config)
        for listen_url in config.listen_urls:
            tunnel_server = server.TunnelServer(listen_url, router)
            tunnel_servers.append(tunnel_server)
    elif args.listen:
        tunnel = args.tunnel
        if not tunnel:
            tunnel = ["tcp://"]
        tunnel_server = server.TunnelServer(args.listen, tunnel)
        tunnel_servers.append(tunnel_server)
    else:
        print("Argument --listen not specified", file=sys.stderr)
        return -1

    log_file = None
    if args.log_file:
        log_file = os.path.abspath(args.log_file)

    handler = logging.StreamHandler()
    fmt = "[%(asctime)s][%(levelname)s]%(message)s"
    enable_color_output = not args.no_color
    if enable_color_output and sys.platform == "win32":
        enable_color_output = utils.enable_native_ansi()
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

    utils.logger.propagate = 0
    utils.logger.addHandler(handler)

    if log_file:
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=4
        )
        formatter = logging.Formatter(
            "[%(asctime)s][%(levelname)s][%(filename)s][%(lineno)d]%(message)s"
        )
        handler.setFormatter(formatter)
        utils.logger.addHandler(handler)

    if args.retry:
        server.TunnelServer.retry_count = args.retry

    if sys.platform == "win32" and sys.version_info[1] >= 8:
        # on Windows, the default asyncio event loop is ProactorEventLoop from python3.8
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)

    for tunnel_server in tunnel_servers:
        tunnel_server.start()


def main():
    print("\x1b[0;36m%s \x1b[0;32m v%s\x1b[0m\n" % (BANNER.rstrip(), VERSION))
    parser = argparse.ArgumentParser(
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

    args = sys.argv[1:]
    if not args:
        parser.print_help()
        return 0

    args = parser.parse_args(args)

    if args.version:
        print("v%s" % VERSION)
        return 0

    if sys.platform != "win32" and args.daemon:
        import daemon

        # fork must be called before create event loop
        daemon.DaemonContext(stderr=open("error.txt", "w")).open()
    elif args.daemon:
        utils.win32_daemon()
        return 0

    handle_args(args)

    def handle_exception(loop, context):
        registry.plugin_registry.notify("unload")
        print("Exception caught:\n", file=sys.stderr)
        message = context["message"]
        exp = context.get("exception")
        if exp:
            message = "".join(
                traceback.format_exception(
                    etype=type(exp), value=exp, tb=exp.__traceback__
                )
            )
        print(message, file=sys.stderr)
        if args.stop_on_error:
            loop.stop()

    loop = asyncio.get_event_loop()
    loop.set_exception_handler(handle_exception)

    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        registry.plugin_registry.notify("unload")
        tasks = utils.AsyncTaskManager().running_tasks
        for task in tasks:
            print("Task %s can't auto exit" % task, file=sys.stderr)
        print("Process exit warmly.")


if __name__ == "__main__":
    sys.exit(main())
