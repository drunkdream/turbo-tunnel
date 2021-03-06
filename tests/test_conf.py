# -*- coding: utf-8 -*-

import socket
from unittest.mock import patch

import pytest

from turbo_tunnel import conf
from .util import conf_yaml


def test_tunnel():
    tunnel = conf.Tunnel(
        {
            "id": "test",
            "url": "socks://:1024",
            "default": True,
            "dependency": conf.Tunnel(
                {"id": "web", "url": "http://web-proxy.com:8080"}
            ),
        }
    )
    assert tunnel.id == "test"
    assert tunnel.url == "socks://:1024"
    assert tunnel.urls == ["http://web-proxy.com:8080", "socks://:1024"]
    assert tunnel.dependency.id == "web"
    assert tunnel.is_default() == True
    assert tunnel.is_blocked() == False


async def test_tunnel_rule():
    rule = conf.TunnelRule(
        {
            "id": "test",
            "priority": 100,
            "addr": "*.baidu.com;www.qq.com",
            "port": "80;443;5555-5566",
            "tunnel": "web",
        }
    )
    assert rule.id == "test"
    assert rule.priority == 100
    with patch("socket.getaddrinfo") as mocked_getaddrinfo:
        mocked_getaddrinfo.side_effect = lambda *args: [
            (socket.AddressFamily(2), ("1.1.1.1", 0))
        ]
        assert await rule.is_hit(("www.baidu.com", 80)) == True
        assert await rule.is_hit(("baidu.com", 80)) == False
        assert await rule.is_hit(("www.baidu.com", 801)) == False
        assert await rule.is_hit(("www.qq.com", 443)) == True
        assert await rule.is_hit(("www.qq.com", 5555)) == True
        assert await rule.is_hit(("www.qq.com", 5566)) == True
        assert await rule.is_hit(("www.qq.com", 5567)) == False
        assert await rule.is_hit(("wwww.qq.com", 5555)) == False


def test_conf_yaml():
    conf_file = "conf.yml"
    with open(conf_file, "w") as fp:
        fp.write(conf_yaml)
    config = conf.TunnelConfiguration(conf_file)
    rules = config.rules
    assert rules[0].id == "local"
    assert rules[1].id == "lan"
    assert rules[2].id == "wan"
