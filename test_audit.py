"""临时测试脚本：复现 audit/logs 500 错误"""
import asyncio
import sys
sys.path.insert(0, "src")

from edgelite.services.audit_service import AuditService

async def test():
    svc = AuditService()
    await svc.initialize()
    try:
        logs, total = await svc.query(page=1, size=20)
        print(f"Success: {total} logs")
        if logs:
            print(f"First log keys: {list(logs[0].keys())}")
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await svc.close()

asyncio.run(test())
