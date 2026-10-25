FROM python:3.13-slim-trixie

# Install uv from the official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Configure environment:
# - UV_COMPILE_BYTECODE: compile installed packages to bytecode at build time
# - UV_LINK_MODE=copy: copy instead of hardlinking across mount points
# - PYTHONUNBUFFERED=1: flush stdout/stderr immediately so `docker logs` is live
# - PYTHONDONTWRITEBYTECODE=1: the container runs as an arbitrary non-root user
#   (see docker-compose.yml) that can't write under /app
# - AAATA_GTFS_DATA_PATH: data folder in the container, mounted from the host's ./data
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AAATA_GTFS_DATA_PATH=/app/data \
    PATH="/app/.venv/bin:$PATH"

# Install dependencies first for Docker layer caching
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project

COPY . .

# Install the project itself (aaata-gtfs)
RUN uv sync --frozen \
    && chmod +x docker-entrypoint.sh \
    # Readable by any user; /app/data writable even without a mount.
    && chmod -R a+rX /app \
    && mkdir -p /app/data && chmod 777 /app/data

ENTRYPOINT ["/app/docker-entrypoint.sh"]

CMD ["collect.py"]
