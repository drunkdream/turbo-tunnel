# -*- coding: utf-8 -*-

"""
"""

import asyncio
from unittest import mock

import tornado

from turbo_tunnel import chain
from turbo_tunnel import registry
from turbo_tunnel import tunnel
from turbo_tunnel import utils


class MockTunnel(tunnel.Tunnel):
    async def connect(self):
        return True

    async def read(self):
        return b"mock data"

    async def write(self, buffer):
        return True

    def close(self):
        pass


class MockStream(object):
    def __getattr__(self, attr):
        print("getattr", attr)
        raise AttributeError(attr)


async def test_tunnel_chain_with_url():
    registry.tunnel_registry.register("mock", MockTunnel)
    tunnel_urls = [utils.Url("mock://")]
    target_address = ("127.0.0.1", 80)

    future = asyncio.Future()
    future.set_result(MockStream())
    with mock.patch(
        "tornado.iostream.IOStream.connect", return_value=future
    ) as patcher:
        with chain.TunnelChain(tunnel_urls) as tunnel_chain:
            await tunnel_chain.create_tunnel(target_address)
            data = await tunnel_chain.tail.read()
            assert data == b"mock data"
