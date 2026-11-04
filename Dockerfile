FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/ocr-deps:/source:/source/memory:/source/agent_console:/tool:/tool/vendor \
    TZ=Asia/Shanghai \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache \
    WECHAT_DIGEST_FONT=/source/assets/fonts/ResourceHanRoundedCN-Regular.ttf

RUN apt-get update \
    && apt-get install -y --no-install-recommends gdb binutils procps libx11-6 libxtst6 libgl1 libglib2.0-0 fontconfig fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
RUN mkdir -p /opt/tiktoken-cache \
    && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')" \
    && chmod -R a-w /opt/tiktoken-cache

COPY memory /source/memory
COPY agent_console /source/agent_console
COPY deployment /tool
ARG WITH_OCR=0
COPY scripts/install-ocr.py /opt/install-ocr.py
RUN if [ "$WITH_OCR" = 1 ]; then python /opt/install-ocr.py --lock /tool/ocr-wheel-lock.json --target /ocr-deps --wheel-dir /tmp/ocr-wheels && rm -rf /tmp/ocr-wheels; fi
COPY tools/wcdb-key-tool /opt/wcdb-key-tool
COPY assets/fonts /source/assets/fonts
COPY LICENSE THIRD_PARTY_NOTICES.md /source/

WORKDIR /source
USER 1000:1000
ENTRYPOINT ["python3"]
CMD ["/tool/digest.py", "--help"]
