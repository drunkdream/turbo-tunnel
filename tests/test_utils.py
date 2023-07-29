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

    def hooked_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        raise RuntimeError("socket.getaddrinfo should not called")

    socket.getaddrinfo = hooked_getaddrinfo

    async def resolve(domain):
        time0 = time.time()
        address = await utils.resolve_address((domain, 80))
        time1 = time.time()
        return address[0], time1 - time0

    result, cost = await resolve("www.github.com")
    socket.getaddrinfo = orig_getaddrinfo
    assert re.match(r"[\d\.]+", result)
    assert cost < 1
    socket.getaddrinfo = hooked_getaddrinfo
    result, cost = await resolve("domainnotexist.com")
    socket.getaddrinfo = orig_getaddrinfo
    assert result == "domainnotexist.com"
    assert cost < 6


def test_checksum():
    data = b"12345678"
    result = utils.checksum(data)
    assert result == 0x2f2b
    data = b"123456780"
    result = utils.checksum(data)
    assert result == 0xf72a
