# -*- coding: utf-8 -*-

'''
'''

import asyncio
import random
import socket

import pytest
import tornado

from turbo_tunnel import utils


def test_url():
    url = utils.Url('http://www.qq.com:8080/test')
    assert url.protocol == 'http'
    assert url.host == 'www.qq.com'
    assert url.port == 8080
    assert url.address == ('www.qq.com', 8080)
    assert url.path == '/test'

    url = utils.Url('http://www.qq.com/test')
    assert url.port == 80

    url = utils.Url('https://www.qq.com/test')
    assert url.protocol == 'https'
    assert url.port == 443

    url = utils.Url('tcp://:8080/')
    assert url.host == '0.0.0.0'


@pytest.mark.asyncio
async def test_tcp_stream():
    class DemoTCPServer(tornado.tcpserver.TCPServer):
        async def handle_stream(self, stream, address):
            buffer = await stream.read_until(b'\n')
            await stream.write(buffer)
            stream.close()
    server = DemoTCPServer()
    port = random.randint(1000, 65000)
    server.listen(port)
    s = socket.socket()
    stream = utils.TCPStream(s)
    await stream.connect(('127.0.0.1', port))
    data = b'Hello world\n'
    await stream.write(data)
    buffer = await stream.read()
    assert buffer == data
    server.stop()

