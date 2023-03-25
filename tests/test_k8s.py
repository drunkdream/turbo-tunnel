# -*- coding: utf-8 -*-

import os
import socket

from turbo_tunnel import k8s
from turbo_tunnel import tunnel
from turbo_tunnel import utils
from .util import DemoTCPServer, get_random_port, start_mock_k8s_websocket_server


async def test_k8s_port_forward_tunnel():
    res_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "res")
    port1 = get_random_port()
    start_mock_k8s_websocket_server(port1)

    port2 = get_random_port()
    server = DemoTCPServer()
    server.listen(port2)

    url = "k8s://localhost:%d/?kubeconfig=%s" % (
        port1,
        os.path.join(res_dir, "kubeconfig"),
    )
    s = socket.socket()
    tun = tunnel.TCPTunnel(s, address=("127.0.0.1", port1))
    await tun.connect()

    tun = k8s.KubernetesPortForwardTunnel(tun, utils.Url(url), ("pod-1", port2))
    await tun.connect()
    await tun.write(b"Hello python\n")
    assert await tun.read() == b"Hello python\n"
    tun.close()


async def test_k8s_exec_tunnel():
    res_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "res")
    port1 = get_random_port()
    start_mock_k8s_websocket_server(port1)

    port2 = get_random_port()
    server = DemoTCPServer()
    server.listen(port2)

    url_list = [
        "k8s+process://localhost:%d/bin/telnet?pod=pod-1&client_cert=%s&client_key=%s&ca_cert=%s"
        % (
            port1,
            os.path.join(res_dir, "client.crt"),
            os.path.join(res_dir, "client.key"),
            os.path.join(res_dir, "ca.crt"),
        ),
        "k8s+process://localhost:%d/bin/telnet?pod=pod-1&kubeconfig=%s"
        % (port1, os.path.join(res_dir, "kubeconfig")),
    ]

    for url in url_list:
        s = socket.socket()
        tun = tunnel.TCPTunnel(s, address=("127.0.0.1", port1))
        await tun.connect()

        tun = k8s.KubernetesExecTunnel(tun, utils.Url(url), ("127.0.0.1", port2))
        await tun.connect()
        await tun.write(b"Hello python\n")
        assert await tun.read() == b"Hello python\n"
        tun.close()
