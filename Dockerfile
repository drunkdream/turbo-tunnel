FROM --platform=$TARGETPLATFORM python:3.11-slim-bookworm
ARG MIRROR=mirrors.tencent.com
# ARG MIRROR=mirrors.aliyun.com

WORKDIR /app

ADD . ./

RUN set -ex \
    && sed "s+//.*debian.org+//${MIRROR}+g; /^#/d" -i /etc/apt/sources.list.d/debian.sources \
    && apt --allow-releaseinfo-change -y update \
    # Ensure that System SSL; Add net-tools
    && apt install -y --no-install-recommends ca-certificates net-tools \
    && update-ca-certificates \
    \
    && pip3 install -U virtualenv pip -i https://${MIRROR}/pypi/simple/ \
    && pip3 install -e . -i https://${MIRROR}/pypi/simple/

ENTRYPOINT ["turbo-tunnel"]

EXPOSE 80
