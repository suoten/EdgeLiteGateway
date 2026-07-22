"""Clean up EdgeLite test devices."""
import io
import sys
import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

EDGELITE_URL = "http://127.0.0.1:8180"

resp = requests.post(
    f"{EDGELITE_URL}/api/v1/auth/login",
    json={"username": "admin", "password": "EdgeLite@2026"},
    timeout=10,
)
data = resp.json().get("data", resp.json())
token = data.get("access_token", "")
csrf = data.get("csrf_token", "")
headers = {"Authorization": f"Bearer {token}"}
if csrf:
    headers["X-CSRF-Token"] = csrf

resp = requests.get(f"{EDGELITE_URL}/api/v1/devices", headers=headers, timeout=10)
devices_data = resp.json()
inner = devices_data.get("data", devices_data)
devices = inner if isinstance(inner, list) else inner.get("items", inner.get("devices", []))

print(f"Found {len(devices)} devices in EdgeLite")

for dev in devices:
    dev_id = dev.get("device_id") or dev.get("id", "")
    if dev_id:
        print(f"  Deleting {dev_id}...")
        try:
            resp = requests.delete(
                f"{EDGELITE_URL}/api/v1/devices/{dev_id}",
                headers=headers,
                timeout=10,
            )
            print(f"    HTTP {resp.status_code}")
        except Exception as e:
            print(f"    Error: {e}")

print("Done!")
