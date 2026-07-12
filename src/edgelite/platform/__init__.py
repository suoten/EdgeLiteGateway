from edgelite.platform.base import PlatformHandler

# FIXED: 原问题-顶层硬导入 edgelite.platform.north_base（模块不存在）导致整个 platform
# 包导入失败，连带使 platform_service / api.platforms 等北向集成功能被 app.py 的可选路由
# 加载器静默跳过（ImportError 被捕获并 warning）。将 BaseNorthAdapter 改为懒加载，与
# 各 Handler 子类的懒加载模式保持一致。north_base 模块缺失属已知问题，待北向适配器重建。
__all__ = [
    "PlatformHandler",
    "BaseNorthAdapter",
    "IoTSharpHandler",
    "ThingsBoardHandler",
    "HuaweiIoTDAHandler",
    "ThingsCloudHandler",
    "ThingsPanelHandler",
    "CustomMqttHandler",
]


def __getattr__(name: str):
    if name in __all__:
        try:
            if name == "BaseNorthAdapter":
                from edgelite.platform.north_base import BaseNorthAdapter
                return BaseNorthAdapter
            elif name == "IoTSharpHandler":
                from edgelite.platform.iotsharp import IoTSharpHandler
                return IoTSharpHandler
            elif name == "ThingsBoardHandler":
                from edgelite.platform.thingsboard import ThingsBoardHandler
                return ThingsBoardHandler
            elif name == "HuaweiIoTDAHandler":
                from edgelite.platform.huawei_iotda import HuaweiIoTDAHandler
                return HuaweiIoTDAHandler
            elif name == "ThingsCloudHandler":
                from edgelite.platform.thingscloud import ThingsCloudHandler
                return ThingsCloudHandler
            elif name == "ThingsPanelHandler":
                from edgelite.platform.thingspanel import ThingsPanelHandler
                return ThingsPanelHandler
            elif name == "CustomMqttHandler":
                from edgelite.platform.custom_mqtt import CustomMqttHandler
                return CustomMqttHandler
        except ImportError:
            return None
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
