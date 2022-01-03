# -*- coding: utf-8 -*-

"""
"""

import random
import socket

import tornado.tcpserver


class DemoTCPServer(tornado.tcpserver.TCPServer):
    def __init__(self, autoclose=False, ssl_options=None):
        super(DemoTCPServer, self).__init__(ssl_options=ssl_options)
        self._autoclose = autoclose
        self._stream = None

    @property
    def stream(self):
        return self._stream

    def kick_client(self):
        if self._stream:
            self._stream.close()
            self._stream = None

    async def handle_stream(self, stream, address):
        if not self._stream:
            self._stream = stream
        buffer = await stream.read_until(b"\n")
        await stream.write(buffer)
        if self._autoclose:
            stream.close()


def start_demo_http_server(port):
    print("Start demo http server on port %d" % port)

    class HTTPServerHandler(tornado.web.RequestHandler):
        async def get(self):
            self.write(b"Hello HTTP!")

    handlers = [
        (r".*", HTTPServerHandler),
    ]
    app = tornado.web.Application(handlers)
    app.listen(port)


conf_yaml = r"""
version: 1.0

listen: 
  - http://127.0.0.1:6666
  - socks5://127.0.0.1:7777

include: ./second.yml

tunnels:
  - id: direct
    url: tcp://
    default: true

  - id: block
    url: block://

  - id: web
    url: http://127.0.0.1:8888

rules:
  - id: local
    priority: 100
    addr: 127.0.0.1
    tunnel: direct
  
  - id: lan
    priority: 99
    addr: "*.lan.com"
    port: 80;443
    tunnel: web

  - id: wan
    priority: 98
    addr: "*"
    port: 1-65535
    tunnel: block
"""

conf_yaml2 = """
version: 1.0

tunnels:
  - id: test
    url: http://127.0.0.1:7777

rules:
  - id: test
    priority: 90
    addr: "*.test.com"
    port: 1-65535
    tunnel: block

"""
def get_random_port():
    while True:
        port = random.randint(10000, 65000)
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
        except:
            continue
        else:
            s.close()
            return port
