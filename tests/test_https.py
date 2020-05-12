# -*- coding: utf-8 -*-

'''
'''

import asyncio
import random
import socket

import pytest

from turbo_tunnel import https
from turbo_tunnel import tunnel
from turbo_tunnel import utils

from .util import DemoTCPServer


@pytest.mark.asyncio
async def test_https_tunnel_server():
    port1 = random.randint(1000, 65000)
    listen_url = 'http://127.0.0.1:%d' % port1
    server1 = https.HTTPSTunnelServer(listen_url, ['tcp://'])
    server1.start()

    server2 = DemoTCPServer()
    port2 = random.randint(1000, 65000)
    server2.listen(port2)

    s = socket.socket()
    tun = tunnel.TCPTunnel(s, address=('127.0.0.1', port1))
    await tun.connect()

    await tun.write(b'CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n' % port2)
    response = await tun.read()
    assert response.endswith(b'\r\n\r\n')
    first_line = response.splitlines()[0]
    items = first_line.split()
    assert int(items[1]) == 200

    await tun.write(b'Hello python\n')
    assert await tun.read() == b'Hello python\n'

    assert server2.stream.closed() == False
    tun.close()
    await asyncio.sleep(1)

    assert server2.stream.closed() == True


@pytest.mark.asyncio
async def test_https_tunnel_server_auto_close():
    port1 = random.randint(1000, 65000)
    listen_url = 'http://127.0.0.1:%d' % port1
    server1 = https.HTTPSTunnelServer(listen_url, ['tcp://'])
    server1.start()

    server2 = DemoTCPServer()
    port2 = random.randint(1000, 65000)
    server2.listen(port2)

    s = socket.socket()
    tun = tunnel.TCPTunnel(s, address=('127.0.0.1', port1))
    await tun.connect()

    await tun.write(b'CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n' % port2)
    response = await tun.read()
    assert response.endswith(b'\r\n\r\n')
    first_line = response.splitlines()[0]
    items = first_line.split()
    assert int(items[1]) == 200

    server2.kick_client()
    await asyncio.sleep(1)
    assert server2.stream == None
    assert tun.closed() == True

