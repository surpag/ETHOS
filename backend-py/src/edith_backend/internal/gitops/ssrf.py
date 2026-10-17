"""clone URL 安全校验：拒绝本地/内网/云元数据端点（防 SSRF）。

逻辑复用 `userconfig/mcp.py` 的既有校验（localhost / 回环 / 私有 / 链路本地 / 未指定），
并补充云元数据端点——`169.254.169.254`（AWS/GCP/Azure 元数据服务）与常见元数据域名。
clone 是后端/沙箱发起的网络请求，必须防 SSRF 与"读云凭据"。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ..netguard import HostRejected, assert_public_host

# scp-like 形式：git@github.com:owner/repo.git
_SCP_LIKE_RE = re.compile(r"^[^@/\s]+@([^:\s]+):")


class CloneUrlRejected(Exception):
    """clone URL 未通过安全校验。"""


def _assert_public_host(host: str) -> None:
    """host 必须是公网主机名/IP；规则见 `internal/netguard.py`（与模型端点共用）。"""
    try:
        assert_public_host(host)
    except HostRejected as exc:
        raise CloneUrlRejected(f"clone URL rejected: {exc}") from exc


def assert_safe_clone_url(url: str) -> None:
    """校验 clone 目标 URL；非法时抛 CloneUrlRejected。允许 https / ssh 与 scp-like 形式。"""
    url = (url or "").strip()
    if not url:
        raise CloneUrlRejected("clone URL is required")

    # scp-like：git@host:path
    m = _SCP_LIKE_RE.match(url)
    if m:
        _assert_public_host(m.group(1))
        return

    parsed = urlparse(url)
    if parsed.scheme == "file":
        raise CloneUrlRejected("file:// clone URLs are not allowed")
    if parsed.scheme == "git":
        raise CloneUrlRejected("git:// (unencrypted) clone URLs are not allowed")
    if parsed.scheme not in ("https", "ssh"):
        raise CloneUrlRejected(f"unsupported clone URL scheme: {parsed.scheme or '(none)'!r}")
    _assert_public_host(parsed.hostname)
