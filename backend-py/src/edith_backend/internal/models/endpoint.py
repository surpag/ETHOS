"""用户自填模型端点的校验、模型发现与连通性探测。

三件事各自独立：URL 校验是纯函数（无 IO，可单测），发现与探测才发网络请求。
发请求一律 `max_retries=0` + 显式超时：这两条路径是超时敏感的用户交互，
SDK 默认的重试会把一次「填错了」变成几十秒白屏。
"""

from __future__ import annotations

import os
import time
from urllib.parse import urlparse

from openai import AsyncOpenAI

from ..netguard import HostRejected, assert_public_host, reject_metadata_host

_DEFAULT_PROBE_TIMEOUT_S = 15.0


class EndpointRejected(Exception):
    """自定义模型端点未通过校验或探测失败。"""


def allow_local_endpoint() -> bool:
    """是否允许本地/内网端点（自建 Ollama、vLLM、公司内网网关）。

    默认关闭：云端多租户下 `base_url` 是用户可控输入，放开等于把后端变成
    内网探测器。自建部署可以显式打开 `EDITH_ALLOW_LOCAL_MODEL_ENDPOINT=true`。
    """
    return os.getenv("EDITH_ALLOW_LOCAL_MODEL_ENDPOINT", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def normalize_endpoint_url(raw: str) -> str:
    """校验并规范化模型端点的 Base URL，返回去掉尾斜杠的形式。

    允许 https（任意公网主机）与 http（仅本地端点开关打开时）；不允许携带
    query / fragment —— 那通常是把完整接口地址误填进来，静默接受会让请求 404。
    """
    url = (raw or "").strip()
    if not url:
        raise EndpointRejected("Base URL is required")
    parsed = urlparse(url)
    local_ok = allow_local_endpoint()
    if parsed.scheme not in ("https", "http"):
        raise EndpointRejected("Base URL must start with https:// (or http:// for local endpoints)")
    if parsed.scheme == "http" and not local_ok:
        raise EndpointRejected("http:// endpoints are only allowed when EDITH_ALLOW_LOCAL_MODEL_ENDPOINT is on")
    if parsed.username or parsed.password:
        raise EndpointRejected("Base URL must not embed credentials")
    if parsed.query or parsed.fragment:
        raise EndpointRejected("Base URL must not contain query string or fragment")
    if local_ok:
        # 本地开关只放宽回环/私网（自建 Ollama、vLLM、公司内网网关），
        # 云元数据端点永不放开：那是「读云凭据」而不是「访问模型」。
        host = (parsed.hostname or "").strip()
        if not host:
            raise EndpointRejected("Base URL is missing a host")
        try:
            reject_metadata_host(host)
        except HostRejected as exc:
            raise EndpointRejected(f"Base URL rejected: {exc}") from exc
    else:
        try:
            assert_public_host(parsed.hostname or "")
        except HostRejected as exc:
            raise EndpointRejected(f"Base URL rejected: {exc}") from exc
    return url.rstrip("/")


def validate_api_model(api_model: str) -> str:
    """校验真实模型名：不允许空白与控制字符（拼进请求体会直接 400）。"""
    value = (api_model or "").strip()
    if not value:
        raise EndpointRejected("model name is required")
    if len(value) > 128:
        raise EndpointRejected("model name is too long")
    if any(char.isspace() for char in value):
        raise EndpointRejected("model name must not contain spaces")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise EndpointRejected("model name must not contain control characters")
    return value


def _client(base_url: str, api_key: str, timeout_s: float) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_s,
        max_retries=0,
    )


def _timeout() -> float:
    raw = os.getenv("EDITH_MODEL_PROBE_TIMEOUT_S", "").strip()
    try:
        return float(raw) if raw else _DEFAULT_PROBE_TIMEOUT_S
    except ValueError:
        return _DEFAULT_PROBE_TIMEOUT_S


async def discover_models(base_url: str, api_key: str) -> list[str]:
    """请求 `GET {base_url}/models`，返回远端声明的模型名列表。

    不是所有 OpenAI 兼容端点都实现了这个接口（部分网关直接 404），
    所以调用方必须把失败当成「可以手填模型名」而不是阻塞保存。
    """
    if not api_key:
        raise EndpointRejected("API Key is required to list models")
    client = _client(base_url, api_key, _timeout())
    try:
        page = await client.models.list()
        return sorted({item.id for item in page.data if getattr(item, "id", "")})
    except Exception as exc:
        raise EndpointRejected(_describe(exc)) from exc


async def probe_model(base_url: str, api_key: str, api_model: str) -> dict:
    """发一次 `max_tokens=1` 的最小请求，验证端点 + 凭据 + 模型名三者都对。"""
    if not api_key:
        raise EndpointRejected("API Key is required")
    api_model = validate_api_model(api_model)
    client = _client(base_url, api_key, _timeout())
    started = time.monotonic()
    try:
        await client.chat.completions.create(
            model=api_model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            stream=False,
        )
    except Exception as exc:
        raise EndpointRejected(_describe(exc)) from exc
    return {"ok": True, "latencyMs": int((time.monotonic() - started) * 1000)}


def _describe(exc: Exception) -> str:
    """把 SDK 异常压成一句人话（原始类型名留在括号里便于排查）。"""
    status = getattr(exc, "status_code", None)
    message = str(getattr(exc, "message", "") or exc).strip()
    if len(message) > 300:
        message = message[:300] + "…"
    name = type(exc).__name__
    suffix = f" {status}" if status else ""
    return f"{message} ({name}{suffix})"
