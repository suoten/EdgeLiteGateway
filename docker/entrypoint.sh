#!/bin/sh
set -e

if [ ! -f configs/config.yaml ] && [ -f configs/config.example.yaml ]; then
    cp configs/config.example.yaml configs/config.yaml
    echo "[entrypoint] configs/config.yaml created from config.example.yaml"
fi

exec "$@"
