# Multi-stage so the final image doesn't ship build tooling.
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Install the package into a dedicated prefix so we can copy a clean tree
# into the runtime stage. Copy pyproject + src together so the install can
# resolve the wheel.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --prefix=/install .

# ----------------------------------------------------------------------

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/weather.db

# Non-root user; the mounted volume needs to be writable by it.
RUN useradd --create-home --uid 1000 watch && \
    mkdir -p /data && chown -R watch:watch /data

COPY --from=builder /install /usr/local

USER watch
WORKDIR /home/watch

EXPOSE 8000

# ``--proxy-headers`` is harmless behind a reverse proxy and a no-op
# otherwise. ``--no-access-log`` keeps the logs to our structured ones.
CMD ["uvicorn", "watchagent.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--no-access-log"]
