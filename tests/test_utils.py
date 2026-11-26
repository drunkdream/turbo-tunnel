# -*- coding: utf-8 -*-
"""
"""

import asyncio
import re
import socket
import time

from turbo_tunnel import utils


def test_url():
    url = utils.Url("http://www.qq.com:8080/test")
    assert url.protocol == "http"
    assert url.host == "www.qq.com"
    assert url.port == 8080
    assert url.address == ("www.qq.com", 8080)
    assert url.path == "/test"

    url = utils.Url("http://www.qq.com/test")
    assert url.port == 80

    url = utils.Url("https://www.qq.com/test")
    assert url.protocol == "https"
    assert url.port == 443
    url.protocol = "http"
    assert url.protocol == "http"
    url.params["p"] = 123
    assert url.params["p"] == 123
    assert url.query == "p=123"
    assert str(url) == "http://www.qq.com/test?p=123"

    url = utils.Url("http://www.qq.com:8080/test?p=1&p=2&p=3")
    assert url.params["p"] == ["1", "2", "3"]

    url = utils.Url("tcp://:8080/")
    assert url.host == "0.0.0.0"

    url = utils.Url("ssh://127.0.0.1")
    assert url.port == 22


async def test_async_task_manager():
    async def task():
        await asyncio.sleep(2)

    async_task_mgr = utils.AsyncTaskManager()
    async_task_mgr1 = utils.AsyncTaskManager()
    assert async_task_mgr == async_task_mgr1
    _task = task()
    async_task_mgr.start_task(_task)
    await asyncio.sleep(0.1)
    assert _task in async_task_mgr.running_tasks
    await asyncio.sleep(2)
    assert _task not in async_task_mgr.running_tasks


async def test_wait_for_tasks():
    async def task1():
        await asyncio.sleep(2)

    async def task2():
        await asyncio.sleep(3)

    tasks = [task1(), task2()]
    time0 = time.time()
    await utils.wait_for_tasks(tasks, return_when=asyncio.ALL_COMPLETED)
    assert time.time() - time0 > 2.99


async def test_wait_for_tasks2():
    async def task1():
        await asyncio.sleep(2)

    async def task2():
        await asyncio.sleep(3)

    async_task_mgr = utils.AsyncTaskManager()
    tasks = [task1(), task2()]
    await async_task_mgr.wait_for_tasks(tasks)
    assert tasks[0] not in async_task_mgr.running_tasks
    assert tasks[1] not in async_task_mgr.running_tasks


async def test_resolve_address():
    orig_getaddrinfo = socket.getaddrinfo
    orig_resolve = utils._resolve

    def hooked_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        raise RuntimeError("socket.getaddrinfo should not called")

    async def hooked_resolve(resolver, name_server, domain, queue):
        if domain == "www.github.com":
            await queue.put("1.1.1.1")

    socket.getaddrinfo = hooked_getaddrinfo
    utils._resolve = hooked_resolve

    async def resolve(domain):
        time0 = time.time()
        address = await utils.resolve_address((domain, 80))
        time1 = time.time()
        return address[0], time1 - time0

    try:
        result, cost = await resolve("www.github.com")
        assert re.match(r"[\d\.]+", result)
        assert cost < 1
        result, cost = await resolve("domainnotexist.com")
        assert result == "domainnotexist.com"
        assert cost < 6
    finally:
        utils._resolve = orig_resolve
        socket.getaddrinfo = orig_getaddrinfo


async def test_resolve_address_with_nameserver():
    orig_resolve = utils._resolve
    called_name_servers = []

    async def hooked_resolve(resolver, name_server, domain, queue):
        called_name_servers.append(name_server)
        if domain == "www.github-nameserver.com":
            await queue.put("1.1.1.1")

    utils._resolve = hooked_resolve
    # Avoid cache interference from other tests.
    utils.resolve_cache.clear()
    try:
        address = await utils.resolve_address(
            ("www.github-nameserver.com", 80), name_servers=["9.9.9.9"]
        )
        assert address[0] == "1.1.1.1"
        # The caller-specified nameserver must be used instead of the globals.
        assert called_name_servers and all(ns == "9.9.9.9" for ns in called_name_servers)
    finally:
        utils._resolve = orig_resolve
        utils.resolve_cache.clear()


async def test_resolve_address_with_protocol():
    orig_resolve = utils._resolve
    captured = []

    async def hooked_resolve(resolver, name_server, domain, queue):
        # name_server is an async_dns.core.Address carrying the protocol.
        captured.append(name_server)
        if domain == "www.github-proto.com":
            await queue.put("1.1.1.1")

    utils._resolve = hooked_resolve
    utils.resolve_cache.clear()
    try:
        address = await utils.resolve_address(
            ("www.github-proto.com", 80),
            name_servers=["tcp://9.9.9.9", "https://dns.google/dns-query"],
        )
        assert address[0] == "1.1.1.1"
        # Both specified nameservers must be attempted.
        assert len(captured) == 2
        assert str(captured[0]) == "tcp://9.9.9.9"
        # DoH server specified by hostname must be allowed (allow_domain).
        assert str(captured[1]) == "https://dns.google/dns-query"
    finally:
        utils._resolve = orig_resolve
        utils.resolve_cache.clear()


def test_checksum():
    data = b"12345678"
    result = utils.checksum(data)
    assert result == 0x2f2b
    data = b"123456780"
    result = utils.checksum(data)
    assert result == 0xff2a
