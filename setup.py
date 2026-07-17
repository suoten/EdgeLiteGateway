from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

try:
    from Cython.Build import cythonize

    HAS_CYTHON = True
except ImportError:
    HAS_CYTHON = False


def get_extensions():
    if not HAS_CYTHON:
        return []
    return cythonize(
        [
            Extension("edgelite._cython.rule_compare", ["src/edgelite/_cython/rule_compare.pyx"]),
            Extension("edgelite._cython.modbus_mapper", ["src/edgelite/_cython/modbus_mapper.pyx"]),
        ],
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
        },
    )


class BuildExtWithFallback(build_ext):
    def build_extensions(self):
        try:
            super().build_extensions()
        except Exception as e:
            import warnings

            warnings.warn(
                f"Cython编译失败，将使用纯Python回退实现: {e}\n提示: 安装C编译器(如gcc/MSVC)或Cython可启用加速",
                stacklevel=2,
            )
            self.extensions = []


setup(
    ext_modules=get_extensions(),
    cmdclass={"build_ext": BuildExtWithFallback},
)
