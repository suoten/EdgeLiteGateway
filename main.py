"""EdgeLiteGateway 快捷启动脚本

使用方式:
    python main.py [选项]

等价于:
    pip install -e . && python -m edgelite [选项]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from edgelite.__main__ import main

if __name__ == "__main__":
    main()
