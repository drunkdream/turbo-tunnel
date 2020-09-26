# -*- coding: utf-8 -*-

import random
import socket

import pytest

from turbo_tunnel import tunnel

from .util import DemoTCPServer, get_random_port


@pytest.mark.asyncio
async def test_tcp_tunnel():
    server = DemoTCPServer()
    port = get_random_port()
    server.listen(port)
    s = socket.socket()
    tunn = tunnel.TCPTunnel(s, address=('127.0.0.1', port))
    await tunn.connect()
    data = b'Hello world\n'
    await tunn.write(data)
    buffer = await tunn.read()
    assert buffer == data
    server.stop()
