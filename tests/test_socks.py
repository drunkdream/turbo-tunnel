# -*- coding: utf-8 -*-


from turbo_tunnel import socks

def test_socks4_req():
    address = ('1.2.3.4', 1234)
    req = socks.Socks4RequestPacket(address)
    buffer = req.serialize()
    assert buffer == b'\x04\x01\x04\xd2\x01\x02\x03\x04\x00'


    req, buffer = socks.Socks4RequestPacket.unserialize_from(b'\x04\x01\x04\xd2\x01\x02\x03\x04\x00')
    assert req.address == address
    assert req.userid == ''
    assert buffer == b''

    req, buffer = socks.Socks4RequestPacket.unserialize_from(b'\x04\x01\x04\xd2\x01\x02\x03\x04qwert\x00')
    assert req.userid == 'qwert'
    assert buffer == b''

    req, buffer = socks.Socks4RequestPacket.unserialize_from(b'\x04\x01\x04\xd2\x01\x02\x03\x04qwert\x00test')
    assert req.userid == 'qwert'
    assert buffer == b'test'
