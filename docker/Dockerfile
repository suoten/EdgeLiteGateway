# EdgeLiteGateway Docker Image
FROM python:3.11-slim AS builder

WORKDIR /app

COPY pyproject.toml setup.py ./
COPY src/edgelite/ src/edgelite/
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /install /usr/local
COPY configs/ configs/
COPY alembic/ alembic/
COPY alembic.ini alembic.ini
COPY scripts/ scripts/

RUN mkdir -p data/backups logs

ENV PYTHONPATH=/app
ENV EDGELITE_CONFIG=configs/config.yaml

EXPOSE 8080

CMD ["python", "-m", "edgelite", "--host", "0.0.0.0", "--port", "8080"]
