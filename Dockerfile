# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sysdesign/ sysdesign/
ARG GIT_SHA=unknown
ARG APP_VERSION=dev
ENV GIT_SHA=${GIT_SHA} APP_VERSION=${APP_VERSION}
LABEL org.opencontainers.image.revision=${GIT_SHA}
RUN useradd -r -u 10001 app
USER 10001
EXPOSE 8000
CMD ["python", "-m", "sysdesign.server"]
