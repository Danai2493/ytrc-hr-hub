# YTRC HR Hub. One image runs anywhere Docker runs: a company server, a VM,
# or any cloud (Google Cloud Run, AWS, Azure). Nothing here is tied to one host.
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    APP_ENV=production STORAGE_DIR=/data/files

WORKDIR /app
COPY requirements.txt .
# Behind a company proxy with its own certificate: docker build --secret id=ca,src=company-ca.crt .
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini docker-entrypoint.sh ./

RUN useradd --create-home --uid 1000 hrhub && mkdir -p /data/files && chown -R hrhub /data && chmod +x docker-entrypoint.sh
USER hrhub
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
