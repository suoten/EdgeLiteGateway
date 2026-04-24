"""EdgeLiteGateway 构建配置

支持Cython加速模块编译：
    python setup.py build_ext --inplace

编译后_cython/目录下的.pyx文件会被编译为.c/.so（Linux）或.pyd（Windows），
提供3-10倍的CPU密集型操作加速。

未编译时自动回退到纯Python实现（_cython/*_py.py），功能完全相同。
"""

from setuptools import setup, Extension
from pathlib import Path

try:
    from Cython.Build import cythonize
    CYTHON_AVAILABLE = True
except ImportError:
    CYTHON_AVAILABLE = False

if CYTHON_AVAILABLE:
    extensions = [
        Extension(
            "edgelite._cython.modbus_mapper",
            ["src/edgelite/_cython/modbus_mapper.pyx"],
        ),
        Extension(
            "edgelite._cython.rule_compare",
            ["src/edgelite/_cython/rule_compare.pyx"],
        ),
    ]
    ext_modules = cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
        },
    )
else:
    ext_modules = []
    print("Cython未安装，跳过编译。使用纯Python回退实现。")
    print("安装Cython后执行: pip install cython && python setup.py build_ext --inplace")

setup(
    name="edgelite",
    ext_modules=ext_modules,
)
