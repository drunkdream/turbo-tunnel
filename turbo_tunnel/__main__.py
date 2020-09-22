# -*- coding: utf-8 -*-
"""
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys

import tornado.ioloop

from . import conf
from . import registry
from . import route
from . import server
from . import utils


def main():
    parser = argparse.ArgumentParser(
        prog="turbo-tunnel", description="TurboTunnel cmdline tool."
    )
    parser.add_argument("-c", "--config", help="config yaml file path")
    parser.add_argument("-l", "--listen", help="listen url")
    parser.add_argument("-t", "--tunnel", action="append", help="tunnel url")
    parser.add_argument(
        "--log-level",
        help="log level, default is info",
        choices=("debug", "info", "warn", "error"),
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
    parser.add_argument("-p", "--plugin", help="load plugin", action="append")

    args = sys.argv[1:]
    if not args:
        parser.print_help()
        return 0

    args = parser.parse_args(args)

    if args.plugin:
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

    tunnel_server = None
    if args.config:
        if not os.path.exists(args.config):
            print("Config file %s not exist" % args.config, file=sys.stderr)
            return -1
        config = conf.TunnelConfiguration(args.config, args.auto_reload)
        router = route.TunnelRouter(config)
        tunnel_server = server.TunnelServer(config.listen_url, router)
    elif args.listen:
        tunnel = args.tunnel
        if not tunnel:
            tunnel = ["tcp://"]
        tunnel_server = server.TunnelServer(args.listen, tunnel)
    else:
        print("Argument --listen not specified", file=sys.stderr)
        return -1

    log_file = None
    if args.log_file:
        log_file = os.path.abspath(args.log_file)

    if sys.platform != "win32" and args.daemon:
        import daemon

        daemon.DaemonContext(stderr=open("error.txt", "w")).open()

    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s][%(levelname)s]%(message)s")
    handler.setFormatter(formatter)

    if args.log_level == "debug":
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

    tunnel_server.start()
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
