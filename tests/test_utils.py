# -*- coding: utf-8 -*-
"""
"""

import asyncio
import random
import socket

import tornado

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
    curr_tasks = len(async_task_mgr.running_tasks)
    async_task_mgr.start_task(task())
    await asyncio.sleep(0.1)
    assert len(async_task_mgr.running_tasks) == curr_tasks + 1
    await asyncio.sleep(2)
    assert len(async_task_mgr.running_tasks) == curr_tasks


async def test_wait_for_tasks():
    async def task1():
        await asyncio.sleep(2)

    async def task2():
        await asyncio.sleep(3)

    async_task_mgr = utils.AsyncTaskManager()
    curr_tasks = len(async_task_mgr.running_tasks)
    await async_task_mgr.wait_for_tasks([task1(), task2()])
    assert len(async_task_mgr.running_tasks) == curr_tasks
