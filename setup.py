# -*- coding: utf-8 -*-

import setuptools

import turbo_tunnel

with open('README.md') as fp:
    README = fp.read()


with open('requirements.txt') as fp:
    text = fp.read()
    REQUIREMENTS = text.split('\n')


setuptools.setup(
    author="drunkdream",
    author_email="drunkdream@qq.com",
    name='turbo-tunnel',
    license="MIT",
    description='Fast tcp/https/websocket/socks4/ssh tunnel serving as unified proxy server.',
    version=turbo_tunnel.VERSION,
    long_description=README,
    long_description_content_type="text/markdown",
    url='https://github.com/drunkdream/turbo-tunnel',
    packages=setuptools.find_packages(),
    python_requires=">=3.5",
    install_requires=REQUIREMENTS,
    classifiers=[
        # Trove classifiers
        # (https://pypi.python.org/pypi?%3Aaction=list_classifiers)
        'Development Status :: 3 - Alpha',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Intended Audience :: Developers',
    ],
    entry_points={
        'console_scripts': [
            'turbo-tunnel = turbo_tunnel.__main__:main',
        ],
    }
)
