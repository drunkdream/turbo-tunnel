# -*- coding: utf-8 -*-

import asyncio
import random
import socket

import pytest

from turbo_tunnel import tunnel
from turbo_tunnel import utils
from turbo_tunnel import websocket

from .util import DemoTCPServer


@pytest.mark.asyncio
async def test_websocket_tunnel_server():
    port1 = random.randint(1000, 65000)
    listen_url = 'ws://127.0.0.1:%d/{addr}/{port}' % port1
    server1 = websocket.WebSocketTunnelServer(listen_url, ['tcp://'])
    server1.start()

    server2 = DemoTCPServer()
    port2 = random.randint(1000, 65000)
    server2.listen(port2)

    s = socket.socket()
    tun = tunnel.TCPTunnel(s, address=('127.0.0.1', port1))
    await tun.connect()

    ws_tun = websocket.WebSocketTunnel(tun, utils.Url(listen_url), ('127.0.0.1', port2))
    await ws_tun.connect()

    await ws_tun.write(b'Hello python\n')
    assert await ws_tun.read() == b'Hello python\n'

    assert server2.stream.closed() == False
    ws_tun.close()
    await asyncio.sleep(1)

    assert server2.stream.closed() == True


