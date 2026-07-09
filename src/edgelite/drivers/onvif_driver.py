"""ONVIF视频设备驱动 - 设备发现/RTSP流/PTZ云台控制"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import math
import time
from collections import deque
from datetime import UTC, datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

try:
    import defusedxml.ElementTree as _ET
    _XML_SAFE_PARSER = True
except ImportError:
    import xml.etree.ElementTree as _ET
    _XML_SAFE_PARSER = False

import contextlib

from edgelite.api.debug import record_packet
from edgelite.constants import _ONVIF_MULTICAST_PORT, _ONVIF_MULTICAST_TTL
from edgelite.drivers.base import DriverCapabilities, DriverPlugin, PointValue
from edgelite.services.i18n import t as _t

logger = logging.getLogger(__name__)


class _NonceCountingWsse:
    def __init__(self, username: str, password: str, counter_func):
        self._username = username
        self._password = password
        self._counter_func = counter_func
        self._token_cls = None
        try:
            from zeep.wsse.username import UsernameToken
            self._token_cls = UsernameToken
        except ImportError as e:
            logger.debug("[onvif] _init__ failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

    def apply(self, envelope, headers):
        if self._token_cls is None:
            return envelope, headers
        counter = self._counter_func()
        raw_nonce = f"edgelite-{counter}-{time.time()}".encode()
        nonce = hashlib.sha256(raw_nonce).digest()
        token = self._token_cls(
            self._username, self._password,
            use_digest=True, nonce=nonce,
        )
        return token.apply(envelope, headers)

    def verify(self, envelope, headers):
        return envelope, headers


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
    supported_protocols = ("onvif",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _required_dependencies: tuple[str, ...] = ("onvif",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    config_schema = {
        "description": "ONVIF video device protocol, supports device discovery/RTSP stream/PTZ control",  # FIXED: 原问题-中文硬编码description
        "required": ["ip", "username", "password"],
        "properties": {"ip": {"type": "string", "description": "ONVIF device IP", "format": "ipv4"}, "port": {"type": "integer", "description": "ONVIF service port", "minimum": 1, "maximum": 65535}, "username": {"type": "string", "description": "Device auth username"}, "password": {"type": "string", "description": "Device auth password"}},
        "fields": [
            {"name": "ip", "type": "string", "label": "IP Address", "description": "ONVIF device IP address", "default": "", "required": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "port", "type": "integer", "label": "Port", "description": "ONVIF service port, default 80", "default": 80, "min": 1, "max": 65535},  # FIXED: 原问题-中文硬编码label/description
            {"name": "username", "type": "string", "label": "Username", "description": "Device authentication username", "default": ""},  # FIXED-P2: 默认用户名从admin改为空，防止使用默认凭据连接设备
            {"name": "password", "type": "string", "label": "Password", "description": "Device authentication password", "secret": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "auth_type", "type": "string", "label": "Auth Type", "description": "Authentication method: Basic or Digest", "default": "Basic", "options": ["Basic", "Digest"]},
            {"name": "timeout", "type": "number", "label": "Timeout (s)", "description": "ONVIF communication timeout in seconds", "default": 10.0, "min": 1, "max": 60},
            {"name": "connect_timeout", "type": "number", "label": "Connect Timeout (s)", "description": "SOAP connection timeout in seconds", "default": 5.0, "min": 1, "max": 30},
            {"name": "read_timeout", "type": "number", "label": "Read Timeout (s)", "description": "SOAP read/response timeout in seconds", "default": 10.0, "min": 1, "max": 60},
            {"name": "ptz_timeout", "type": "number", "label": "PTZ Timeout (s)", "description": "PTZ operation timeout in seconds", "default": 5.0, "min": 1, "max": 30},
            {"name": "wsdl_dir", "type": "string", "label": "WSDL Directory", "description": "Path to ONVIF WSDL files (optional)", "default": ""},
            {"name": "allow_private_rtsp", "type": "boolean", "label": "Allow Private RTSP", "description": "Allow RTSP URLs pointing to private/internal IP addresses", "default": False},
            {"name": "deadband", "type": "object", "label": "Deadband", "description": "Deadband filter config: {type, threshold}", "default": None},
            {"name": "scaling", "type": "object", "label": "Scaling", "description": "Linear scaling config: {ratio, offset}", "default": None},
            {"name": "clamp", "type": "object", "label": "Clamp", "description": "Clamp range config: {min, max}", "default": None},
        ],
    }

    experimental = True
    capabilities = DriverCapabilities(discover=False, read=True, write=True, subscribe=False, batch_read=False, batch_write=False)
    constraints = ()  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple

    _MAX_RECONNECT_ATTEMPTS = 3
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0
    _SNAPSHOT_CACHE_TTL = 300.0
    _PTZ_MIN_INTERVAL = 0.5

    def __init__(self):
        super().__init__()  # FIXED-P0: 基类属性未初始化
        self._running = False
        self._config: dict = {}
        self._lock = asyncio.Lock()
        # 设备ID → 相机实例（每个设备独立，避免状态覆盖）
        self._cams: dict[str, Any] = {}
        # 设备ID → media 服务实例
        self._medias: dict[str, Any] = {}
        # 设备ID → PTZ 服务实例
        self._ptzs: dict[str, Any] = {}
        self._devices: dict[str, dict] = {}
        self._reconnect_counts: dict[str, int] = {}
        self._reconnect_delays: dict[str, float] = {}
        self._reconnect_locks: dict[str, asyncio.Lock] = {}  # FIXED-P1: 设备级重连锁，防止并发重连
        # _health_stats inherited from base class (dict[str, DriverHealthStats])  # FIXED-P2: 删除遮蔽基类的覆盖初始化
        self._last_values: dict[str, Any] = {}
        self._auth_failed: dict[str, bool] = {}
        # FIXED-P0: 认证失败重试冷却时间戳，允许定时重试认证而非永久阻止
        self._auth_failed_since: dict[str, float] = {}
        self._AUTH_RETRY_COOLDOWN = 300.0  # FIXED-P0: 认证失败后5分钟允许重试（原为局部变量，访问时AttributeError）
        self._profiles_cache: dict[str, list[dict]] = {}
        self._snapshot_uri_cache: dict[str, tuple[str, float]] = {}
        self._ptz_limits_cache: dict[str, dict[str, tuple[float, float]]] = {}
        self._last_ptz_time: dict[str, float] = {}
        self._ptz_audit_log: deque = deque(maxlen=1000)  # FIXED-P2: ONVIF-R03 改为deque自动淘汰
        self._nonce_counters: dict[str, int] = {}
        self._watchdog_task: asyncio.Task | None = None

    def _log_error(self, device_id: str, error_code: str, message: str) -> None:
        i18n_msg = _t(error_code) if error_code.startswith("ERR_") else error_code
        logger.error("[onvif] device=%s code=%s %s", device_id, error_code, f"i18n={i18n_msg} {message}")

    @staticmethod
    def _classify_soap_error(exc: Exception) -> str:
        msg = str(exc).lower()
        if any(k in msg for k in ("notauthorized", "ter:notauthorized", "env:sender", "401", "access denied")):
            return "ERR_ONVIF_AUTH_FAILED"
        if any(k in msg for k in ("actionnotsupported", "ter:actionnotsupported", "not supported", "unsupported")):
            return "ERR_ONVIF_NOT_SUPPORTED"
        if any(k in msg for k in ("timeout", "timed out", "connectionerror", "connection refused", "errno")):
            return "ERR_ONVIF_CONN_TIMEOUT"
        return "ERR_ONVIF_SOAP_ERROR"

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        return OnvifDriver._classify_soap_error(exc) == "ERR_ONVIF_AUTH_FAILED"

    def _get_read_timeout(self) -> float:
        return float(self._config.get("read_timeout", self._config.get("timeout", 10.0)))

    def _get_ptz_timeout(self) -> float:
        return float(self._config.get("ptz_timeout", self._config.get("timeout", 10.0)))

    async def _detect_profiles(self, device_id: str) -> list[dict]:
        if device_id in self._profiles_cache:
            return self._profiles_cache[device_id]
        media = self._ensure_media(device_id)
        if not media:
            return []
        try:
            profiles = await asyncio.wait_for(asyncio.to_thread(media.GetProfiles), timeout=self._get_read_timeout())
            result = []
            for p in (profiles or []):
                result.append({"token": p.token, "name": getattr(p, "Name", "")})
            self._profiles_cache[device_id] = result
            return result
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_RTSP_FAILED", f"profile detect failed: {e}")
            return []

    def _resolve_profile_token(self, device_id: str, profile_token: str) -> str:
        if profile_token:
            return profile_token
        profiles = self._profiles_cache.get(device_id, [])
        if profiles:
            return profiles[0]["token"]
        return ""

    @staticmethod
    def _parse_ptz_status(status: Any) -> dict[str, Any] | None:
        if status is None:
            return None
        try:
            result: dict[str, Any] = {}
            if hasattr(status, "Position"):
                pos = status.Position
                if hasattr(pos, "PanTilt"):
                    result["pan"] = float(pos.PanTilt.x) if hasattr(pos.PanTilt, "x") else 0.0
                    result["tilt"] = float(pos.PanTilt.y) if hasattr(pos.PanTilt, "y") else 0.0
                if hasattr(pos, "Zoom"):
                    result["zoom"] = float(pos.Zoom.x) if hasattr(pos.Zoom, "x") else 0.0
            if hasattr(status, "MoveStatus"):
                ms = status.MoveStatus
                if hasattr(ms, "PanTilt"):
                    result["pan_tilt_moving"] = str(ms.PanTilt)
                if hasattr(ms, "Zoom"):
                    result["zoom_moving"] = str(ms.Zoom)
            if hasattr(status, "UtcTime"):
                result["utc_time"] = str(status.UtcTime)
            return result if result else None
        except Exception as e:
            logger.warning("ONVIF get_status failed: %s", e)  # FIXED-P1: 原问题-异常返回None无日志
            return None

    _MAX_SNAPSHOT_SIZE = 10 * 1024 * 1024  # FIXED-P2: 快照最大10MB，防止OOM

    async def _download_snapshot(self, device_id: str, uri: str) -> bytes | None:
        if not uri:
            return None
        # FIXED-P0: 校验快照URI不指向私有IP，防止SSRF
        try:
            import ipaddress
            from urllib.parse import urlparse
            parsed = urlparse(uri)
            hostname = parsed.hostname or ""
            ip_obj = ipaddress.ip_address(hostname)
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved:
                self._log_error(device_id, "ERR_ONVIF_SNAPSHOT_SSRF_BLOCKED", f"Snapshot URI points to private IP: {hostname}")
                return None
        except ValueError:
            # FIXED-P0: hostname不是IP格式（可能是域名），异步解析DNS后检查解析结果是否为私有IP
            # 之前：使用socket.getaddrinfo()同步DNS解析，DNS慢时阻塞整个事件循环
            # 之后：使用asyncio.to_thread()将DNS解析移入线程池，不阻塞事件循环
            try:
                import socket as _socket
                def _resolve_dns(hostname):
                    return _socket.getaddrinfo(hostname, None, _socket.AF_INET, _socket.SOCK_STREAM)
                resolved_ips = await asyncio.to_thread(_resolve_dns, hostname)
                for _, _, _, _, addr in resolved_ips:
                    resolved_ip = ipaddress.ip_address(addr[0])
                    if resolved_ip.is_private or resolved_ip.is_loopback or resolved_ip.is_link_local or resolved_ip.is_reserved:
                        self._log_error(device_id, "ERR_ONVIF_SNAPSHOT_SSRF_BLOCKED", f"Snapshot URI domain {hostname} resolves to private IP: {addr[0]}")
                        return None
            except Exception:
                # FIXED-P2: DNS解析失败时拒绝请求，防止DNS rebinding攻击绕过SSRF防护
                self._log_error(device_id, "ERR_ONVIF_SNAPSHOT_DNS_FAILED", f"Snapshot URI domain {hostname} DNS resolution failed, request rejected")
                return None
        config = self._devices.get(device_id, {}).get("config", self._config)
        username = config.get("username", "")  # FIXED-P1: 默认空字符串与config_schema一致，避免使用硬编码凭据
        password = config.get("password", "")
        auth = httpx.DigestAuth(username, password) if config.get("auth_type") == "Digest" else (username, password) if username else None
        try:
            async with httpx.AsyncClient(timeout=self._get_read_timeout(), verify=self._config.get("verify_ssl", True), follow_redirects=False) as client:  # FIXED-P0: 禁止重定向防止绕过IP校验
                resp = await client.get(uri, auth=auth)
                resp.raise_for_status()
                if len(resp.content) > self._MAX_SNAPSHOT_SIZE:
                    self._log_error(device_id, "ERR_ONVIF_SNAPSHOT_TOO_LARGE", f"Snapshot size {len(resp.content)} exceeds limit {self._MAX_SNAPSHOT_SIZE}")
                    return None
                return resp.content
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_SNAPSHOT_DOWNLOAD_FAILED", str(e))
            return None

    async def _get_ptz_limits(self, device_id: str, profile_token: str) -> dict[str, tuple[float, float]]:
        cache_key = f"{device_id}:{profile_token}"
        if cache_key in self._ptz_limits_cache:
            return self._ptz_limits_cache[cache_key]
        ptz = self._ensure_ptz(device_id)
        if not ptz:
            return {}
        try:
            configs = await asyncio.wait_for(asyncio.to_thread(ptz.GetConfigurations), timeout=self._get_read_timeout())
            for cfg in (configs if isinstance(configs, list) else [configs]):
                token = getattr(cfg, "token", "")
                if token != profile_token:
                    continue
                limits: dict[str, tuple[float, float]] = {}
                sp = getattr(cfg, "SpaceLimits", None)
                if sp:
                    apt = getattr(sp, "AbsolutePanTiltPositionSpace", None)
                    if apt:
                        ur = getattr(apt, "URIRange", None)
                        if ur:
                            limits["pan"] = (float(getattr(ur, "XMin", -1.0)), float(getattr(ur, "XMax", 1.0)))
                            limits["tilt"] = (float(getattr(ur, "YMin", -1.0)), float(getattr(ur, "YMax", 1.0)))
                    az = getattr(sp, "AbsoluteZoomPositionSpace", None)
                    if az:
                        ur = getattr(az, "URIRange", None)
                        if ur:
                            limits["zoom"] = (float(getattr(ur, "XMin", 0.0)), float(getattr(ur, "XMax", 1.0)))
                self._ptz_limits_cache[cache_key] = limits
                if len(self._ptz_limits_cache) > 10000:  # FIXED-P2: 缓存容量限制
                    for k in list(self._ptz_limits_cache.keys())[:2000]:
                        self._ptz_limits_cache.pop(k, None)
                return limits
            return {}
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_SOAP_ERROR", f"GetConfigurations failed: {e}")
            return {}

    def _validate_ptz_range(self, limits: dict[str, tuple[float, float]], pan: float, tilt: float, zoom: float) -> str | None:
        # 协议边界校验: NaN 与任何数比较均返回 False 会绕过范围校验，需显式拦截
        if any(math.isnan(v) or math.isinf(v) for v in (pan, tilt, zoom)):
            return "ERR_ONVIF_PTZ_INVALID_VALUE"
        if "pan" in limits:
            lo, hi = limits["pan"]
            if pan < lo or pan > hi:
                return "ERR_ONVIF_PTZ_OUT_OF_RANGE"
        if "tilt" in limits:
            lo, hi = limits["tilt"]
            if tilt < lo or tilt > hi:
                return "ERR_ONVIF_PTZ_OUT_OF_RANGE"
        if "zoom" in limits:
            lo, hi = limits["zoom"]
            if zoom < lo or zoom > hi:
                return "ERR_ONVIF_PTZ_OUT_OF_RANGE"
        return None

    def _check_ptz_rate_limit(self, device_id: str) -> bool:
        now = time.monotonic()
        last = self._last_ptz_time.get(device_id, 0.0)
        if (now - last) < self._PTZ_MIN_INTERVAL:
            return False
        self._last_ptz_time[device_id] = now
        return True

    def _audit_ptz(self, device_id: str, action: str, pan: float, tilt: float, zoom: float, result: bool) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "device_id": device_id,
            "ptz_action": action,
            "pan": pan,
            "tilt": tilt,
            "zoom": zoom,
            "result": result,
        }
        self._ptz_audit_log.append(entry)

        logger.info(
            "[onvif] audit device=%s action=%s pan=%.3f tilt=%.3f zoom=%.3f result=%s",
            device_id, action, pan, tilt, zoom, result,
        )

    async def start(self, config: dict) -> None:
        self._config = config
        ip = config.get("ip", "")
        if not ip:
            raise ValueError("ONVIF驱动配置缺少ip参数")

        try:
            # 启动时的 config 作为默认设备，建立独立相机实例
            cam = await asyncio.wait_for(asyncio.to_thread(self._create_camera, config), timeout=30.0)  # FIXED-P1: 添加超时保护，防止ONVIF相机创建无限阻塞
            self._setup_digest_nonce(cam, "_default")
            self._cams["_default"] = cam
            self._medias["_default"] = None
            self._ptzs["_default"] = None
            self._running = True
            self._reconnect_counts["_default"] = 0
            self._reconnect_delays["_default"] = self._RECONNECT_BASE_DELAY
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
            logger.info("ONVIF驱动启动: %s", ip)
        except ImportError:
            raise ImportError("python-onvif-zeep未安装，请执行: pip install onvif-zeep") from None
        except Exception as e:
            if self._is_auth_error(e):
                self._log_error("_default", "ERR_ONVIF_AUTH_FAILED", str(e))
                self._auth_failed["_default"] = True
                raise
            self._log_error("_default", "ERR_ONVIF_START_FAILED", str(e))
            raise

    @staticmethod
    def _create_camera(config: dict) -> Any:
        from onvif import ONVIFCamera

        ip = config.get("ip", "")
        port = int(config.get("port", 80))
        username = config.get("username", "")  # FIXED-P1: 默认空字符串与config_schema一致，避免使用硬编码凭据
        password = config.get("password", "")
        wsdl_dir = config.get("wsdl_dir")
        auth_type = config.get("auth_type", "Basic")
        connect_timeout = float(config.get("connect_timeout", config.get("timeout", 10.0)))

        kwargs = {}
        if wsdl_dir:
            kwargs["wsdl_dir"] = wsdl_dir
        if auth_type == "Digest":
            kwargs["digest"] = True
        kwargs["timeout"] = connect_timeout

        cam = ONVIFCamera(ip, port, username, password, **kwargs)
        return cam

    def _increment_nonce(self, device_id: str) -> int:
        self._nonce_counters[device_id] = self._nonce_counters.get(device_id, 0) + 1
        return self._nonce_counters[device_id]

    def _setup_digest_nonce(self, cam: Any, device_id: str) -> None:
        if device_id not in self._nonce_counters:
            self._nonce_counters[device_id] = 0
        try:
            username = getattr(cam, "user", "") or ""
            password = getattr(cam, "password", "") or ""
            def counter_func(did=device_id):
                return self._increment_nonce(did)
            wsse = _NonceCountingWsse(username, password, counter_func)
            cam.wsse = wsse
            for svc_name in ("devicemgmt",):
                svc = getattr(cam, svc_name, None)
                if svc is not None:
                    if hasattr(svc, "soap_client"):
                        svc.soap_client.wsse = wsse
                    elif hasattr(svc, "zeep_client"):
                        svc.zeep_client.wsse = wsse
        except Exception as e:
            logger.debug("[onvif] digest nonce setup skipped: %s", e)

    async def stop(self) -> None:
        try:
            if self._watchdog_task is not None:
                self._watchdog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._watchdog_task
                self._watchdog_task = None
            if self._cams:
                for device_id in list(self._cams.keys()):
                    cam = self._cams.get(device_id)
                    if cam and hasattr(cam, "devicemgmt") and hasattr(cam.devicemgmt, "soap_client"):
                        try:
                            soap_client = cam.devicemgmt.soap_client
                            if soap_client is not None and hasattr(soap_client, "get_transport"):  # FIXED-P2: soap_client为None时跳过
                                transport = soap_client.get_transport()
                                if transport is not None:
                                    await asyncio.to_thread(transport.close)
                        except Exception as e:
                            logger.debug("ONVIF关闭传输失败: %s", e)
            self._cams.clear()
            self._medias.clear()
            self._ptzs.clear()
            # FIXED-P2: 清理残留状态，防止stop后restart行为异常
            self._devices.clear()
            self._reconnect_counts.clear()
            self._reconnect_delays.clear()
            # R5-G-02: 清理设备级重连锁和认证冷却时间戳，防止字典无界增长
            self._reconnect_locks.clear()
            self._auth_failed_since.clear()
            self._last_values.clear()
            self._auth_failed.clear()
            self._profiles_cache.clear()
            self._snapshot_uri_cache.clear()
            self._ptz_limits_cache.clear()
            self._last_ptz_time.clear()
            self._ptz_audit_log.clear()
            self._nonce_counters.clear()
        finally:
            self._running = False
            logger.info("ONVIF驱动已停止")
            # FIXED-P1: ONVIF-R01 调用基类stop()确保_shutdown_executor和_cancel_background_tasks执行
            await super().stop()

    def _ensure_media(self, device_id: str) -> Any:
        """确保指定设备的media服务已创建"""
        if self._medias.get(device_id) is None:
            cam = self._cams.get(device_id)
            if cam is not None:
                self._medias[device_id] = cam.create_media_service()
        return self._medias.get(device_id)

    def _ensure_ptz(self, device_id: str) -> Any:
        """确保指定设备的PTZ服务已创建"""
        if self._ptzs.get(device_id) is None:
            cam = self._cams.get(device_id)
            if cam is not None:
                self._ptzs[device_id] = cam.create_ptz_service()
        return self._ptzs.get(device_id)

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加ONVIF设备，为每台设备创建独立的相机实例"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        try:
            cam = await asyncio.wait_for(asyncio.to_thread(self._create_camera, config), timeout=30.0)  # FIXED-P1: 添加超时保护，防止ONVIF相机创建无限阻塞
            self._setup_digest_nonce(cam, device_id)
            self._cams[device_id] = cam
            self._medias[device_id] = None
            self._ptzs[device_id] = None
            logger.info("ONVIF设备已添加: %s (%d测点, 相机实例已创建)", device_id, len(points))
        except Exception as e:
            if self._is_auth_error(e):
                self._log_error(device_id, "ERR_ONVIF_AUTH_FAILED", str(e))
                self._auth_failed[device_id] = True
                self._auth_failed_since[device_id] = time.monotonic()  # FIXED-P0: 记录认证失败时间
            else:
                self._log_error(device_id, "ERR_ONVIF_CAMERA_INIT_FAILED", str(e))
            # 允许设备添加成功但相机实例创建失败，后续操作会检测到cam缺失
            self._cams[device_id] = None
            self._medias[device_id] = None
            self._ptzs[device_id] = None

    async def discover_devices(self, config: dict) -> list[dict]:
        """WS-Discovery发现ONVIF设备，超时返回空设备列表"""
        try:
            import socket
            from urllib.parse import urlparse

            from onvif import ONVIFCamera
        except ImportError:
            logger.error("[onvif] onvif-zeep未安装，无法发现设备")
            return []

        try:
            discover_timeout = float(config.get("timeout", 5.0))
            devices = await asyncio.wait_for(
                asyncio.to_thread(self._ws_discover),
                timeout=discover_timeout,
            )
            return devices
        except TimeoutError:
            logger.warning("[onvif] WS-Discovery timed out, returning empty device list")
            return []
        except Exception as e:
            self._log_error("", "ERR_ONVIF_DISCOVER_FAILED", str(e))
            return []

    @staticmethod
    def _verify_onvif_fingerprint(response: str) -> bool:
        if not _XML_SAFE_PARSER and len(response) > 65536:
            logger.warning("WS-Discovery response too large, skipping")
            return False
        onvif_indicators = [
            "NetworkVideoTransmitter",
            "www.onvif.org",
            "onvif.org/ver10",
            "onvif.org/ver20",
            "dn:NetworkVideoTransmitter",
        ]
        response_lower = response.lower()
        matched = [ind for ind in onvif_indicators if ind.lower() in response_lower]
        found = len(matched)
        if found >= 2:
            logger.info("[onvif] WS-Discovery fingerprint verified: %d indicators matched: %s", found, matched)
            return True
        if found >= 1:
            logger.warning("[onvif] WS-Discovery fingerprint weak: only 1 indicator matched: %s, requiring XML validation", matched)
        try:
            root = _ET.fromstring(response)
            ns = {
                "s": "http://www.w3.org/2003/05/soap-envelope",
                "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
            }
            types_el = root.find(".//d:Types", ns)
            if types_el is not None and types_el.text:
                if "NetworkVideoTransmitter" in types_el.text or "Device" in types_el.text:
                    xml_matched = types_el.text.strip()
                    logger.info("[onvif] WS-Discovery fingerprint verified via XML Types: %s", xml_matched)
                    return True
        except Exception as e:
            logger.warning("[onvif] verify_onvif_fingerprint failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        return False

    @staticmethod
    def _safe_xml_parse(xml_string: str) -> Any:
        return _ET.fromstring(xml_string)

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
                        if not OnvifDriver._verify_onvif_fingerprint(response):
                            logger.warning("[onvif] WS-Discovery: suspicious response from %s, missing ONVIF fingerprint", addr[0])
                            continue
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

    async def _validate_rtsp_url(self, url: str, device_id: str) -> str:
        if not url:
            return ""
        if not url.startswith("rtsp://"):
            self._log_error(device_id, "ERR_ONVIF_RTSP_INVALID_SCHEME", f"url={url}")
            return ""
        config = self._devices.get(device_id, {}).get("config", self._config)
        allow_private = config.get("allow_private_rtsp", False)
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if not hostname:
                self._log_error(device_id, "ERR_ONVIF_RTSP_INVALID_HOST", f"url={url}")
                return ""
            if not allow_private:
                def _check_ip():
                    import socket as _socket
                    for family in (_socket.AF_INET, _socket.AF_INET6):
                        try:
                            resolved = _socket.getaddrinfo(hostname, None, family)
                            for addr_info in resolved:
                                ip_str = addr_info[4][0]
                                ip = ipaddress.ip_address(ip_str)
                                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                                    return ip_str
                        except (_socket.gaierror, ValueError) as e:
                            logger.warning("[onvif] check_ip failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    return None
                blocked_ip = await asyncio.to_thread(_check_ip)
                if blocked_ip:
                    self._log_error(device_id, "ERR_ONVIF_RTSP_PRIVATE_IP", f"host={hostname} ip={blocked_ip}")
                    return ""
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_RTSP_VALIDATION_FAILED", str(e))
            return ""
        return url

    async def _get_rtsp_url(self, device_id: str, profile_token: str = "") -> str:
        media = self._ensure_media(device_id)
        if not media:
            return ""

        resolved_token = self._resolve_profile_token(device_id, profile_token)
        if not resolved_token:
            return ""

        stream_setup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }

        def _sync_get_url(tok: str) -> str:
            resp = media.GetStreamUri(
                {"StreamSetup": stream_setup, "ProfileToken": tok}
            )
            return resp.Uri if hasattr(resp, "Uri") else str(resp)

        try:
            url = await asyncio.wait_for(asyncio.to_thread(_sync_get_url, resolved_token), timeout=self._get_read_timeout())
            return await self._validate_rtsp_url(url, device_id)
        except Exception as e:
            if not profile_token:
                profiles = self._profiles_cache.get(device_id, [])
                for alt in profiles[1:]:
                    try:
                        url = await asyncio.wait_for(asyncio.to_thread(_sync_get_url, alt["token"]), timeout=self._get_read_timeout())
                        validated = await self._validate_rtsp_url(url, device_id)
                        if validated:
                            return validated
                    except Exception as alt_err:
                        logger.debug("[onvif] device=%s alt profile %s get rtsp failed: %s", device_id, alt.get("token"), alt_err)
                        continue
            self._log_error(device_id, "ERR_ONVIF_RTSP_FAILED", str(e))
            return ""

    async def _ptz_continuous_move(
        self, device_id: str, profile_token: str, pan: float = 0.0, tilt: float = 0.0, zoom: float = 0.0
    ) -> bool:
        ptz = self._ensure_ptz(device_id)
        if not ptz:
            return False

        def _sync_move() -> None:
            velocity = {
                "PanTilt": {"x": pan, "y": tilt},
                "Zoom": {"x": zoom},
            }
            ptz.ContinuousMove({"ProfileToken": profile_token, "Velocity": velocity})

        try:
            await asyncio.wait_for(asyncio.to_thread(_sync_move), timeout=self._get_ptz_timeout())
            return True
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_PTZ_CONTINUOUS_FAILED", str(e))
            return False

    async def _ptz_absolute_move(
        self, device_id: str, profile_token: str, pan: float, tilt: float, zoom: float = 0.0
    ) -> bool:
        ptz = self._ensure_ptz(device_id)
        if not ptz:
            return False

        def _sync_move() -> None:
            position = {
                "PanTilt": {"x": pan, "y": tilt},
                "Zoom": {"x": zoom},
            }
            ptz.AbsoluteMove({"ProfileToken": profile_token, "Position": position})

        try:
            await asyncio.wait_for(asyncio.to_thread(_sync_move), timeout=self._get_ptz_timeout())
            return True
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_PTZ_ABSOLUTE_FAILED", str(e))
            return False

    async def _ptz_relative_move(
        self, device_id: str, profile_token: str, pan: float, tilt: float, zoom: float = 0.0
    ) -> bool:
        ptz = self._ensure_ptz(device_id)
        if not ptz:
            return False

        def _sync_move() -> None:
            translation = {
                "PanTilt": {"x": pan, "y": tilt},
                "Zoom": {"x": zoom},
            }
            ptz.RelativeMove({"ProfileToken": profile_token, "Translation": translation})

        try:
            await asyncio.wait_for(asyncio.to_thread(_sync_move), timeout=self._get_ptz_timeout())
            return True
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_PTZ_RELATIVE_FAILED", str(e))
            return False

    async def _ptz_stop(self, device_id: str, profile_token: str) -> bool:
        ptz = self._ensure_ptz(device_id)
        if not ptz:
            return False

        try:
            await asyncio.wait_for(asyncio.to_thread(ptz.Stop, ProfileToken=profile_token, PanTilt=True, Zoom=True), timeout=self._get_ptz_timeout())
            return True
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_PTZ_STOP_FAILED", str(e))
            return False

    async def _ptz_set_preset(self, device_id: str, profile_token: str, preset_name: str = "") -> str | None:
        ptz = self._ensure_ptz(device_id)
        if not ptz:
            return None
        def _sync() -> str | None:
            kwargs = {"ProfileToken": profile_token}
            if preset_name:
                kwargs["PresetName"] = preset_name
            resp = ptz.SetPreset(kwargs)
            return resp.token if hasattr(resp, "token") else str(resp)
        try:
            return await asyncio.wait_for(asyncio.to_thread(_sync), timeout=self._get_ptz_timeout())
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_PRESET_SET_FAILED", str(e))
            return None

    async def _ptz_goto_preset(self, device_id: str, profile_token: str, preset_token: str) -> bool:
        ptz = self._ensure_ptz(device_id)
        if not ptz:
            return False
        try:
            await asyncio.wait_for(asyncio.to_thread(ptz.GotoPreset, {"ProfileToken": profile_token, "PresetToken": preset_token}), timeout=self._get_ptz_timeout())
            return True
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_PRESET_GOTO_FAILED", str(e))
            return False

    async def _ptz_remove_preset(self, device_id: str, profile_token: str, preset_token: str) -> bool:
        ptz = self._ensure_ptz(device_id)
        if not ptz:
            return False
        try:
            await asyncio.wait_for(asyncio.to_thread(ptz.RemovePreset, {"ProfileToken": profile_token, "PresetToken": preset_token}), timeout=self._get_ptz_timeout())
            return True
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_PRESET_REMOVE_FAILED", str(e))
            return False

    async def _ptz_get_presets(self, device_id: str, profile_token: str) -> list[dict]:
        ptz = self._ensure_ptz(device_id)
        if not ptz:
            return []
        try:
            resp = await asyncio.wait_for(asyncio.to_thread(ptz.GetPresets, {"ProfileToken": profile_token}), timeout=self._get_ptz_timeout())
            presets = []
            if hasattr(resp, "Preset"):
                for p in resp.Preset:
                    presets.append({"token": p.token, "name": getattr(p, "Name", "")})
            return presets
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_PRESET_GET_FAILED", str(e))
            return []

    async def _get_snapshot_uri(self, device_id: str, profile_token: str = "") -> str:
        media = self._ensure_media(device_id)
        if not media:
            return ""

        cache_key = f"{device_id}:{profile_token}"
        cached = self._snapshot_uri_cache.get(cache_key)
        if cached and (time.monotonic() - cached[1]) < self._SNAPSHOT_CACHE_TTL:
            return cached[0]

        resolved_token = self._resolve_profile_token(device_id, profile_token)
        if not resolved_token:
            return ""

        def _sync(tok: str) -> str:
            resp = media.GetSnapshotUri({"ProfileToken": tok})
            return resp.Uri if hasattr(resp, "Uri") else str(resp)

        try:
            uri = await asyncio.wait_for(asyncio.to_thread(_sync, resolved_token), timeout=self._get_read_timeout())
            self._snapshot_uri_cache[cache_key] = (uri, time.monotonic())
            if len(self._snapshot_uri_cache) > 10000:  # FIXED-P2: 缓存容量限制
                for k in list(self._snapshot_uri_cache.keys())[:2000]:
                    self._snapshot_uri_cache.pop(k, None)
            return uri
        except Exception as e:
            if not profile_token:
                profiles = self._profiles_cache.get(device_id, [])
                for alt in profiles[1:]:
                    try:
                        alt_key = f"{device_id}:{alt['token']}"
                        uri = await asyncio.wait_for(asyncio.to_thread(_sync, alt["token"]), timeout=self._get_read_timeout())
                        self._snapshot_uri_cache[alt_key] = (uri, time.monotonic())
                        if len(self._snapshot_uri_cache) > 10000:  # FIXED-P2: 缓存容量限制
                            for k in list(self._snapshot_uri_cache.keys())[:2000]:
                                self._snapshot_uri_cache.pop(k, None)
                        return uri
                    except Exception as alt_err:
                        logger.debug("[onvif] device=%s alt profile %s snapshot uri failed: %s", device_id, alt.get("token"), alt_err)
                        continue
            self._log_error(device_id, "ERR_ONVIF_SNAPSHOT_URI_FAILED", str(e))
            return ""

    async def _subscribe_events(self, device_id: str) -> bool:
        """创建PullPoint事件订阅"""
        cam = self._cams.get(device_id)
        if not cam:
            return False
        try:
            event_service = await asyncio.wait_for(asyncio.to_thread(cam.create_events_service), timeout=15.0)  # FIXED-P2: 添加超时保护
            await asyncio.wait_for(asyncio.to_thread(event_service.CreatePullPointSubscription), timeout=15.0)  # FIXED-P2: 添加超时保护
            logger.info("[onvif] device=%s code=EVENT_SUBSCRIBED msg=PullPoint subscription created", device_id)
            return True
        except Exception as e:
            self._log_error(device_id, "ERR_ONVIF_EVENT_SUBSCRIBE_FAILED", str(e))
            return False

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, PointValue]:
        # FIXED-P0: 认证失败后允许冷却重试，而非永久阻止
        if self._auth_failed.get(device_id):
            failed_since = self._auth_failed_since.get(device_id, 0.0)
            if time.monotonic() - failed_since >= self._AUTH_RETRY_COOLDOWN:
                self._auth_failed[device_id] = False
                self._auth_failed_since.pop(device_id, None)
                logger.info("[onvif] device=%s auth retry cooldown expired, allowing re-authentication", device_id)
            else:
                now = datetime.now(UTC)
                return {p: PointValue(value=None, quality="bad", timestamp=now) for p in points}

        cam = self._cams.get(device_id)
        if not self._running or cam is None:
            await self._try_reconnect(device_id)
            now = datetime.now(UTC)
            return {p: PointValue(value=None, quality="bad", timestamp=now) for p in points}

        await self._detect_profiles(device_id)

        global_deadband = self._config.get("deadband")
        global_scaling = self._config.get("scaling")
        global_clamp = self._config.get("clamp")
        now = datetime.now(UTC)

        result: dict[str, PointValue] = {}
        async with self._lock:
            for point in points:
                try:
                    record_packet("tx", "onvif", device_id, f"ONVIF SOAP: {point}")
                    raw = None
                    if point == "rtsp":
                        raw = await self._get_rtsp_url(device_id)
                    elif point.startswith("rtsp:"):
                        token = point.split(":", 1)[1]
                        raw = await self._get_rtsp_url(device_id, token)
                    elif point == "profiles":
                        raw = self._profiles_cache.get(device_id) or None
                    elif point.startswith("ptz_status:"):
                        token = point.split(":", 1)[1]
                        ptz = self._ensure_ptz(device_id)
                        if ptz:
                            status_resp = await asyncio.wait_for(asyncio.to_thread(ptz.GetStatus, {"ProfileToken": token}), timeout=self._get_read_timeout())
                            raw = self._parse_ptz_status(status_resp)
                        else:
                            raw = None
                    elif point.startswith("snapshot:"):
                        token = point.split(":", 1)[1] if ":" in point else ""
                        uri = await self._get_snapshot_uri(device_id, token)
                        raw = await self._download_snapshot(device_id, uri) if uri else None
                    elif point == "snapshot":
                        uri = await self._get_snapshot_uri(device_id)
                        raw = await self._download_snapshot(device_id, uri) if uri else None
                    else:
                        raw = None

                    val = raw
                    if isinstance(val, (int, float)):
                        device_info = self._devices.get(device_id)
                        point_cfg = (device_info.get("points", {}) if device_info else {}).get(point, {})
                        db = point_cfg.get("deadband", global_deadband)
                        sc = point_cfg.get("scaling", global_scaling)
                        cl = point_cfg.get("clamp", global_clamp)
                        # FIXED-P2: 使用device_id:point作为key，避免多设备相同测点名时数据互相覆盖
                        _lv_key = f"{device_id}:{point}"
                        val = self._apply_deadband(val, self._last_values.get(_lv_key), db)
                        val = self._apply_scaling(val, sc)
                        val, clamped = self._apply_clamp(val, cl)
                        if not clamped:
                            result[point] = PointValue(value=None, quality="bad", timestamp=now)
                            self._last_values[_lv_key] = None
                            continue
                    _lv_key = f"{device_id}:{point}"
                    self._last_values[_lv_key] = val
                    quality = "bad" if val is None else "good"
                    result[point] = PointValue(value=val, quality=quality, timestamp=now)
                    record_packet("rx", "onvif", device_id, f"ONVIF SOAP: {point} = {val}")
                except Exception as e:
                    err_code = self._classify_soap_error(e)
                    self._log_error(device_id, err_code, str(e))
                    if err_code == "ERR_ONVIF_AUTH_FAILED":
                        self._auth_failed[device_id] = True
                        self._auth_failed_since[device_id] = time.monotonic()  # FIXED-P0: 记录认证失败时间
                    record_packet("rx", "onvif", device_id, f"ONVIF SOAP ERROR: {point} - {e}")
                    result[point] = PointValue(value=None, quality="bad", timestamp=now)

        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        # FIXED-P0: 认证失败后允许冷却重试
        if self._auth_failed.get(device_id):
            failed_since = self._auth_failed_since.get(device_id, 0.0)
            if time.monotonic() - failed_since >= self._AUTH_RETRY_COOLDOWN:
                self._auth_failed[device_id] = False
                self._auth_failed_since.pop(device_id, None)
            else:
                return False

        cam = self._cams.get(device_id)
        if not self._running or cam is None:
            await self._try_reconnect(device_id)
            return False

        try:
            async with self._lock:
                record_packet("tx", "onvif", device_id, f"ONVIF SOAP: {point}")

                if point.startswith("ptz_continuous:"):
                    token = point.split(":", 1)[1]
                    v = value if isinstance(value, dict) else {}
                    pan, tilt, zoom = v.get("pan", 0.0), v.get("tilt", 0.0), v.get("zoom", 0.0)
                    if not self._check_ptz_rate_limit(device_id):
                        self._log_error(device_id, "ERR_ONVIF_PTZ_RATE_LIMITED", "")
                        return False
                    limits = await self._get_ptz_limits(device_id, token)
                    err = self._validate_ptz_range(limits, pan, tilt, zoom)
                    if err:
                        self._log_error(device_id, err, f"pan={pan} tilt={tilt} zoom={zoom}")
                        self._audit_ptz(device_id, "continuous", pan, tilt, zoom, False)
                        return False
                    ok = await self._ptz_continuous_move(device_id, token, pan, tilt, zoom)
                    self._audit_ptz(device_id, "continuous", pan, tilt, zoom, ok)
                    record_packet("rx", "onvif", device_id, f"ONVIF SOAP: {point} = {ok}")
                    return ok

                elif point.startswith("ptz_absolute:"):
                    token = point.split(":", 1)[1]
                    v = value if isinstance(value, dict) else {}
                    pan, tilt, zoom = v.get("pan", 0.0), v.get("tilt", 0.0), v.get("zoom", 0.0)
                    if not self._check_ptz_rate_limit(device_id):
                        self._log_error(device_id, "ERR_ONVIF_PTZ_RATE_LIMITED", "")
                        return False
                    limits = await self._get_ptz_limits(device_id, token)
                    err = self._validate_ptz_range(limits, pan, tilt, zoom)
                    if err:
                        self._log_error(device_id, err, f"pan={pan} tilt={tilt} zoom={zoom}")
                        self._audit_ptz(device_id, "absolute", pan, tilt, zoom, False)
                        return False
                    ok = await self._ptz_absolute_move(device_id, token, pan, tilt, zoom)
                    self._audit_ptz(device_id, "absolute", pan, tilt, zoom, ok)
                    record_packet("rx", "onvif", device_id, f"ONVIF SOAP: {point} = {ok}")
                    return ok

                elif point.startswith("ptz_relative:"):
                    token = point.split(":", 1)[1]
                    v = value if isinstance(value, dict) else {}
                    pan, tilt, zoom = v.get("pan", 0.0), v.get("tilt", 0.0), v.get("zoom", 0.0)
                    if not self._check_ptz_rate_limit(device_id):
                        self._log_error(device_id, "ERR_ONVIF_PTZ_RATE_LIMITED", "")
                        return False
                    ok = await self._ptz_relative_move(device_id, token, pan, tilt, zoom)
                    self._audit_ptz(device_id, "relative", pan, tilt, zoom, ok)
                    record_packet("rx", "onvif", device_id, f"ONVIF SOAP: {point} = {ok}")
                    return ok

                elif point.startswith("ptz_stop:"):
                    token = point.split(":", 1)[1]
                    ok = await self._ptz_stop(device_id, token)
                    self._audit_ptz(device_id, "stop", 0.0, 0.0, 0.0, ok)
                    record_packet("rx", "onvif", device_id, f"ONVIF SOAP: {point} = {ok}")
                    return ok

                elif point.startswith("preset_remove:"):
                    parts = point.split(":", 2)
                    token = parts[1] if len(parts) >= 2 else ""
                    preset_token = parts[2] if len(parts) >= 3 else ""
                    if not preset_token:
                        v = value if isinstance(value, dict) else {}
                        preset_token = v.get("preset_token", "")
                    if not preset_token:
                        self._log_error(device_id, "ERR_ONVIF_WRITE_INVALID", point)
                        return False
                    existing = await self._ptz_get_presets(device_id, token)
                    if not any(p["token"] == preset_token for p in existing):
                        self._log_error(device_id, "ERR_ONVIF_PRESET_NOT_FOUND", preset_token)
                        return False
                    ok = await self._ptz_remove_preset(device_id, token, preset_token)
                    self._audit_ptz(device_id, f"preset_remove:{preset_token}", 0.0, 0.0, 0.0, ok)
                    return ok

                else:
                    self._log_error(device_id, "ERR_ONVIF_WRITE_INVALID", point)
                    return False
        except Exception as e:
            err_code = self._classify_soap_error(e)
            self._log_error(device_id, err_code, f"{point}: {e}")
            record_packet("rx", "onvif", device_id, f"ONVIF SOAP ERROR: {point} - {e}")
            if err_code == "ERR_ONVIF_AUTH_FAILED":
                self._auth_failed[device_id] = True
                self._auth_failed_since[device_id] = time.monotonic()  # FIXED-P0: 记录认证失败时间
            if cam is None:
                await self._try_reconnect(device_id)
            return False

    async def remove_device(self, device_id: str) -> None:  # FIXED-P0: 改为async，transport.close()通过to_thread避免阻塞事件循环
        with self._stats_lock:  # FIXED-P2: 健康统计pop纳入_stats_lock，与写入路径锁保护一致
            self._health_stats.pop(device_id, None)
            self._offline_since.pop(device_id, None)
        self._reconnect_counts.pop(device_id, None)
        self._reconnect_delays.pop(device_id, None)
        self._auth_failed.pop(device_id, None)
        # FIXED-P2: 清理认证冷却时间戳和最新值缓存
        self._auth_failed_since.pop(device_id, None)
        # R5-G-02: 清理设备级重连锁，防止设备移除后字典残留导致内存泄漏
        self._reconnect_locks.pop(device_id, None)
        self._last_values.pop(device_id, None)
        # FIXED-P0: 清理device_id:point格式的_last_values条目，防止设备移除后内存泄漏
        self._last_values = {k: v for k, v in self._last_values.items() if not k.startswith(f"{device_id}:")}
        self._profiles_cache.pop(device_id, None)
        self._last_ptz_time.pop(device_id, None)
        self._nonce_counters.pop(device_id, None)
        self._ptz_limits_cache = {k: v for k, v in self._ptz_limits_cache.items() if not k.startswith(f"{device_id}:")}
        # FIXED-P2: 使用精确匹配替代前缀匹配，防止device_id前缀重叠时误删其他设备缓存（如cam1误删cam10）
        self._snapshot_uri_cache = {k: v for k, v in self._snapshot_uri_cache.items() if k.split(":", 1)[0] != device_id}
        cam = self._cams.pop(device_id, None)
        if cam:
            try:
                # FIXED-P1: 检查soap_client是否为None，与stop()方法(line 427)保持一致，防止AttributeError
                if hasattr(cam, "devicemgmt") and hasattr(cam.devicemgmt, "soap_client"):
                    soap_client = cam.devicemgmt.soap_client
                    if soap_client is not None and hasattr(soap_client, "get_transport"):
                        transport = soap_client.get_transport()
                        if transport:
                            await asyncio.to_thread(transport.close)  # FIXED-P0: 同步transport.close()通过to_thread执行，避免阻塞事件循环
            except Exception as e:
                logger.warning("[onvif] remove_device failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        self._medias.pop(device_id, None)
        self._ptzs.pop(device_id, None)
        self._devices.pop(device_id, None)
        logger.info("ONVIF device removed: %s", device_id)

    async def _watchdog_loop(self) -> None:
        """看门狗：每15秒检查设备连接，连续2次失败（30秒）触发重连"""
        fail_counts: dict[str, int] = {}
        while self._running:
            await asyncio.sleep(15)
            if not self._running:
                break
            for device_id in list(self._cams.keys()):
                cam = self._cams.get(device_id)
                if cam is None:
                    fail_counts[device_id] = fail_counts.get(device_id, 0) + 1
                    if fail_counts[device_id] >= 2:
                        self._log_error(device_id, "ERR_ONVIF_WATCHDOG_TRIGGER", "")
                        fail_counts[device_id] = 0
                        await self._try_reconnect(device_id)
                    continue
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(cam.devicemgmt.GetDeviceInformation),
                        timeout=self._get_read_timeout(),
                    )
                    fail_counts[device_id] = 0
                    with self._stats_lock:  # FIXED-P0: _offline_since操作纳入_stats_lock，与remove_device/reset_health_stats锁保护一致，防止竞态
                        self._offline_since.pop(device_id, None)
                except TimeoutError:
                    fail_counts[device_id] = fail_counts.get(device_id, 0) + 1
                    if fail_counts[device_id] >= 2:
                        self._log_error(device_id, "ERR_ONVIF_WATCHDOG_TRIGGER", "timeout")
                        fail_counts[device_id] = 0
                        with self._stats_lock:  # FIXED-P0: _offline_since操作纳入_stats_lock，与remove_device/reset_health_stats锁保护一致，防止竞态
                            self._offline_since[device_id] = datetime.now(UTC)
                        await self._try_reconnect(device_id)
                except Exception:
                    fail_counts[device_id] = fail_counts.get(device_id, 0) + 1
                    if fail_counts[device_id] >= 2:
                        self._log_error(device_id, "ERR_ONVIF_WATCHDOG_TRIGGER", "")
                        fail_counts[device_id] = 0
                        with self._stats_lock:  # FIXED-P0: _offline_since操作纳入_stats_lock，与remove_device/reset_health_stats锁保护一致，防止竞态
                            self._offline_since[device_id] = datetime.now(UTC)
                        await self._try_reconnect(device_id)

    async def _try_reconnect(self, device_id: str) -> None:
        # FIXED(严重): 移除lock.locked() TOCTOU竞态检查，直接async with lock串行化重连
        # 原问题：lock.locked()检查与async with lock之间存在时间窗口，两个协程可能都通过检查导致重复重连
        lock = self._reconnect_locks.setdefault(device_id, asyncio.Lock())
        # 记录进入锁前的相机引用，用于锁内判断是否已被其他协程重连
        cam_before = self._cams.get(device_id)
        async with lock:
            # FIXED(严重): 锁内检查连接是否已由其他协程恢复，避免重复重连
            # 若相机实例已被替换为新的非None对象，说明其他协程已完成重连，无需再次重连
            cam_after = self._cams.get(device_id)
            if cam_after is not None and cam_after is not cam_before:
                return
            # FIXED-P0: 认证失败后允许冷却重试
            if self._auth_failed.get(device_id):
                failed_since = self._auth_failed_since.get(device_id, 0.0)
                if time.monotonic() - failed_since >= self._AUTH_RETRY_COOLDOWN:
                    self._auth_failed[device_id] = False
                    self._auth_failed_since.pop(device_id, None)
                else:
                    return

            device_info = self._devices.get(device_id)
            config = device_info.get("config", self._config) if device_info else self._config
            if not config:
                return

            count = self._reconnect_counts.get(device_id, 0) + 1
            self._reconnect_counts[device_id] = count
            if count > self._MAX_RECONNECT_ATTEMPTS:
                self._log_error(device_id, "ERR_ONVIF_RECONNECT_GIVEUP", f"attempts={count}")
                await self._set_connection_state(device_id, "offline", "max reconnect attempts reached")
                return

            delay = min(self._reconnect_delays.get(device_id, self._RECONNECT_BASE_DELAY), self._RECONNECT_MAX_DELAY)
            logger.warning("[onvif] reconnect in %.1fs (attempt %d) device=%s", delay, count, device_id)
            await asyncio.sleep(delay)
            new_delay = min(self._reconnect_delays.get(device_id, self._RECONNECT_BASE_DELAY) * 2, self._RECONNECT_MAX_DELAY)
            self._reconnect_delays[device_id] = new_delay

            cam = self._cams.get(device_id)
            if cam is not None:
                try:
                    # FIXED-P1: 与 stop() 保持一致的 None 保护，逐级检查 devicemgmt/soap_client/transport，
                    # 避免 None 链式访问抛 AttributeError（原代码无 None 检查，与 stop() 不一致）
                    if hasattr(cam, "devicemgmt") and hasattr(cam.devicemgmt, "soap_client"):
                        soap_client = cam.devicemgmt.soap_client
                        if soap_client is not None and hasattr(soap_client, "get_transport"):
                            transport = soap_client.get_transport()
                            if transport is not None:
                                await asyncio.to_thread(transport.close)
                except Exception as e:
                    logger.debug("[onvif] close camera error: %s", e)
                self._cams[device_id] = None
                self._medias[device_id] = None
                self._ptzs[device_id] = None

            try:
                new_cam = await asyncio.wait_for(asyncio.to_thread(self._create_camera, config), timeout=30.0)
                self._cams[device_id] = new_cam
                self._setup_digest_nonce(new_cam, device_id)
                self._medias[device_id] = None
                self._ptzs[device_id] = None
                self._reconnect_counts[device_id] = 0
                self._reconnect_delays[device_id] = self._RECONNECT_BASE_DELAY
                self._auth_failed.pop(device_id, None)
                self._auth_failed_since.pop(device_id, None)
                self._profiles_cache.pop(device_id, None)
                self._snapshot_uri_cache = {k: v for k, v in self._snapshot_uri_cache.items() if k.split(":", 1)[0] != device_id}
                ip = config.get("ip", "")
                logger.info("ONVIF重连成功: %s (device=%s)", ip, device_id)
            except Exception as e:
                new_cam = self._cams.get(device_id)
                if new_cam is not None:
                    try:
                        # FIXED-P1: 同上，逐级 None 检查避免链式访问抛 AttributeError（与 stop() 保持一致）
                        if hasattr(new_cam, "devicemgmt") and hasattr(new_cam.devicemgmt, "soap_client"):
                            soap_client = new_cam.devicemgmt.soap_client
                            if soap_client is not None and hasattr(soap_client, "get_transport"):
                                transport = soap_client.get_transport()
                                if transport is not None:
                                    await asyncio.to_thread(transport.close)
                    except Exception as e:
                        logger.warning("[onvif] operation failed: %s", e)
                    self._cams[device_id] = None
                if self._is_auth_error(e):
                    self._log_error(device_id, "ERR_ONVIF_AUTH_FAILED", str(e))
                    self._auth_failed[device_id] = True
                    self._auth_failed_since[device_id] = time.monotonic()
                else:
                    self._log_error(device_id, "ERR_ONVIF_RECONNECT_FAILED", str(e))

    async def health_check(self, device_id: str) -> bool:
        if not self._running:
            return False
        return self.is_device_connected(device_id)
