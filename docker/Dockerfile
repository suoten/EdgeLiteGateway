FROM node:26-alpine AS frontend-builder

# FIXED-P2: npm 镜像可参数化，国内默认 npmmirror，海外构建可用：
#   docker build --build-arg NPM_REGISTRY=https://registry.npmjs.org -f docker/Dockerfile .
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
# 构建命令: docker build --build-arg INSTALL_AI=false -f docker/Dockerfile .
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

COPY --from=builder /install /usr/local
# FIXED-SEC: 消灭 jaraco.context<6.1.0 / wheel<0.46.2 的三处残留源（.ci-sim 最小复现实测）：
# 1) 基础镜像 setuptools 将旧版 vendor 进 setuptools/_vendor/（Trivy 实报 0.45.1 的来源）→ 升级 setuptools 换新 vendor
# 2) 旧版 jaraco.context 5.3.0 的 dist-info 目录名带点（jaraco.context-5.3.0.dist-info），
#    新版规范化为下划线，两种命名都必须清理
# 3) /install 拷贝与基础镜像自带包同目录共存，pip 只升级它"看见"的一套，另一套残留
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

RUN groupadd -r appuser && useradd -r -g appuser appuser  # FIXED-P0: 先创建用户再chown

RUN mkdir -p data/backups logs && chown -R appuser:appuser data logs \
    && chown -R appuser:appuser /app

# Verify onnxruntime is available and model files are installed (only when AI deps are included)
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

# FIXED: 使用 /health/live 轻量端点替代 /health [2026-06-29]
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
    CMD python -c "import urllib.request; exit(0 if urllib.request.urlopen('http://localhost:8080/health/live', timeout=5).status == 200 else 1)"

# FIXED(2026-09-19): 恢复 --port 8080 — __main__.py 默认端口已改为 8180（与
# 本地部署及前端代理对齐），但镜像 EXPOSE/HEALTHCHECK 均为 8080，裸 docker run
# 时应用监听 8180 导致健康检查必败（CI smoke/acceptance 实证）。镜像内固定
# 8080 与镜像声明自洽；compose 部署仍可用 EDGELITE_SERVER__PORT 覆盖（同为 8080）
# 保留 --host 0.0.0.0 因为容器内必须监听所有网卡（默认 127.0.0.1 无法从外部访问）
CMD ["python", "-m", "edgelite", "--host", "0.0.0.0", "--port", "8080"]
