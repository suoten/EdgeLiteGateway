# EdgeLite Gateway - Root Dockerfile (scanner compatibility)
# The canonical Dockerfile is at docker/Dockerfile — both are kept in sync.
# Build: docker build .  OR  docker build -f docker/Dockerfile .

FROM node:18-alpine AS frontend-builder

# FIXED-P2: npm 镜像可参数化，国内默认 npmmirror，海外构建可用：
#   docker build --build-arg NPM_REGISTRY=https://registry.npmjs.org .
ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci --registry $NPM_REGISTRY
COPY web/ ./
RUN npm run build

FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# FIXED: 支持条件安装AI依赖，减小非AI场景的镜像体积
ARG INSTALL_AI=true

COPY pyproject.toml setup.py ./
COPY src/edgelite/ src/edgelite/
RUN if [ "$INSTALL_AI" = "true" ]; then \
        pip install --no-cache-dir --prefix=/install ".[ai]"; \
    else \
        pip install --no-cache-dir --prefix=/install "."; \
    fi

FROM python:3.11-slim

WORKDIR /app

# FIXED-SEC: 升级带安全修复的系统包（与 docker/Dockerfile 同步）。官方 python:3.11-slim 只在自身
# 重建时才吸收 Debian 安全更新，补丁发布到镜像重建之间存在窗口（如 CVE-2026-124851 / libzstd1，
# 2026-11-24 披露），期间 Trivy 镜像扫描因"存在修复版本"直接失败。
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
# FIXED-SEC: 与 docker/Dockerfile 同步——消灭 jaraco.context<6.1.0 / wheel<0.46.2 的残留
# （基础镜像 setuptools 的 vendor、旧 dist-info 目录、与 /install 共存的另一套）
RUN pip install --no-cache-dir --upgrade "setuptools>=80.9" \
 && rm -rf /usr/local/lib/python3.11/site-packages/wheel-*.dist-info \
            /usr/local/lib/python3.11/site-packages/jaraco_context-*.dist-info \
            /usr/local/lib/python3.11/site-packages/jaraco.context-*.dist-info \
 && pip install --no-cache-dir "jaraco.context>=6.1.0" "wheel>=0.46.2"
COPY --from=frontend-builder /build/dist /app/frontend/dist
COPY configs/ configs/
COPY alembic/ alembic/
COPY alembic.ini alembic.ini
COPY scripts/init_db.py scripts/

COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

RUN groupadd -r appuser && useradd -r -g appuser appuser

RUN mkdir -p data/backups logs && chown -R appuser:appuser data logs \
    && chown -R appuser:appuser /app

ARG INSTALL_AI=true
RUN if [ "$INSTALL_AI" = "true" ]; then \
        python -c "import onnxruntime; print('onnxruntime version:', onnxruntime.__version__)" && \
        python -c "from pathlib import Path; import edgelite; p = Path(edgelite.__file__).parent / 'ai_models'; print('AI models dir:', p, 'files:', list(p.glob('*.onnx')))"; \
    else \
        echo "AI dependencies skipped (INSTALL_AI=false)"; \
    fi

USER appuser

ENV PYTHONPATH=/app
ENV EDGELITE_CONFIG=configs/config.yaml

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
    CMD python -c "import urllib.request; exit(0 if urllib.request.urlopen('http://localhost:8080/health/live', timeout=5).status == 200 else 1)"

CMD ["python", "-m", "edgelite", "--host", "0.0.0.0", "--port", "8080"]
