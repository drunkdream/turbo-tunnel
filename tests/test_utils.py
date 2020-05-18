# -*- coding: utf-8 -*-
'''
'''

import asyncio
import random
import socket

import pytest
import tornado

from turbo_tunnel import utils


def test_url():
    url = utils.Url('http://www.qq.com:8080/test')
    assert url.protocol == 'http'
    assert url.host == 'www.qq.com'
    assert url.port == 8080
    assert url.address == ('www.qq.com', 8080)
    assert url.path == '/test'

    url = utils.Url('http://www.qq.com/test')
    assert url.port == 80

    url = utils.Url('https://www.qq.com/test')
    assert url.protocol == 'https'
    assert url.port == 443

    url = utils.Url('tcp://:8080/')
    assert url.host == '0.0.0.0'

    url = utils.Url('ssh://127.0.0.1')
    assert url.port == 22


@pytest.mark.asyncio
async def test_async_task_manager():
    async def task():
        await asyncio.sleep(2)
    async_task_mgr = utils.AsyncTaskManager()
    async_task_mgr1 = utils.AsyncTaskManager()
    assert async_task_mgr == async_task_mgr1
    async_task_mgr.start_task(task())
    await asyncio.sleep(0.1)
    assert len(async_task_mgr.running_tasks) == 1
    await asyncio.sleep(2)
    assert len(async_task_mgr.running_tasks) == 0


@pytest.mark.asyncio
async def test_wait_for_tasks():
    async def task1():
        await asyncio.sleep(2)
    async def task2():
        await asyncio.sleep(3)
    
    async_task_mgr = utils.AsyncTaskManager()
    await async_task_mgr.wait_for_tasks([task1(), task2()])
    assert len(async_task_mgr.running_tasks) == 0
