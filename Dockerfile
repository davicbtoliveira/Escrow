FROM ghcr.io/astral-sh/uv:0.9.16 AS uv

FROM python:3.13-slim-bookworm

COPY --from=uv /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY manage.py ./
COPY src ./src

RUN groupadd --system escrow \
    && useradd --system --gid escrow --home-dir /app --shell /usr/sbin/nologin escrow \
    && chown -R escrow:escrow /app /tmp/uv-cache

USER escrow

EXPOSE 8000

CMD ["sh", "-c", "uv run --no-sync python manage.py migrate --noinput && uv run --no-sync daphne --access-log /dev/null -b 0.0.0.0 -p 8000 escrow.asgi:application"]
