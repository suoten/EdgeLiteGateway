"""ONVIF视频设备驱动 - 设备发现/RTSP流/PTZ云台控制"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from edgelite.constants import _ONVIF_MULTICAST_PORT, _ONVIF_MULTICAST_TTL
from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class OnvifDriver(DriverPlugin):
    """ONVIF视频设备驱动

    配置参数:
        ip: 设备IP地址
        port: ONVIF端口 (默认80)
        username: 用户名
        password: 密码
        wsdl_dir: WSDL文件目录 (> (可选)
    """

    plugin_name = "onvif"
    plugin_version = "1.0.0"
    supported_protocols = ["onvif"]
    config_schema = {
        "description": "ONVIF video device protocol, supports device discovery/RTSP stream/PTZ control",  # FIXED: 原问题-中文硬编码description
        "fields": [
            {"name": "ip", "type": "string", "label": "IP Address", "description": "ONVIF device IP address", "default": "", "required": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "port", "type": "integer", "label": "Port", "description": "ONVIF service port, default 80", "default": 80},  # FIXED: 原问题-中文硬编码label/description
            {"name": "username", "type": "string", "label": "Username", "description": "Device authentication username", "default": "admin"},  # FIXED: 原问题-中文硬编码label/description
            {"name": "password", "type": "string", "label": "Password", "description": "Device authentication password", "secret": True},  # FIXED: 原问题-中文硬编码label/description
        ],
    }

    def __init__(self):
        self._running = False
        self._config: dict = {}
        self._cam: Any = None
        self._media: Any = None
        self._ptz: Any = None
        self._lock = asyncio.Lock()
        self._devices: dict[str, dict] = {}

    async def start(self, config: dict) -> None:
        self._config = config
        ip = config.get("ip", "")
        if not ip:
            raise ValueError("ONVIF驱动配置缺少ip参数")

        try:
            self._cam = await asyncio.to_thread(self._create_camera, config)
            self._running = True
            logger.info("ONVIF驱动启动: %s", ip)
        except ImportError:
            raise ImportError("python-onvif-zeep未安装，请执行: pip install onvif-zeep") from None
        except Exception as e:
            logger.error("ONVIF驱动启动失败: %s - %s", ip, e)
            raise

    @staticmethod
    def _create_camera(config: dict) -> Any:
        """同步创建ONVIF摄像头连接"""
        from onvif import ONVIFCamera

        ip = config.get("ip", "")
        port = int(config.get("port", 80))
        username = config.get("username", "admin")
        password = config.get("password", "")
        wsdl_dir = config.get("wsdl_dir")

        kwargs = {}
        if wsdl_dir:
            kwargs["wsdl_dir"] = wsdl_dir

        cam = ONVIFCamera(ip, port, username, password, **kwargs)
        return cam

    async def stop(self) -> None:
        self._running = False
        if self._cam is not None:
            try:
                await asyncio.to_thread(self._close_camera)
            except Exception as e:
                logger.warning("ONVIF关闭连接异常: %s", e)
        self._cam = None
        self._media = None
        self._ptz = None
        logger.info("ONVIF驱动已停止")

    def _close_camera(self) -> None:
        if (
            self._cam
            and hasattr(self._cam, "devicemgmt")
            and hasattr(self._cam.devicemgmt, "soap_client")
        ):
            try:
                self._cam.devicemgmt.soap_client.get_transport().close()
            except Exception as e:
                logger.debug("ONVIF关闭传输失败: %s", e)

    def _ensure_media(self) -> Any:
        """确保media服务已创建"""
        if self._media is None and self._cam is not None:
            self._media = self._cam.create_media_service()
        return self._media

    def _ensure_ptz(self) -> Any:
        """确保PTZ服务已创建"""
        if self._ptz is None and self._cam is not None:
            self._ptz = self._cam.create_ptz_service()
        return self._ptz

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加ONVIF设备，保存配置和PTZ/流映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        logger.info("ONVIF设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        """WS-Discovery发现ONVIF设备"""
        try:
            import socket
            from urllib.parse import urlparse

            from onvif import ONVIFCamera
        except ImportError:
            logger.error("onvif-zeep未安装，无法发现设备")
            return []

        try:
            devices = await asyncio.to_thread(self._ws_discover)
            return devices
        except Exception as e:
            logger.error("ONVIF设备发现失败: %s", e)
            return []

    @staticmethod
    def _ws_discover() -> list[dict]:
        """同步WS-Discovery"""
        import socket

        multicast_group = "239.255.255.250"
        multicast_port = _ONVIF_MULTICAST_PORT  # FIXED: 原问题-multicast_port=3702魔法数字

        probe_msg = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
            "<s:Header><d:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</d:Action>"
            "<d:MessageID>uuid:edgelite-onvif-probe</d:MessageID>"
            "<d:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</d:To>"
            "</s:Header>"
            "<s:Body><d:Probe/><d:Types>dn:NetworkVideoTransmitter</d:Types></s:Body>"
            "</s:Envelope>"
        )

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, _ONVIF_MULTICAST_TTL)  # FIXED: 原问题-TTL=4魔法数字

        devices = []
        seen = set()

        try:
            sock.sendto(probe_msg.encode(), (multicast_group, multicast_port))
            while True:
                try:
                    data, addr = sock.recvfrom(8192)
                    response = data.decode("utf-8", errors="replace")
                    if "onvif" in response.lower() and addr[0] not in seen:
                        seen.add(addr[0])
                        devices.append(
                            {
                                "device_id": addr[0],
                                "name": f"ONVIF Device @ {addr[0]}",
                                "ip": addr[0],
                                "protocol": "onvif",
                                "details": {"port": addr[1] if len(addr) > 1 else 80},  # FIXED-P2: 发现设备端口硬编码80，改为使用实际响应端口
                            }
                        )
                except TimeoutError:
                    break
        finally:
            sock.close()

        return devices

    async def _get_rtsp_url(self, profile_token: str = "") -> str:
        """获取RTSP流地址"""
        media = self._ensure_media()
        if not media:
            return ""

        def _sync_get_url() -> str:
            nonlocal profile_token
            if not profile_token:
                profiles = media.GetProfiles()
                if not profiles:
                    return ""
                profile_token = profiles[0].token

            stream_setup = {
                "Stream": "RTP-Unicast",
                "Transport": {"Protocol": "RTSP"},
            }
            resp = media.GetStreamUri(
                {
                    "StreamSetup": stream_setup,
                    "ProfileToken": profile_token,
                }
            )
            return resp.Uri if hasattr(resp, "Uri") else str(resp)

        try:
            return await asyncio.to_thread(_sync_get_url)
        except Exception as e:
            logger.error("ONVIF获取RTSP地址失败: %s", e)
            return ""

    async def _ptz_continuous_move(
        self, profile_token: str, pan: float = 0.0, tilt: float = 0.0, zoom: float = 0.0
    ) -> bool:
        """PTZ连续移动"""
        ptz = self._ensure_ptz()
        if not ptz:
            return False

        def _sync_move() -> None:
            velocity = {
                "PanTilt": {"x": pan, "y": tilt},
                "Zoom": {"x": zoom},
            }
            ptz.ContinuousMove({"ProfileToken": profile_token, "Velocity": velocity})

        try:
            await asyncio.to_thread(_sync_move)
            return True
        except Exception as e:
            logger.error("ONVIF PTZ连续移动失败: %s", e)
            return False

    async def _ptz_absolute_move(
        self, profile_token: str, pan: float, tilt: float, zoom: float = 0.0
    ) -> bool:
        """PTZ绝对定位"""
        ptz = self._ensure_ptz()
        if not ptz:
            return False

        def _sync_move() -> None:
            position = {
                "PanTilt": {"x": pan, "y": tilt},
                "Zoom": {"x": zoom},
            }
            ptz.AbsoluteMove({"ProfileToken": profile_token, "Position": position})

        try:
            await asyncio.to_thread(_sync_move)
            return True
        except Exception as e:
            logger.error("ONVIF PTZ绝对定位失败: %s", e)
            return False

    async def _ptz_relative_move(
        self, profile_token: str, pan: float, tilt: float, zoom: float = 0.0
    ) -> bool:
        """PTZ相对移动"""
        ptz = self._ensure_ptz()
        if not ptz:
            return False

        def _sync_move() -> None:
            translation = {
                "PanTilt": {"x": pan, "y": tilt},
                "Zoom": {"x": zoom},
            }
            ptz.RelativeMove({"ProfileToken": profile_token, "Translation": translation})

        try:
            await asyncio.to_thread(_sync_move)
            return True
        except Exception as e:
            logger.error("ONVIF PTZ相对移动失败: %s", e)
            return False

    async def _ptz_stop(self, profile_token: str) -> bool:
        """停止PTZ移动"""
        ptz = self._ensure_ptz()
        if not ptz:
            return False

        try:
            await asyncio.to_thread(ptz.Stop, ProfileToken=profile_token, PanTilt=True, Zoom=True)
            return True
        except Exception as e:
            logger.error("ONVIF PTZ停止失败: %s", e)
            return False

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取ONVIF测点值

        测点地址格式:
            "rtsp" / "rtsp:profile_token" - RTSP流地址
            "profiles" - 获取所有配置文件
            "ptz_status:profile_token" - PTZ状态
        """
        if not self._running or not self._cam:
            return {}

        result = {}
        for point in points:
            try:
                if point == "rtsp":
                    result[point] = await self._get_rtsp_url()
                elif point.startswith("rtsp:"):
                    token = point.split(":", 1)[1]
                    result[point] = await self._get_rtsp_url(token)
                elif point == "profiles":
                    media = self._ensure_media()
                    if media:
                        profiles = await asyncio.to_thread(media.GetProfiles)
                        result[point] = [
                            {"token": p.token, "name": p.Name if hasattr(p, "Name") else ""}
                            for p in (profiles or [])
                        ]
                    else:
                        result[point] = []
                elif point.startswith("ptz_status:"):
                    token = point.split(":", 1)[1]
                    ptz = self._ensure_ptz()
                    if ptz:
                        status = await asyncio.to_thread(ptz.GetStatus, {"ProfileToken": token})
                        result[point] = status
                    else:
                        result[point] = None
                else:
                    result[point] = None
            except Exception as e:
                logger.warning("ONVIF读取失败 %s: %s", point, e)
                result[point] = None

        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入PTZ控制命令

        地址格式:
            "ptz_continuous:profile_token" - value为{pan, tilt, zoom}字典
            "ptz_absolute:profile_token" - value为{pan, tilt, zoom}字典
            "ptz_relative:profile_token" - value为{pan, tilt, zoom}字典
            "ptz_stop:profile_token" - value无意义
        """
        if not self._running or not self._cam:
            return False

        try:
            if point.startswith("ptz_continuous:"):
                token = point.split(":", 1)[1]
                v = value if isinstance(value, dict) else {}
                return await self._ptz_continuous_move(
                    token, v.get("pan", 0.0), v.get("tilt", 0.0), v.get("zoom", 0.0)
                )
            elif point.startswith("ptz_absolute:"):
                token = point.split(":", 1)[1]
                v = value if isinstance(value, dict) else {}
                return await self._ptz_absolute_move(
                    token, v.get("pan", 0.0), v.get("tilt", 0.0), v.get("zoom", 0.0)
                )
            elif point.startswith("ptz_relative:"):
                token = point.split(":", 1)[1]
                v = value if isinstance(value, dict) else {}
                return await self._ptz_relative_move(
                    token, v.get("pan", 0.0), v.get("tilt", 0.0), v.get("zoom", 0.0)
                )
            elif point.startswith("ptz_stop:"):
                token = point.split(":", 1)[1]
                return await self._ptz_stop(token)
            else:
                logger.error("ONVIF写入地址格式无效: %s", point)
                return False
        except Exception as e:
            logger.error("ONVIF写入失败 %s: %s", point, e)
            return False

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("ONVIF device removed: %s", device_id)
