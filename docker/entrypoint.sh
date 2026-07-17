#!/bin/sh
set -e

if [ ! -f configs/config.yaml ] && [ -f configs/config.example.yaml ]; then
    # 7#淇: configs 鍦ㄧ敓浜?compose 涓凡鏀逛负鍙鎸傝浇锛?ro锛夛紝cp 浼氬け璐ュ鑷?set -e 閫€鍑恒€?
    # 浠呭湪 configs 鍙啓鏃讹紙濡?dev compose锛夎嚜鍔ㄥ鍒讹紱鍙鍦烘櫙瑕佹眰瀹夸富鏈轰晶棰勫厛鍑嗗濂?config.yaml
    if [ -w configs ]; then
        cp configs/config.example.yaml configs/config.yaml
        echo "[entrypoint] configs/config.yaml created from config.example.yaml"
    else
        echo "[entrypoint] WARNING: configs/config.yaml missing and configs is read-only; please create it on the host (e.g. cp configs/config.example.yaml configs/config.yaml) before starting the container"
    fi
fi

# Ensure data and logs directories are writable
mkdir -p data/backups data/ota logs

# 6#淇: Dockerfile 绗?2琛屽凡 `USER appuser`锛屽鍣ㄤ互闈?root 鐢ㄦ埛鍚姩锛?
# 姝ゅ `id -u == 0` 鍒ゆ柇姘歌繙涓?false锛屽師 chown 閫昏緫澶辨晥銆?
# 鐢变簬 docker-compose 閫氳繃 bind mount 灏嗗涓绘満 ../data銆?./logs 鎸傝浇鍒?/app/data銆?app/logs锛?
# 瀹瑰櫒鍐?chown 涔熸棤娉曟敼鍙樺涓绘満鐩綍灞炰富锛堝嵆渚挎湁 root锛夈€?
# 姝ｇ‘鍋氭硶锛氬涓绘満渚ч鍏堜慨姝ｆ寕杞界洰褰曟潈闄愶紝渚嬪锛?
#   sudo chown -R 1000:1000 data logs    # 1000 涓洪暅鍍忓唴 appuser 鐨勯粯璁?uid:gid
#   sudo chmod -R u+rwX data logs
# 闀滃儚鍐?/app/data銆?app/logs 宸插湪 Dockerfile 鏋勫缓闃舵鐢?root 瀹屾垚 chown锛堣 Dockerfile 绗?5-36琛岋級銆?

# FIXED-P2: 鍚姩鍓嶆墽琛屾暟鎹簱杩佺Щ锛屽け璐ユ椂缁堟鍚姩鑰岄潪闈欓粯璺宠繃
if command -v alembic >/dev/null 2>&1; then
    if ! alembic upgrade head 2>&1; then
        echo "[entrypoint] FATAL: alembic migration failed, aborting startup"
        exit 1
    fi
fi

# Check for graceful restart marker
if [ -f data/ota/graceful_restart.json ]; then
    echo "[entrypoint] Graceful restart marker detected, upgrade was applied"
    echo "[entrypoint] Starting with updated code..."
fi

exec "$@"
