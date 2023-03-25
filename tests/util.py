# -*- coding: utf-8 -*-

"""
"""

import os
import random
import socket
import ssl
import struct

import tornado.tcpserver
import tornado.websocket


class MockK8SExecWebSocketHandler(tornado.websocket.WebSocketHandler):
    async def open(self, *args, **kwargs):
        print("[%s] Connection opened" % self.__class__.__name__)
        await self.write_message(b"\x02[OKAY]\n", True)

    async def on_message(self, message):
        if message[0] != 0:
            raise RuntimeError("Invalid message: %r" % message)
        message = message[1:]
        print("[%s] Recevied message: %r" % (self.__class__.__name__, message))
        await self.write_message(b"\x01" + message, True)


class MockK8SPortForwardWebSocketHandler(tornado.websocket.WebSocketHandler):
    async def open(self, *args, **kwargs):
        print("[%s] Connection opened" % self.__class__.__name__)
        port = int(self.request.query_arguments["ports"][0].decode())
        await self.write_message(b"\x00" + struct.pack("H", port), True)
        await self.write_message(b"\x01" + struct.pack("H", port), True)

    async def on_message(self, message):
        if message[0] != 0:
            raise RuntimeError("Invalid message: %r" % message)
        message = message[1:]
        print("[%s] Recevied message: %r" % (self.__class__.__name__, message))
        await self.write_message(b"\x00" + message, True)


def start_mock_k8s_websocket_server(port):
    res_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "res")
    handlers = [
        ("/api/v1/namespaces/default/pods/pod-1/exec", MockK8SExecWebSocketHandler),
        (
            "/api/v1/namespaces/default/pods/pod-1/portforward",
            MockK8SPortForwardWebSocketHandler,
        ),
    ]
    app = tornado.web.Application(handlers)
    app.listen(
        port,
        "127.0.0.1",
        ssl_options={
            "certfile": os.path.join(res_dir, "server.crt"),
            "keyfile": os.path.join(res_dir, "server.key"),
            "cert_reqs": ssl.CERT_REQUIRED,
            "ca_certs": os.path.join(res_dir, "ca.crt"),
        },
    )


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
