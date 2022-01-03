# -*- coding: utf-8 -*-

from turbo_tunnel import conf
from turbo_tunnel import route

from .util import conf_yaml


async def test_route():
    conf_file = "conf.yml"
    with open(conf_file, "w") as fp:
        fp.write(conf_yaml)
    config = conf.TunnelConfiguration(conf_file)
    await config.load()
    router = route.TunnelRouter(config)
    rule, tunnel = await router.select(("127.0.0.1", 80))
    assert rule == "local"
    assert tunnel.url == "tcp://"

    rule, tunnel = await router.select(("www.lan.com", 80))
    assert rule == "lan"
    assert tunnel.url == "http://127.0.0.1:8888"

    rule, tunnel = await router.select(("www.wan.com", 1234))
    assert rule == "block"
    assert tunnel == None
