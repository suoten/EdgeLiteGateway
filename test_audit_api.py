"""临时测试脚本：通过 HTTP 调用 audit/logs API 并获取详细错误"""
import asyncio
import httpx
import sys
sys.path.insert(0, "src")

async def test():
    async with httpx.AsyncClient(base_url="http://localhost:8180") as client:
        # 先登录获取 token
        login_resp = await client.post("/api/v1/auth/login", json={
            "username": "admin",
            "password": "EdgeLite@2026"
        })
        print(f"Login status: {login_resp.status_code}")
        if login_resp.status_code != 200:
            print(f"Login failed: {login_resp.text}")
            return
        
        cookies = login_resp.cookies
        token = login_resp.json().get("data", {}).get("token")
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        
        # 调用 audit/logs
        resp = await client.get("/api/v1/audit/logs?page=1&size=20", cookies=cookies, headers=headers)
        print(f"Audit logs status: {resp.status_code}")
        print(f"Response: {resp.text[:500]}")

asyncio.run(test())
