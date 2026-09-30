FROM python:3.12-slim

ARG DPT_VERSION=2.19.0
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DPT_VERSION=${DPT_VERSION} \
    DPT_JAR=/opt/dpt/dpt.jar \
    DATA_DIR=/app/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jre-headless curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/dpt /app/data/jobs

# Official DPT Shell release. v2.19.0 is the latest release currently listed.
RUN curl -fsSL "https://github.com/luoyesiqiu/dpt-shell/releases/download/v${DPT_VERSION}/executable.zip" -o /tmp/dpt.zip \
    && unzip -q /tmp/dpt.zip -d /opt/dpt \
    && find /opt/dpt -type f -name 'dpt.jar' -exec cp {} /opt/dpt/dpt.jar \; \
    && test -f /opt/dpt/dpt.jar \
    && rm -f /tmp/dpt.zip

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY main.py .

CMD ["python", "main.py"]
