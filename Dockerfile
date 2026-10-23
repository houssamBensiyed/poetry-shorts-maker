FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock ./
# Optional CA bundle for corporate/cloud HTTPS proxies; never disable TLS checks.
# The bundle is mounted only for this build step and is not stored in the image.
RUN --mount=type=secret,id=build_ca_bundle \
    if [ -s /run/secrets/build_ca_bundle ]; then \
        PIP_CERT=/run/secrets/build_ca_bundle python -m pip install --no-cache-dir -r requirements.lock; \
    else \
        python -m pip install --no-cache-dir -r requirements.lock; \
    fi \
    && python -m pip check

RUN groupadd --gid 1000 builder \
    && useradd --uid 1000 --gid builder --create-home builder \
    && mkdir -p /app/buffer/voiceovers /app/assets/images \
        /app/output /app/temp /app/logs /tmp/poetry-video-builder-locks \
    && chown -R builder:builder /app \
    && chmod 1777 /tmp/poetry-video-builder-locks

COPY --chown=builder:builder app ./app
COPY --chown=builder:builder assets ./assets
COPY --chown=builder:builder tests ./tests
COPY --chown=builder:builder main.py api.py config.yaml ./

USER builder
CMD ["python", "main.py"]
