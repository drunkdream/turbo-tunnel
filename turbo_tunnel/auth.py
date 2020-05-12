# -*- coding: utf-8 -*-

'''Authentication
'''

import base64


def http_basic_auth(username, password):
    return base64.b64encode(('%s:%s' % (username, password)).encode('utf8')).decode()
