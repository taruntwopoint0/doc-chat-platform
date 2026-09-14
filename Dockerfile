# Two stages: build the virtualenv, then copy only the venv into a clean base.
# Nothing here installs PyTorch or downloads a model -- that is what the docling
# extra is for, and it is deliberately not part of this image.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .

# --no-compile skips .pyc generation, which is ~40 MB of the install. The cost
# is that each module is compiled in memory on first import, once per container.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --no-compile -r requirements.txt


FROM python:3.12-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

# Legacy .doc/.ppt/.xls need LibreOffice, which is roughly 500 MB installed --
# far more than the rest of the image put together. It is left out; those
# uploads fail with a message saying so, and .xls still works through xlrd.
# To support them, add here and rebuild:
#   RUN apt-get update && apt-get install -y --no-install-recommends \
#         libreoffice-writer libreoffice-impress libreoffice-calc \
#       && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts

# Run as a non-root user; nothing in the image needs to be written to.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url=f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/health\"; \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

# Migrations run at boot: Render has no separate release phase on the Starter
# tier, and the migration is idempotent.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
