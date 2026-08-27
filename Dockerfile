FROM python:3.13-slim

ARG EPUBCHECK_VERSION=5.3.0
ARG EPUBCHECK_SHA256=6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TMPDIR=/data/tmp

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        default-jre-headless \
        tesseract-ocr \
        tesseract-ocr-ara \
        tesseract-ocr-eng \
        unzip \
    && curl -fsSL \
        "https://github.com/w3c/epubcheck/releases/download/v${EPUBCHECK_VERSION}/epubcheck-${EPUBCHECK_VERSION}.zip" \
        -o /tmp/epubcheck.zip \
    && echo "${EPUBCHECK_SHA256}  /tmp/epubcheck.zip" | sha256sum -c - \
    && mkdir -p /opt/epubcheck \
    && unzip -q /tmp/epubcheck.zip -d /opt/epubcheck \
    && rm /tmp/epubcheck.zip \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY pdf2epub ./pdf2epub
COPY docker/epubcheck /usr/local/bin/epubcheck

RUN chmod 0755 /usr/local/bin/epubcheck \
    && pip install . \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data/tmp \
    && chown -R app:app /data

USER app
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

CMD ["uvicorn", "pdf2epub.web:app", "--host", "0.0.0.0", "--port", "8000"]
