# -*- coding: utf-8 -*-

import os
import random
import socket
import ssl

from turbo_tunnel import tunnel, utils

from .util import DemoTCPServer, get_random_port


async def test_tcp_tunnel():
    server = DemoTCPServer()
    port = get_random_port()
    server.listen(port)
    s = socket.socket()
    tunn = tunnel.TCPTunnel(s, address=("127.0.0.1", port))
    await tunn.connect()
    data = b"Hello world\n"
    await tunn.write(data)
    buffer = await tunn.read()
    assert buffer == data
    server.stop()


async def test_ssl_tunnel():
    res_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "res")
    ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_ctx.load_cert_chain(
        os.path.join(res_dir, "server.crt"), os.path.join(res_dir, "server.key")
    )
    server = DemoTCPServer(ssl_options=ssl_ctx)
    port = get_random_port()
    server.listen(port)
    s = socket.socket()
    tunn = tunnel.TCPTunnel(s, address=("127.0.0.1", port))
    await tunn.connect()
    tunn = tunnel.SSLTunnel(tunn, utils.Url("ssl://127.0.0.1/?verify_ssl=false"))
    await tunn.connect()
    data = b"Hello world\n"
    await tunn.write(data)
    buffer = await tunn.read()
    assert buffer == data
    server.stop()


async def test_fork_tunnel():
    server = DemoTCPServer()
    port = get_random_port()
    server.listen(port)
    s = socket.socket()
    tunn = tunnel.TCPTunnel(s, address=("127.0.0.1", port))
    fork_tunn = await tunn.fork()
    data = b"Hello world\n"
    await fork_tunn.write(data)
    buffer = await fork_tunn.read()
    assert buffer == data
    server.stop()
