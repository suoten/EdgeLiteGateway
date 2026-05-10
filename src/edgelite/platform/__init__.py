"""北向平台对接模块"""

from edgelite.platform.base import PlatformHandler

__all__ = [
    "PlatformHandler",
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
            if name == "IoTSharpHandler":
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
