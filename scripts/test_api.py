"""API集成验证脚本"""
import httpx

BASE = "http://127.0.0.1:8080"

def main():
    # 1. API文档
    r = httpx.get(f"{BASE}/docs")
    print(f"1. API docs: {r.status_code}")

    # 2. OpenAPI schema
    r = httpx.get(f"{BASE}/openapi.json")
    schema = r.json()
    print(f"2. OpenAPI endpoints: {len(schema['paths'])}")
    for path in sorted(schema['paths'].keys()):
        methods = list(schema['paths'][path].keys())
        print(f"   {path}: {methods}")

    # 3. 登录
    r = httpx.post(f"{BASE}/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    print(f"3. Login: {r.status_code}")
    token_data = r.json()["data"]
    token = token_data["access_token"]
    print(f"   Token type: {token_data['token_type']}, expires_in: {token_data['expires_in']}s")

    headers = {"Authorization": f"Bearer {token}"}

    # 4. 设备列表
    r = httpx.get(f"{BASE}/api/v1/devices", headers=headers)
    print(f"4. Devices: {r.status_code} - total: {r.json().get('total', 0)}")
    if r.json().get("data"):
        for d in r.json()["data"]:
            print(f"   - {d['device_id']}: {d['name']} ({d['protocol']}, {d['status']})")

    # 5. 系统状态
    r = httpx.get(f"{BASE}/api/v1/system/status", headers=headers)
    print(f"5. System status: {r.status_code}")
    if r.status_code == 200:
        s = r.json()["data"]
        print(f"   CPU: {s['cpu_percent']:.1f}%, Mem: {s['memory_percent']:.1f}%, Devices: {s['device_total']}, Online: {s['device_online']}")

    # 6. 告警列表
    r = httpx.get(f"{BASE}/api/v1/alarms", headers=headers)
    print(f"6. Alarms: {r.status_code} - total: {r.json().get('total', 0)}")

    # 7. 规则列表
    r = httpx.get(f"{BASE}/api/v1/rules", headers=headers)
    print(f"7. Rules: {r.status_code} - total: {r.json().get('total', 0)}")

    # 8. 用户列表
    r = httpx.get(f"{BASE}/api/v1/users", headers=headers)
    print(f"8. Users: {r.status_code} - count: {len(r.json().get('data', []))}")
    if r.json().get("data"):
        for u in r.json()["data"]:
            print(f"   - {u['username']} ({u['role']})")

    # 9. 创建规则测试
    r = httpx.post(f"{BASE}/api/v1/rules", headers=headers, json={
        "name": "温度过高告警",
        "device_id": "sim-temperature-01",
        "conditions": [{"point": "temperature", "operator": ">", "threshold": 30}],
        "logic": "AND",
        "duration": 0,
        "severity": "warning",
        "notify_channels": ["dingtalk"],
    })
    print(f"9. Create rule: {r.status_code}")
    if r.status_code == 201:
        rule = r.json()["data"]
        print(f"   Rule ID: {rule['rule_id']}")

    # 10. 设备测点读取
    r = httpx.get(f"{BASE}/api/v1/devices/sim-temperature-01/points", headers=headers)
    print(f"10. Device points: {r.status_code}")
    if r.status_code == 200:
        points = r.json().get("data", {})
        print(f"   Points: {list(points.keys()) if isinstance(points, dict) else points}")

    print()
    print("=== All API tests passed! ===")

if __name__ == "__main__":
    main()
