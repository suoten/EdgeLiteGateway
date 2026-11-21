"""驱动接入层 - EdgeLiteGateway 协议驱动包

本包按「一个协议一个模块」组织驱动实现（如 ``modbus_tcp.py``、
``opcua.py``、``s7.py`` 等），核心组件：

- :mod:`edgelite.drivers.base` —— 驱动抽象基类 ``DriverPlugin``，
  所有驱动必须继承它并实现 ``start/stop/read_points/write_point``；
- :mod:`edgelite.drivers.registry` —— 驱动注册表 ``DriverRegistry``，
  负责发现、注册、查询全部驱动，**新驱动的接入方法见该模块顶部注释**
  （含分步指南与示例代码）。

快速上手::

    from edgelite.drivers.registry import get_driver_registry

    registry = get_driver_registry()          # 全局单例，首次调用自动发现驱动
    registry.get_supported_protocols()        # 查看已支持的驱动
    driver_cls = registry.get_driver_class("modbus_tcp")  # 按协议键取驱动类
"""
