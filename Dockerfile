FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/source:/source/memory:/source/agent_console:/tool:/tool/vendor \
    TZ=Asia/Shanghai \
    WECHAT_DIGEST_FONT=/source/assets/fonts/ResourceHanRoundedCN-Regular.ttf

RUN apt-get update \
    && apt-get install -y --no-install-recommends gdb binutils procps libx11-6 libxtst6 fontconfig fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY memory /source/memory
COPY agent_console /source/agent_console
COPY deployment /tool
COPY tools/wcdb-key-tool /opt/wcdb-key-tool
COPY assets/fonts /source/assets/fonts
COPY LICENSE THIRD_PARTY_NOTICES.md /source/

WORKDIR /source
USER 1000:1000
ENTRYPOINT ["python3"]
CMD ["/tool/digest.py", "--help"]
