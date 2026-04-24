"""数据库初始化脚本"""

import asyncio
import sys
from pathlib import Path

# 添加src到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from edgelite.storage.database import Database


async def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/edgelite.db"
    print(f"初始化数据库: {db_path}")
    db = Database(db_path)
    await db.connect()
    await db.init_tables()
    await db.close()
    print("数据库初始化完成")


if __name__ == "__main__":
    asyncio.run(main())
