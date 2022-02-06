# -*- coding: utf-8 -*-

"""
"""

import asyncio
import random
import socket
import time

from turbo_tunnel import https
from turbo_tunnel import tunnel
from turbo_tunnel import utils

from .util import DemoTCPServer, get_random_port, start_demo_http_server


async def test_http_proxy():
    port1 = get_random_port()
    listen_url = "http://127.0.0.1:%d" % port1
    server1 = https.HTTPSTunnelServer(listen_url, ["tcp://"])
    server1.start()

    port2 = get_random_port()
    start_demo_http_server(port2)

    s = socket.socket()
    tun = tunnel.TCPTunnel(s, address=("127.0.0.1", port1))
    await tun.connect()

    await tun.write(
        b"GET http://127.0.0.1:%d/ HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
        % (port2, port1)
    )
    rsp = await tun.read()
    pos = rsp.find(b"\r\n\r\n")
    assert pos > 0
    rsp = rsp[pos + 4 :]
    assert rsp == b"b\r\nHello HTTP!\r\n0\r\n\r\n"


async def test_https_tunnel_server():
    port1 = get_random_port()
    listen_url = "http://127.0.0.1:%d" % port1
    server1 = https.HTTPSTunnelServer(listen_url, ["tcp://"])
    server1.start()

    server2 = DemoTCPServer()
    port2 = get_random_port()
    server2.listen(port2)

    s = socket.socket()
    tun = tunnel.TCPTunnel(s, address=("127.0.0.1", port1))
    await tun.connect()

    await tun.write(b"CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % port2)
    response = await tun.read()
    assert response.endswith(b"\r\n\r\n")
    first_line = response.splitlines()[0]
    items = first_line.split()
    assert int(items[1]) == 200

    await tun.write(b"Hello python\n")
    assert await tun.read() == b"Hello python\n"

    assert server2.stream.closed() == False
    tun.close()
    await asyncio.sleep(1)

    assert server2.stream.closed() == True


async def test_https_tunnel_server_auto_close():
    port1 = get_random_port()
    listen_url = "http://127.0.0.1:%d" % port1
    server1 = https.HTTPSTunnelServer(listen_url, ["tcp://"])
    server1.start()

    server2 = DemoTCPServer()
    port2 = get_random_port()
    server2.listen(port2)

    s = socket.socket()
    tun = tunnel.TCPTunnel(s, address=("127.0.0.1", port1))
    await tun.connect()

    await tun.write(b"CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % port2)
    response = await tun.read()
    assert response.endswith(b"\r\n\r\n")
    first_line = response.splitlines()[0]
    items = first_line.split()
    assert int(items[1]) == 200

    server2.kick_client()
    await asyncio.sleep(1)
    assert server2.stream == None
    assert tun.closed() == True


async def test_http_proxy_with_unknown_domain():
    port1 = get_random_port()
    listen_url = "http://127.0.0.1:%d" % port1
    server1 = https.HTTPSTunnelServer(listen_url, ["tcp://"])
    server1.start()

    port2 = get_random_port()
    start_demo_http_server(port2)

    async def async_task():
        s = socket.socket()
        tun = tunnel.TCPTunnel(s, address=("127.0.0.1", port1))
        await tun.connect()
        domain = "domainnotexist.com"
        await tun.write(
            ("GET http://%s/ HTTP/1.1\r\nHost: %s\r\n\r\n" % (domain, domain)).encode()
        )
        rsp = await tun.read()
        print(rsp)

    utils.safe_ensure_future(async_task())

    time0 = time.time()
    await asyncio.sleep(1)

    s = socket.socket()
    tun = tunnel.TCPTunnel(s, address=("127.0.0.1", port1))
    await tun.connect()

    await tun.write(
        b"GET http://127.0.0.1:%d/ HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
        % (port2, port1)
    )
    rsp = await tun.read()
    time1 = time.time()
    pos = rsp.find(b"\r\n\r\n")
    assert pos > 0
    rsp = rsp[pos + 4 :]
    assert rsp == b"b\r\nHello HTTP!\r\n0\r\n\r\n"
    assert time1 - time0 < 2
