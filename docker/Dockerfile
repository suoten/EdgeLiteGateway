FROM node:18-alpine AS frontend-builder

WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci --registry https://registry.npmmirror.com
COPY web/ ./
RUN npm run build

FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml setup.py ./
COPY src/edgelite/ src/edgelite/
RUN pip install --no-cache-dir --prefix=/install ".[ai]"

FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=frontend-builder /build/dist /app/frontend/dist
COPY configs/ configs/
COPY alembic/ alembic/
COPY alembic.ini alembic.ini
COPY scripts/init_db.py scripts/

RUN mkdir -p data/backups logs

# Verify onnxruntime is available and model files are installed
RUN python -c "import onnxruntime; print('onnxruntime version:', onnxruntime.__version__)" && \
    python -c "from pathlib import Path; import edgelite; p = Path(edgelite.__file__).parent / 'ai_models'; print('AI models dir:', p, 'files:', list(p.glob('*.onnx')))"

COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && chown -R appuser:appuser /app

ENV PYTHONPATH=/app
ENV EDGELITE_CONFIG=configs/config.yaml

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
    CMD python -c "
import urllib.request
import json

try:
    # Check main health endpoint
    resp = urllib.request.urlopen('http://localhost:8080/health', timeout=5)
    if resp.status != 200:
        exit(1)

    # Check detailed health (optional endpoint)
    try:
        resp2 = urllib.request.urlopen('http://localhost:8080/api/v1/system/health', timeout=5)
        data = json.loads(resp2.read())
        # Verify key services are healthy
        if not data.get('data', {}).get('database', False):
            exit(1)
    except:
        pass  # Optional endpoint may not exist

    exit(0)
except Exception as e:
    exit(1)
"

CMD ["python", "-m", "edgelite", "--host", "0.0.0.0", "--port", "8080"]
