"""出网目标校验：拒绝本地 / 内网 / 链路本地 / 云元数据主机。

后端替用户发起、且目标地址由用户填写的请求（git clone、模型 API Base URL）
都必须先过这里，否则就是 SSRF 入口，最坏可以读到云上实例凭据。

放在 `internal/` 顶层单文件而不是某个业务模块里，是因为它被 gitops 与 models
共用；若长在 gitops 下，models 导入时会被迫连带加载 e2b（gitops 包 __init__
依赖 executor），把一个纯字符串校验变成一条重依赖。
"""

from __future__ import annotations

import ipaddress

# 云元数据端点（字面 IP 或域名）：命中即拒，不区分云厂商
CLOUD_METADATA_HOSTS = frozenset({
    "169.254.169.254",            # AWS / GCP / Azure 通用链路本地元数据 IP
    "metadata.google.internal",   # GCP
    "metadata.google.internal.",  # 尾点变体
})


class HostRejected(Exception):
    """主机未通过出网安全校验（可读原因，供 HTTP 边界转成用户提示）。"""


def assert_public_host(host: str) -> None:
    """host 必须是公网主机名或 IP；非法时抛 HostRejected。"""
    host = (host or "").rstrip(".").lower()
    if not host:
        raise HostRejected("missing host")
    if host == "localhost" or host.endswith(".localhost"):
        raise HostRejected("localhost is not allowed")
    reject_metadata_host(host)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # 非 IP 字面量 → 视为域名，放行（域名解析后的私网 IP 由部署层网络策略兜底）
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
        raise HostRejected("private/loopback addresses are not allowed")


def reject_metadata_host(host: str) -> None:
    """无条件拒绝云元数据端点。

    独立成一个函数，是因为「允许本地端点」的开关会放宽回环/私网判断，
    但**不能**顺带放开元数据 IP —— 否则本地部署就会变成读云凭据的跳板。
    """
    if (host or "").rstrip(".").lower() in CLOUD_METADATA_HOSTS:
        raise HostRejected("cloud metadata endpoints are not allowed")
