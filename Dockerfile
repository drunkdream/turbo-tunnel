FROM python:3.7

ENV DEBIAN_MIRROR=https://mirrors.tencent.com
ENV PYPI_URL=https://mirrors.tencent.com/pypi/simple/

ADD turbo_tunnel /app/turbo_tunnel
ADD setup.py /app/setup.py
ADD requirements.txt /app/requirements.txt
ADD extra_requirements.txt /app/extra_requirements.txt
ADD README.md /app/README.md

RUN sed -i "s#http://deb.debian.org#$DEBIAN_MIRROR#g" /etc/apt/sources.list \
    && apt update && apt install net-tools \
    && pip3 install virtualenv -i $PYPI_URL \
    && pip3 install -e /app -i $PYPI_URL

ENTRYPOINT ["turbo-tunnel"]

EXPOSE 80
