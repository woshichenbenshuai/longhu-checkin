FROM python:3.11-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data

# 允许构建时指定 pip 源（国内可传清华源加速），默认官方源
ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /app

COPY requirements.lock ./
RUN pip install --require-hashes -i ${PIP_INDEX_URL} -r requirements.lock

COPY app ./app

RUN groupadd --gid 10001 longhu \
    && useradd --uid 10001 --gid 10001 --no-create-home longhu \
    && mkdir -p /data \
    && chown longhu:longhu /data \
    && chmod 700 /data
USER 10001:10001
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
