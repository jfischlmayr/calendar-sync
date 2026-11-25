# syntax=docker/dockerfile:1
# Pinned by digest (python:3.13-slim-bookworm); vdirsyncer 0.21.0 + frozen deps in requirements.lock.
# Third-party licenses (incl. GPL-3.0-or-later aiostream): docs/THIRD-PARTY-LICENSES.md. Do not push this image
# to a public registry without reading the distribution notes there.
FROM python@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src \
    CALSYNC_DATA=/data CALSYNC_RUN=/run/calsync CALSYNC_CONFIG=/config/calsync.toml
RUN apt-get update && apt-get install -y --no-install-recommends age rclone ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin calsync \
    && mkdir -p /data /config /run/calsync && chown calsync /data /run/calsync
COPY src /app/src
COPY config/calsync.example.toml /config/calsync.toml
COPY LICENSE docs/THIRD-PARTY-LICENSES.md /usr/share/doc/calsync/
COPY scripts/entrypoint.sh scripts/restore-drill.sh /app/scripts/
# source/config must be readable by the non-root user regardless of the build context's file modes
RUN chmod 0755 /app/scripts/*.sh && chmod -R a+rX,go-w /app /config /usr/share/doc/calsync
USER calsync
WORKDIR /app
VOLUME ["/data"]
# No EXPOSE: the health endpoint binds 127.0.0.1 inside the container and nothing is published by default.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -m calsync healthcheck || exit 1
ENTRYPOINT ["/usr/bin/tini","--","/app/scripts/entrypoint.sh"]
CMD ["serve"]
