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