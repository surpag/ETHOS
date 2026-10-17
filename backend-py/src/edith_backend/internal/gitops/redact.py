"""git 输出脱敏：在结果返回模型 / 落日志之前，抹掉凭据痕迹。

对应 wuu `internal/tools/git.go:416` 的 `gitSecretValuePatterns`。云端多租户刚需——
凭据若经 URL 形式注入（`https://token@host/...`），`git remote -v`、报错 stderr 都会原样吐出。
"""

from __future__ import annotations

import re

# 每条命中后统一替换为占位符（fail-closed：宁可隐藏，不赌正则全覆盖）
_REDACT_PLACEHOLDER = "<redacted>"

_SECRET_PATTERNS = [
    # Authorization: Bearer xxx / 裸 bearer xxx
    re.compile(r"(?i)\bauthorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    # api_key / access_token / refresh_token / password / secret = xxx
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret)"
        r"\s*([:=])\s*[\"']?[^\"'\s,;]+"
    ),
    # ★ URL 内嵌凭据：https://user:token@github.com/...（含 ssh/git 前缀）
    re.compile(r"(?i)(https?|ssh|git)://[^/\s:@]+:[^@\s/]+@"),
]


def redact_git_output(text: str) -> str:
    """把 text 中所有疑似凭据片段替换为 <redacted>；空值原样返回。"""
    if not text:
        return text
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACT_PLACEHOLDER, text)
    return text
