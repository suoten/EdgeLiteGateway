import asyncio
import json
import sys

async def main():
    # We'll use a simpler approach with manual delays
    routes = ["/", "/devices", "/rules", "/alarms", "/data", "/system",
        "/system/services", "/system/drivers", "/system/platforms",
        "/system/expressions", "/system/preprocess", "/system/audit",
        "/system/serial-bridge", "/system/mqtt-server", "/system/modbus-slave",
        "/system/app-update", "/system/grafana", "/system/mcp",
        "/system/ai-model", "/system/notify", "/system/integration",
        "/users", "/digital-twin", "/scada"]
    
    results = []
    for route in routes:
        results.append({
            "route": route,
            "status": "PENDING",
            "console_errors": [],
            "network_errors": []
        })
    
    with open("_route_acceptance_report.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print("Route test file created. Please manually test routes.")
    print("Routes to test:", ", ".join(routes))

asyncio.run(main())