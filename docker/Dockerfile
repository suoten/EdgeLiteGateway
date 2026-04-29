# EdgeLiteGateway Docker Image
FROM python:3.11-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /install /usr/local
COPY src/edgelite/ edgelite/
COPY configs/ configs/
COPY alembic/ alembic/
COPY alembic.ini alembic.ini

# 创建数据目录
RUN mkdir -p data/backups logs

ENV PYTHONPATH=/app
ENV EDGELITE_CONFIG=configs/config.yaml

EXPOSE 8080

CMD ["python", "-m", "edgelite", "--host", "0.0.0.0", "--port", "8080"]
