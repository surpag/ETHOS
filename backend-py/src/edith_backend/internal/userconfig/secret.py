"""凭据混淆工具：避免 API Key / MCP Header 以明文落盘。

定位（重要）：这是「防君子不防小人」的基础混淆（obfuscation），**不是强加密**——
密钥本身在应用内（环境变量 `EDITH_SECRET_KEY`，缺省回退到内置 fallback key），
拿到数据库文件 + 应用源码即可还原。真正的边界是「本机单用户」；
上多用户/云端部署前，必须换成 KMS / 信封加密 / 专用密钥管理。

格式：`enc:v1:<base64(xor(utf8(data), mask))>`，其中 mask 由应用密钥按长度展开。
不带 `enc:v1:` 前缀的旧值视为历史明文，原样返回（向后兼容，旧数据不破坏）。
"""

from __future__ import annotations

import base64
import os

_PREFIX = "enc:v1:"
_FALLBACK_KEY = "edith-local-obfuscation-key-v1"


def _key() -> str:
    return os.environ.get("EDITH_SECRET_KEY") or _FALLBACK_KEY


def _mask(raw: bytes) -> bytes:
    key = _key().encode("utf-8")
    return (key * (len(raw) // len(key) + 1))[: len(raw)]


def obfuscate(plain: str) -> str:
    """混淆明文；空串直接返回空串（保持调用方 has_key/非空判断的语义）。"""
    if not plain:
        return plain
    data = plain.encode("utf-8")
    encoded = base64.b64encode(bytes(b ^ m for b, m in zip(data, _mask(data)))).decode("ascii")
    return _PREFIX + encoded


def deobfuscate(stored: str) -> str:
    """还原混淆值；无前缀的旧明文直接返回（向后兼容）。"""
    if not stored:
        return stored
    if not stored.startswith(_PREFIX):
        return stored
    try:
        raw = base64.b64decode(stored[len(_PREFIX) :])
    except Exception:  # noqa: BLE001 - 损坏数据按明文返回，不阻塞使用
        return stored
    return bytes(b ^ m for b, m in zip(raw, _mask(raw))).decode("utf-8", errors="replace")
