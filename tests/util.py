# -*- coding: utf-8 -*-

'''
'''

import tornado.tcpserver


class DemoTCPServer(tornado.tcpserver.TCPServer):
    async def handle_stream(self, stream, address):
        buffer = await stream.read_until(b'\n')
        await stream.write(buffer)
        stream.close()