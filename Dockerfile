FROM python:3.12-slim

# 时区数据（让“今天”按北京时间计算）
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai \
    SOLAR_TZ=Asia/Shanghai \
    SOLAR_DB=/data/solar.db \
    SOLAR_HOST=0.0.0.0 \
    SOLAR_PORT=8000 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY solar ./solar
COPY run.py ./

RUN useradd --uid 1000 --create-home solar && mkdir -p /data && chown solar /data
USER solar
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/auth', timeout=4)"

CMD ["python", "run.py", "--no-browser"]
