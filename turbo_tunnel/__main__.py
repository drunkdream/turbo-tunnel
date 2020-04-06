# -*- coding: utf-8 -*-
'''
'''

import argparse
import logging
import logging.handlers
import os
import sys

import tornado.ioloop

from . import conf
from . import route
from . import server
from . import utils


def main():
    parser = argparse.ArgumentParser(prog='turbo-tunnel',
                                     description='TurboTunnel cmdline tool.')
    parser.add_argument('--config', help='config yaml file path')
    parser.add_argument('--listen', help='listen url')
    parser.add_argument('--tunnel', action='append', help='tunnel url')
    parser.add_argument('--log-level', help='log level, default is info', choices=('debug', 'info', 'warn', 'error'), default='info')
    parser.add_argument('--log-file', help='log file save path')
    parser.add_argument('--retry', type=int, help='retry connect count', default=0)

    args = sys.argv[1:]
    if not args:
        parser.print_help()
        return 0

    args = parser.parse_args(args)

    tunnel_server = None
    if args.config:
        if not os.path.exists(args.config):
            print('Config file %s not exist' % args.config, file=sys.stderr)
            return -1
        config = conf.TunnelConfiguration(args.config)
        router = route.TunnelRouter(config)
        tunnel_server = server.TunnelServer(config.listen_url, router)
    elif args.listen:
        if len(args.tunnel) == 0:
            print('Argument --tunnel not specified', file=sys.stderr)
            return -1

        tunnel_server = server.TunnelServer(args.listen, args.tunnel)
    else:
        print('Argument --listen not specified', file=sys.stderr)
        return -1

    if args.log_level == 'debug':
        utils.logger.setLevel(logging.DEBUG)
    elif args.log_level == 'info':
        utils.logger.setLevel(logging.INFO)
    elif args.log_level == 'warn':
        utils.logger.setLevel(logging.WARN)
    elif args.log_level == 'error':
        utils.logger.setLevel(logging.ERROR)
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s][%(levelname)s]%(message)s")
    handler.setFormatter(formatter)
    utils.logger.addHandler(handler)
    if args.log_file:
        handler = logging.handlers.RotatingFileHandler(args.log_file, maxBytes=10*1024*1024, backupCount=4)
        formatter = logging.Formatter("[%(asctime)s][%(levelname)s][%(filename)s][%(lineno)d]%(message)s")
        handler.setFormatter(formatter)
        utils.logger.addHandler(handler)

    if args.retry:
        server.TunnelServer.retry_count = args.retry

    tunnel_server.start()
    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        print('Process exit warmly.')


if __name__ == '__main__':
    sys.exit(main())
