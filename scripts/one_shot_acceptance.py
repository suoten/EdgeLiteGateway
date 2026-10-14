#!/usr/bin/env python3
"""一键验收：预处理设备 -> 运行验收脚本。"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

PF = "http://127.0.0.1:8000/api/v1"
EL = "http://127.0.0.1:8180/api/v1"


class Client:
    def __init__(self, base, user, password):
        self.base = base
        self.user = user
        self.password = password
        self.token = ""
        self.csrf = ""

    def login(self):
        data = json.dumps({"username": self.user, "password": self.password, "no_revoke": True}).encode()
        req = urllib.request.Request(f"{self.base}/auth/login", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.load(resp)
        payload = body.get("data") if isinstance(body.get("data"), dict) else body
        self.token = payload["access_token"]
        self.csrf = resp.headers.get("X-CSRF-Token", "")

    def request(self, method, path, body=None):
        if not self.token:
            self.login()
        for attempt in (1, 2):
            req = urllib.request.Request(f"{self.base}{path}", method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Content-Type", "application/json")
            if self.csrf:
                req.add_header("X-CSRF-Token", self.csrf)
            data = json.dumps(body).encode() if body else None
            try:
                with urllib.request.urlopen(req, data=data, timeout=30) as resp:
                    raw = resp.read().decode()
                    new_csrf = resp.headers.get("X-CSRF-Token")
                    if new_csrf:
                        self.csrf = new_csrf
                    return resp.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as e:
                raw = e.read().decode()
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {"raw": raw[:500]}
                if e.code == 403 and "csrf" in raw.lower():
                    new_csrf = payload.get("csrf_token")
                    if new_csrf:
                        self.csrf = new_csrf
                        if attempt == 1:
                            continue
                if e.code == 401 and attempt == 1:
                    self.login()
                    continue
                return e.code, payload
        raise RuntimeError("unreachable")

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body or {})

    def put(self, path, body=None):
        return self.request("PUT", path, body or {})

    def delete(self, path):
        return self.request("DELETE", path)


# FIXED(安全-P2): 口令不再硬编码，通过环境变量注入（EL_PASS 必填）
_EL_PASS = os.environ.get("EL_PASS", "")
if not _EL_PASS:
    print(
        "ERROR: environment variable EL_PASS is required (EdgeLite admin password for acceptance testing)",
        file=sys.stderr,
    )
    raise SystemExit(1)
pf = Client(PF, "admin", "admin")
el = Client(EL, "admin", _EL_PASS)
pf.login()
el.login()

# 1. 向 ProtoForge 写入特征值
print("=== 1. 向 ProtoForge 写入特征值 ===")
WRITE_SPECS = [
    ("pf-modbus", "temp", 66.6),
    ("pf-modbus", "word1", 4321),
    ("pf-s7", "temp", 12.5),
    ("pf-s7", "word1", 1000),
    ("pf-mc", "d0", 555),
    ("pf-fins", "w0", 1234),
    ("pf-ab", "Temperature", 26.5),
    ("pf-mqtt", "temp", 36.6),
    ("pf-opcua", "motor_speed", 1500),
    ("pf-opcua", "valve_open", True),
    ("pf-http", "temperature", 42.0),
    ("pf-http", "pressure", 1.5),
]
for dev_id, point, value in WRITE_SPECS:
    code, data = pf.put(f"/devices/{dev_id}/points/{point}", {"value": value})
    print(f"  {dev_id}/{point}={value}: HTTP {code}")

# 2. 重启 ProtoForge 协议服务（同步注册表与线上内存）
print("\n=== 2. 重启 ProtoForge 协议服务 ===")
for proto in ("modbus_tcp", "s7", "mc", "fins", "ab", "mqtt"):
    pf.post(f"/protocols/{proto}/stop")
    time.sleep(1)
    pf.post(f"/protocols/{proto}/start")
    print(f"  {proto}: 已重启")
time.sleep(10)

# 3. 重新创建有问题的 EdgeLite 设备
print("\n=== 3. 重新创建 EdgeLite 设备 ===")
RECREATE = {
    "pf-modbus": {
        "device_id": "pf-modbus",
        "name": "PF Modbus TCP",
        "protocol": "modbus_tcp",
        "config": {"host": "127.0.0.1", "port": 5020, "slave_id": 5, "timeout": 5},
        "collect_interval": 5,
        "points": [
            {"name": "temp", "data_type": "float32", "address": "HR100", "access_mode": "rw"},
            {"name": "word1", "data_type": "int16", "address": "HR102", "access_mode": "rw"},
            {"name": "coil1", "data_type": "bool", "address": "C0", "access_mode": "rw"},
            {"name": "coil2", "data_type": "bool", "address": "C1", "access_mode": "rw"},
            {"name": "di1", "data_type": "bool", "address": "I0", "access_mode": "r"},
            {"name": "speed", "data_type": "int16", "address": "HR104", "access_mode": "rw"},
        ],
    },
    "pf-ab": {
        "device_id": "pf-ab",
        "name": "PF AB",
        "protocol": "allen_bradley",
        "config": {"ip": "127.0.0.1", "port": 44818, "timeout": 5},
        "collect_interval": 5,
        "points": [
            {"name": "Temperature", "data_type": "float32", "address": "Temperature", "access_mode": "rw"},
            {"name": "Setpoint", "data_type": "float32", "address": "Setpoint", "access_mode": "rw"},
        ],
    },
    "pf-opcua": {
        "device_id": "pf-opcua",
        "name": "PF OPC UA",
        "protocol": "opcua",
        "config": {"endpoint": "opc.tcp://127.0.0.1:4840", "security_mode": "None", "timeout": 5},
        "collect_interval": 5,
        "points": [
            {"name": "motor_speed", "data_type": "int32", "address": "ns=2;s=motor_speed", "access_mode": "rw"},
            {"name": "valve_open", "data_type": "bool", "address": "ns=2;s=valve_open", "access_mode": "rw"},
        ],
    },
    "pf-mqtt": {
        "device_id": "pf-mqtt",
        "name": "PF MQTT",
        "protocol": "mqtt_client",
        "config": {
            "broker": "127.0.0.1",
            "port": 1883,
            "subscribe_topic": "protoforge/#",
            "publish_topic": "protoforge/command",
            "client_id": "pf-mqtt-el9",
            "topic_prefix": "protoforge",
            "retain": False,
            "qos": 0,
        },
        "collect_interval": 5,
        "points": [
            {
                "name": "temp",
                "data_type": "float32",
                "unit": "C",
                "address": "protoforge/pf-mqtt/temp",
                "access_mode": "rw",
            },
            {"name": "switch", "data_type": "float32", "address": "protoforge/pf-mqtt/switch", "access_mode": "rw"},
        ],
    },
    "pf-http": {
        "device_id": "pf-http",
        "name": "PF HTTP",
        "protocol": "http_webhook",
        "config": {"url": "http://127.0.0.1:8080/webhook/data", "method": "POST", "timeout": 10},
        "collect_interval": 5,
        "points": [
            {"name": "temperature", "data_type": "float32", "address": "temperature", "access_mode": "rw"},
            {"name": "pressure", "data_type": "float32", "address": "pressure", "access_mode": "rw"},
        ],
    },
}

for dev_id, config in RECREATE.items():
    el.delete(f"/devices/{dev_id}")
    time.sleep(1)
    code, data = el.post("/devices", config)
    print(f"  {dev_id}: HTTP {code}")
    time.sleep(1)

# 等待驱动连接
print("\n等待驱动连接 (30s)...")
time.sleep(30)

# 4. 推送 pf-http 数据
print("=== 4. 推送 pf-http 数据 ===")
push_payload = {
    "data": {
        "temperature": {"value": 42.0, "quality": "good", "timestamp": "2026-01-01T00:00:00Z"},
        "pressure": {"value": 1.5, "quality": "good", "timestamp": "2026-01-01T00:00:00Z"},
    }
}
code, data = el.post("/devices/pf-http/push", push_payload)
print(f"  push pf-http: HTTP {code}")

# 5. 再次写入特征值（设备创建后）
print("\n=== 5. 再次写入特征值 ===")
for dev_id, point, value in WRITE_SPECS:
    code, data = pf.put(f"/devices/{dev_id}/points/{point}", {"value": value})
    print(f"  {dev_id}/{point}={value}: HTTP {code}")

# 等待采集
print("\n等待采集 (15s)...")
time.sleep(15)

# 6. 运行验收脚本
print("\n=== 6. 运行验收脚本 ===")
env = os.environ.copy()
env["PF_URL"] = PF
env["PF_USER"] = "admin"
env["PF_PASS"] = "admin"
env["EL_URL"] = EL
env["EL_USER"] = "admin"
env["EL_PASS"] = _EL_PASS

script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "joint_acceptance.py")
report = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "joint_acceptance_report.json")
cmd = [sys.executable, script, "--report", report]
print(f"Running: {' '.join(cmd)}")
result = subprocess.run(cmd, env=env, cwd=os.path.dirname(script))
print(f"\nExit code: {result.returncode}")
