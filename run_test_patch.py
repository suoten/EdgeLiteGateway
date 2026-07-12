import sys
sys.path.insert(0, "src")
import asyncio
from unittest.mock import AsyncMock, patch

async def main():
    print("Before patch, asyncio.sleep:", asyncio.sleep)

    with patch("asyncio.sleep", new=AsyncMock()):
        print("Inside patch, asyncio.sleep:", asyncio.sleep)
        await asyncio.sleep(1.0)
        print("After mocked sleep call")

    print("After patch, asyncio.sleep:", asyncio.sleep)

    import edgelite.services.platform_service as ps
    print("\nps.asyncio:", ps.asyncio)
    print("ps.asyncio is asyncio:", ps.asyncio is asyncio)

    with patch("edgelite.services.platform_service.asyncio.sleep", new=AsyncMock()) as m:
        print("Inside dotted patch, asyncio.sleep:", asyncio.sleep)
        print("ps.asyncio.sleep:", ps.asyncio.sleep)
        await asyncio.sleep(1.0)
        print("After mocked sleep call (dotted)")

asyncio.run(main())
