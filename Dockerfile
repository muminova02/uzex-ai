# UZEX Data Assistant — one image for the one-shot import, the web service and the Telegram bot.
# Secrets are NEVER baked in: .env is excluded by .dockerignore and passed at run time (env_file).
# Build:  docker compose build --build-arg BUILD_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo none)

# ---- stage 1: frontend bundle (locked by web/package-lock.json)
FROM node:22.23.1-bookworm-slim AS web
WORKDIR /w
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- stage 2: Python runtime (locked by requirements-runtime.lock)
FROM python:3.12.13-slim-bookworm AS runtime
ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements-runtime.lock requirements.lock ./
RUN pip install --no-deps -r requirements-runtime.lock && pip check
COPY pyproject.toml ./
COPY src/ ./src/
COPY scripts/write_build_info.py scripts/freeze.py scripts/quota_check.py ./scripts/
COPY --from=web /w/dist ./web/dist
COPY web/package-lock.json ./web/package-lock.json
ARG BUILD_COMMIT=none
RUN python scripts/write_build_info.py --commit "${BUILD_COMMIT}" --out /app/BUILD_INFO.json \
 && useradd --system --uid 10001 --home-dir /app uzex \
 && mkdir -p /data/db /data/state /data/logs && chown -R uzex /data
USER uzex
# runtime defaults; every limit is also set explicitly in docker-compose.yml
ENV UZEX_DATA_DIR=/data/source UZEX_DB_PATH=/data/db/uzex.duckdb UZEX_LLM_GATE_PATH=/data/state/llm_gate.sqlite \
    UZEX_BUILD_INFO_PATH=/app/BUILD_INFO.json UZEX_RUNTIME_IMPORT=false UZEX_WEB_HOST=0.0.0.0 UZEX_WEB_PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "uzex_assistant.web"]
