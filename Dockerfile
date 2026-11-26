# syntax=docker/dockerfile:1
FROM python:3.12-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-trixie

RUN useradd --system --uid 1000 --home-dir /data paperless-sources \
    && mkdir -p /data /config \
    && chown paperless-sources /data

COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

USER paperless-sources
VOLUME ["/data"]
ENTRYPOINT ["paperless-sources"]
