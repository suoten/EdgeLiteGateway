"""核心链路验证脚本"""

import os
import time

import httpx

BASE = os.environ.get("EDGELITE_TEST_BASE", "http://127.0.0.1:8080")
# FIXED: 硬编码密码admin123，改为环境变量读取
_TEST_USER = os.environ.get("EDGELITE_TEST_USER", "admin")
_TEST_PASS = os.environ.get("EDGELITE_TEST_PASS", "")


def main():
    # 登录
    if not _TEST_PASS:
        print("Login: SKIPPED (set EDGELITE_TEST_PASS env var)")
        return
    r = httpx.post(f"{BASE}/api/v1/auth/login", json={"username": _TEST_USER, "password": _TEST_PASS})
    token = r.json()["data"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 等待采集数据
    print("等待10秒让模拟器采集数据...")
    time.sleep(10)

    # 读取设备测点
    r = httpx.get(f"{BASE}/api/v1/devices/sim-temperature-01/points", headers=headers)
    points = r.json().get("data", {})
    print(f"实时测点值: {points}")

    # 检查告警
    r = httpx.get(f"{BASE}/api/v1/alarms", headers=headers)
    alarms = r.json()
    total = alarms.get("total", 0)
    print(f"告警数量: {total}")
    if alarms.get("data"):
        for a in alarms["data"]:
            print(f"  - {a['alarm_id']}: {a['severity']} {a['status']} device={a['device_id']}")

    # 检查规则
    r = httpx.get(f"{BASE}/api/v1/rules", headers=headers)
    rules = r.json()
    print(f"规则数量: {rules.get('total', 0)}")
    if rules.get("data"):
        for rule in rules["data"]:
            print(f"  - {rule['rule_id']}: {rule['name']} enabled={rule['enabled']}")

    # 创建新模拟设备
    r = httpx.post(
        f"{BASE}/api/v1/devices/simulator",
        headers=headers,
        json={
            "device_id": "sim-pressure-01",
            "name": "压力传感器模拟",
            "points": [
                {
                    "name": "pressure",
                    "data_type": "float32",
                    "unit": "MPa",
                    "address": "0",
                    "access_mode": "r",
                    "min": 0.1,
                    "max": 2.0,
                    "mode": "random_walk",
                },
            ],
            "collect_interval": 3,
        },
    )
    print(f"创建新模拟设备: {r.status_code}")

    # 验证设备列表
    r = httpx.get(f"{BASE}/api/v1/devices", headers=headers)
    data = r.json()
    print(f"设备总数: {data['total']}")
    for d in data["data"]:
        print(f"  - {d['device_id']}: {d['name']} ({d['protocol']}, {d['status']})")

    # 创建备份
    r = httpx.post(f"{BASE}/api/v1/system/backup", headers=headers)
    print(f"系统备份: {r.status_code}")

    print()
    print("=== 核心链路验证完成 ===")


if __name__ == "__main__":
    main()
