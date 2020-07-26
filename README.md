
```
 _____            _          _____                        _ 
/__   \_   _ _ __| |__   ___/__   \_   _ _ __  _ __   ___| |
  / /\/ | | | '__| '_ \ / _ \ / /\/ | | | '_ \| '_ \ / _ \ |
 / /  | |_| | |  | |_) | (_) / /  | |_| | | | | | | |  __/ |
 \/    \__,_|_|  |_.__/ \___/\/    \__,_|_| |_|_| |_|\___|_| 
```

[![PyPi version](https://img.shields.io/pypi/v/turbo-tunnel.svg)](https://pypi.python.org/pypi/turbo-tunnel/) 
![Unittest](https://github.com/drunkdream/turbo-tunnel/workflows/Unittest/badge.svg)
[![codecov.io](https://codecov.io/github/drunkdream/turbo-tunnel/coverage.svg?branch=master)](https://codecov.io/github/drunkdream/turbo-tunnel)

## 环境要求

* 操作系统： `Windows`、`Linux`、`MacOS`
* Python: `>=3.5`

## 主要功能

* 提供访问各种不同代理服务的能力
* 目前支持的代理服务类型包括：`https`、`socks4`、`websocket`、`ssh`
* 目前可以创建的代理服务类型有：`https`、`socks4`、`websocket`
* 还支持端口映射等非代理服务
* 支持通过配置文件的方式，指定不同的策略，允许使用不同的代理服务访问不同的目标服务

## 使用方法

### 安装方法

```bash
$ pip3 install turbo-tunnel
```

### 端口映射

```bash
$ turbo-tunnel -l tcp://127.0.0.1:8080 -t tcp://www.qq.com:443
```

该命令可以将`www.qq.com`的`443`端口到本地的`8080`端口。

```bash
$ turbo-tunnel -l tcp://127.0.0.1:8080 -t http://web-proxy.com:8080 -t tcp://www.qq.com:443
```

该命令会通过`https`类型的代理服务`web-proxy.com:8080`，将`www.qq.com`的`443`端口到本地的`8080`端口。

如果不写`127.0.0.1`，则默认是`0.0.0.0`。

### 创建代理服务

使用`-l`或`--listen`参数，可以创建代理服务。

* 创建https代理服务

```bash
$ turbo-tunnel -l http://username:password@:8080
```

`username`和`password`是用于鉴权的用户名和密码，不指定则不对客户端鉴权；如果包含特殊字符需要进行`urlencode`。



* 创建socks4代理服务

```bash
$ turbo-tunnel -l socks4://userid@127.0.0.1:1080
```

`socks4://`也可以写成`socks://`，表示默认使用`socks4`协议。

`userid`参数用于鉴权，不指定则无需鉴权。

* 创建WebSocket代理服务

```bash
$ turbo-tunnel -l ws://username:password@127.0.0.1/{addr}/{port}
```

`{addr}`和`{port}`在这里是变量占位符，用于指示`目标地址`和`目标端口`参数。这是因为`WebSocket`协议本身并不是一种代理协议，因此需要使用特定的字段来描述这些信息。运行过程中，当需要访问`1.1.1.1:8888`服务时，客户端会动态生成请求路径：`/1.1.1.1/8888`。

这里可以根据自己的需要改成不同的格式，例如：`/proxy-{addr}-{port}`。但是，这里设置的格式需要与客户端指定的url格式一致。

目前没有提供`wss`协议，用户如果需要的话可以配合`nginx`搭建。

### 指定访问通道

通过`-t`或`--tunnel`参数，可以指定访问目标服务器的通道，默认是直连。

* 配置单个通道

```bash
$ turbo-tunnel -l http://127.0.0.1:8080 -t wss://username:password@proxy.com/{addr}/{port}
```

所有访问本地https代理服务的请求都会通过`WebSocket`代理服务进行转发。

> 这种方法可以用作代理协议格式的转换。

* 配置多个通道

```bash
$ turbo-tunnel -l http://127.0.0.1:8080 -t socks://10.1.1.1:1080 -t ssh://username:password@10.2.2.2:22
```

配置多个通道时，会进行代理协议的嵌套，从而依次穿越所有通道，进行目标服务的访问。

### 设置全局透明代理

创建代理服务后，我们希望程序访问网络时能自动使用代理去访问。此时可以通过配置全局透明代理的方式来解决。

* Windows & MacOS 可以安装`Proxifier`工具，该工具可以配置各种访问策略，访问不同服务时使用不同的代理服务器。

* Liunx 可以使用`proxychains`工具，该工具类似于`Proxifier`，但是不能配置策略，而且需要在启动目标应用时在命令前增加`proxychains`前缀。


## 高级用法

### 使用配置文件

`-c`或`--config`参数可以指定一个`yaml`格式的配置文件，可以配置服务参数访问策略。下面是一个配置文件的例子：

```yaml
version: 1.0

listen: http://127.0.0.1:6666 # 配置监听地址

tunnels:
  - id: direct
    url: tcp://
    default: true # 默认使用直连策略
  
  - id: block
    url: block:// # 禁止访问策略

  - id: web
    url: http://web-proxy.com:8080
  
  - id: private
    url: wss://test:mypassword@ws-proxy.com/proxy/{addr}/{port}
    dependency: web # 依赖web代理

rules:
  - id: local
    priority: 100 # 优先级，1-100，策略冲突时会选择优先级最高的策略
    addr: 127.0.0.1
    tunnel: direct
  
  - id: internal
    priority: 99
    addr: "192.168.*"
    port: 1-65535
    tunnel: direct
  
  - id: public
    priority: 90
    addr: "*"
    port: 80;443;8080
    tunnel: web # 访问外网使用web通道
  
  - id: private
    priority: 95
    addr: "*.private.com"
    port: 1-65535
    tunnel: private

  - id: test
    priority: 90
    addr: "*.baidu.com"
    port: 80;443
    tunnel: block # 不允许访问
```

### 扩展插件

turbo-tunnel允许用户开发自己的插件，以支持新的代理服务或通道类型，还可以在运行过程中执行特殊操作，例如：动态修改收发的数据。

目前自带的插件有：

* terminal: 展示运行过程中的连接情况

```bash
$ turbo-tunnel -l http://127.0.0.1:8080 -p terminal -p xxx
```

通过`-p`或`--plugin`可以指定`1-N`个插件，插件加载顺序由启动命令行中`-p`参数的顺序决定。

