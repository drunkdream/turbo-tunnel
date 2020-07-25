# -*- coding: utf-8 -*-

'''
'''

import tornado.tcpserver


class DemoTCPServer(tornado.tcpserver.TCPServer):

    def __init__(self, autoclose=False):
        super(DemoTCPServer, self).__init__()
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
        buffer = await stream.read_until(b'\n')
        await stream.write(buffer)
        if self._autoclose:
            stream.close()

conf_yaml = r'''
version: 1.0

listen: https://127.0.0.1:6666

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
'''