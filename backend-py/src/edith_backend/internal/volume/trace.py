"""volume 的诊断日志与隐私指纹。

对应 Go 版 internal/volume/trace.go 的 traceEnabled / userFingerprint。
"""

from __future__ import annotations

import hashlib
import os

# EDITH_E2B_DEBUG_HTTP 开启 E2B Volume 边界的短时诊断日志。
TRACE_ENV = "EDITH_E2B_DEBUG_HTTP"


def trace_enabled() -> bool:
    return os.getenv(TRACE_ENV, "").strip().lower() == "true"


def user_fingerprint(user_id: str) -> str:
    """用户 ID 指纹化，避免日志泄露明文 userId。"""
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]


def token_fingerprint(token: str) -> str:
    """Token 指纹化，避免日志泄露明文密钥。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
