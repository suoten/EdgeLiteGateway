"""通用工具函数模块。"""

import ipaddress
import socket
import time


def resolve_and_validate_host(host: str) -> str | None:
    """SSRF 校验 + DNS rebinding 防护：解析并校验 host，返回可直连的安全 IP。

    校验规则（设备连通性测试场景，允许内网地址，但拦截危险地址段）：
    - 拦截 loopback / link-local（云元数据 169.254.x.x）/ unspecified /
      multicast / reserved 地址；
    - IP 字面量直接校验；
    - 域名先通过 socket.getaddrinfo 解析，任一解析结果命中黑名单即整体拒绝。

    返回解析出的首个安全 IP（调用方应使用该 IP 直连，Host 头/SNI 保持原域名），
    校验失败或解析失败返回 None。
    """
    if not host:
        return None
    # 直接作为 IP 字面量解析
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            return None
        return str(ip)
    except ValueError:
        pass
    # 域名：解析为 IP 后逐个校验
    try:
        addrs = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return None
    if not addrs:
        return None
    first_safe_ip: str | None = None
    for _family, _stype, _proto, _canon, sockaddr in addrs:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            return None
        if first_safe_ip is None:
            first_safe_ip = str(ip)
    return first_safe_ip


def timestamp_ms() -> int:
    """返回当前时间的毫秒级 Unix 时间戳。"""
    return int(time.time() * 1000)


def timestamp_s() -> int:
    """返回当前时间的秒级 Unix 时间戳。"""
    return int(time.time())
