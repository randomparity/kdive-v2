"""Default backing-service compose file installed by ``python -m kdive install-compose``."""

from __future__ import annotations

LOCAL_COMPOSE = """services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: kdive
      POSTGRES_PASSWORD: kdive # pragma: allowlist secret
      POSTGRES_DB: kdive
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U kdive"]
      interval: 5s
      timeout: 5s
      retries: 20

  minio:
    image: minio/minio:RELEASE.2025-04-22T22-12-26Z
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin # pragma: allowlist secret
    ports:
      - "9000:9000"
      - "9001:9001"
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 5s
      timeout: 5s
      retries: 20

  minio-init:
    image: minio/mc:RELEASE.2025-04-16T18-13-26Z
    depends_on:
      minio:
        condition: service_healthy
    entrypoint:
      - /bin/sh
      - -c
      - |
        mc alias set local http://minio:9000 minioadmin minioadmin # pragma: allowlist secret
        mc mb --ignore-existing local/kdive-artifacts

  oidc:
    image: ghcr.io/navikt/mock-oauth2-server:3.0.3
    ports:
      - "8090:8080"
    environment:
      SERVER_PORT: "8080"
"""
