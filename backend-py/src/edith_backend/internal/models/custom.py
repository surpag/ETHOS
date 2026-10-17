"""用户自定义供应商与自定义模型的存储。

设计约束（与 `catalog.py` 的分工）：
- 内置目录是**只读权威**：模型参数里的代码级知识（`extra_fields`、
  `does_not_report_cached_prompt_tokens`、各厂商 base_url）留在常量里，不入库。
- 自定义是**叠加层**：ID 一律带前缀（`custom.provider:` / `custom.model:`），
  与内置 ID 空间不相交 → 合并时是集合并集，下游不需要「是不是自定义」的判断。
- API Key 的读写只有 `userconfig.providers` 一个出口，本模块只转调。
"""

from __future__ import annotations

import secrets

import aiosqlite

from ..userconfig.providers import Providers
from .endpoint import EndpointRejected, normalize_endpoint_url, validate_api_model
from .types import (
    CUSTOM_MODEL_PREFIX,
    CUSTOM_PROVIDER_PREFIX,
    ModelDefinition,
    ProviderInfo,
)

# 上下文窗口的合法区间：0 会让摘要阈值退化成「每轮都压缩」，过大会静默超限。
_MIN_CONTEXT_WINDOW = 4_096
_MAX_CONTEXT_WINDOW = 10_000_000
DEFAULT_CONTEXT_WINDOW = 128_000


def is_custom_model_id(model_id: str) -> bool:
    return model_id.startswith(CUSTOM_MODEL_PREFIX)


def is_custom_provider_id(provider_id: str) -> bool:
    return provider_id.startswith(CUSTOM_PROVIDER_PREFIX)


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


def _check_name(value: str, label: str) -> str:
    name = (value or "").strip()
    if not name:
        raise EndpointRejected(f"{label} is required")
    if len(name) > 64:
        raise EndpointRejected(f"{label} is too long")
    return name


def _check_context_window(value: int) -> int:
    size = int(value or 0)
    if size < _MIN_CONTEXT_WINDOW or size > _MAX_CONTEXT_WINDOW:
        raise EndpointRejected(
            f"context window must be between {_MIN_CONTEXT_WINDOW} and {_MAX_CONTEXT_WINDOW}"
        )
    return size


class CustomModels:
    """某个用户的自定义供应商与模型集合。"""

    def __init__(self, db: aiosqlite.Connection, providers: Providers) -> None:
        self._db = db
        self._providers = providers

    async def providers(self, user_id: str) -> list[ProviderInfo]:
        """返回该用户的自定义供应商（含端点地址与模型数量）。"""
        cursor = await self._db.execute(
            """
            SELECT p.provider_id, p.name, p.base_url, COUNT(m.model_id) AS model_count
            FROM user_custom_providers AS p
            LEFT JOIN user_custom_models AS m
                   ON m.user_id = p.user_id AND m.provider_id = p.provider_id
            WHERE p.user_id = ?
            GROUP BY p.provider_id
            ORDER BY p.created_at, p.name
            """,
            (user_id.strip(),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            ProviderInfo(
                id=row["provider_id"],
                name=row["name"],
                base_url=row["base_url"],
                custom=True,
                model_count=int(row["model_count"]),
            )
            for row in rows
        ]

    async def definitions(self, user_id: str) -> list[ModelDefinition]:
        """返回该用户所有自定义模型的完整定义（运行时可直接使用）。

        base_url 优先取供应商表里的当前值（用户删了供应商重加同一个地址时能跟上），
        回退到建模型时的快照 —— 内置供应商下的模型只能靠快照，它不在本模块的
        provider 表里。
        """
        cursor = await self._db.execute(
            """
            SELECT m.model_id, m.provider_id, m.name, m.api_model,
                   m.context_window, m.vision,
                   COALESCE(NULLIF(p.base_url, ''), m.base_url) AS base_url
            FROM user_custom_models AS m
            LEFT JOIN user_custom_providers AS p
                   ON p.user_id = m.user_id AND p.provider_id = m.provider_id
            WHERE m.user_id = ?
            ORDER BY m.created_at, m.model_id
            """,
            (user_id.strip(),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            ModelDefinition(
                id=row["model_id"],
                provider_id=row["provider_id"],
                name=row["name"],
                context_window=int(row["context_window"]),
                api_model=row["api_model"],
                base_url=row["base_url"],
                vision=bool(row["vision"]),
            )
            for row in rows
        ]

    async def find(self, user_id: str, model_id: str) -> ModelDefinition | None:
        """按自定义模型 ID 查找；非自定义 ID 直接返回 None（不查库）。"""
        if not is_custom_model_id(model_id):
            return None
        for definition in await self.definitions(user_id):
            if definition.id == model_id:
                return definition
        return None

    async def create_provider(
        self,
        user_id: str,
        name: str,
        base_url: str,
        api_key: str,
    ) -> ProviderInfo:
        """新建自定义供应商，并把它提供的 Key 写进 user_providers（与内置同表）。"""
        user_id = user_id.strip()
        clean_name = _check_name(name, "provider name")
        clean_url = normalize_endpoint_url(base_url)
        provider_id = _new_id(CUSTOM_PROVIDER_PREFIX)
        await self._db.execute(
            """
            INSERT INTO user_custom_providers (user_id, provider_id, name, base_url)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, provider_id, clean_name, clean_url),
        )
        await self._db.commit()
        if api_key.strip():
            await self._providers.save(user_id, [(provider_id, api_key.strip())])
        return ProviderInfo(
            id=provider_id,
            name=clean_name,
            base_url=clean_url,
            custom=True,
            model_count=0,
        )

    async def delete_provider(self, user_id: str, provider_id: str) -> None:
        """删除自定义供应商：连带它的模型与凭据。

        内置供应商不可删 —— 在这里统一拒绝，而不是让调用方各自判断。
        """
        user_id = user_id.strip()
        if not is_custom_provider_id(provider_id):
            raise EndpointRejected("built-in providers cannot be deleted")
        cursor = await self._db.execute(
            "SELECT 1 FROM user_custom_providers WHERE user_id = ? AND provider_id = ?",
            (user_id, provider_id),
        )
        exists = await cursor.fetchone()
        await cursor.close()
        if exists is None:
            raise EndpointRejected("provider not found")
        await self._db.execute(
            "DELETE FROM user_custom_models WHERE user_id = ? AND provider_id = ?",
            (user_id, provider_id),
        )
        await self._db.execute(
            "DELETE FROM user_custom_providers WHERE user_id = ? AND provider_id = ?",
            (user_id, provider_id),
        )
        await self._db.commit()
        await self._providers.delete(user_id, provider_id)

    async def create_model(
        self,
        user_id: str,
        provider_id: str,
        api_model: str,
        base_url: str,
        name: str = "",
        context_window: int = DEFAULT_CONTEXT_WINDOW,
        vision: bool = False,
    ) -> ModelDefinition:
        """在指定供应商下新增一个自定义模型。

        `base_url` 由调用方解析后传入：内置供应商的端点在代码常量里，
        自定义供应商在 provider 表里 —— 本模块只负责落库，不猜来源。
        存快照是为了让内置供应商下的模型也能不依赖外部表自洽。
        """
        user_id = user_id.strip()
        if not base_url:
            raise EndpointRejected("provider endpoint is unknown")
        if is_custom_provider_id(provider_id):
            # 自定义供应商必须真实存在，否则会留下一个永远解析不出端点的孤儿模型
            await self.provider_base_url(user_id, provider_id)
        clean_api_model = validate_api_model(api_model)
        clean_name = (name or "").strip() or clean_api_model
        size = _check_context_window(context_window)
        if len(clean_name) > 64:
            raise EndpointRejected("display name is too long")
        model_id = _new_id(CUSTOM_MODEL_PREFIX)
        await self._db.execute(
            """
            INSERT INTO user_custom_models
                (user_id, model_id, provider_id, name, api_model,
                 context_window, vision, base_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                model_id,
                provider_id,
                clean_name,
                clean_api_model,
                size,
                int(bool(vision)),
                base_url,
            ),
        )
        await self._db.commit()
        return ModelDefinition(
            id=model_id,
            provider_id=provider_id,
            name=clean_name,
            context_window=size,
            api_model=clean_api_model,
            base_url=base_url,
            vision=bool(vision),
        )

    async def update_model(
        self,
        user_id: str,
        model_id: str,
        *,
        name: str | None = None,
        api_model: str | None = None,
        context_window: int | None = None,
        vision: bool | None = None,
    ) -> None:
        """更新显示名 / 模型名 / 上下文窗口 / 视觉能力（None 表示不改）。"""
        user_id = user_id.strip()
        if not is_custom_model_id(model_id):
            raise EndpointRejected("built-in models cannot be modified")
        assignments: list[str] = []
        params: list[object] = []
        if name is not None:
            assignments.append("name = ?")
            params.append(_check_name(name, "display name"))
        if api_model is not None:
            assignments.append("api_model = ?")
            params.append(validate_api_model(api_model))
        if context_window is not None:
            assignments.append("context_window = ?")
            params.append(_check_context_window(context_window))
        if vision is not None:
            assignments.append("vision = ?")
            params.append(int(bool(vision)))
        if not assignments:
            return
        params.extend([user_id, model_id])
        cursor = await self._db.execute(
            f"UPDATE user_custom_models SET {', '.join(assignments)} "
            "WHERE user_id = ? AND model_id = ?",
            tuple(params),
        )
        await self._db.commit()
        if cursor.rowcount == 0:
            raise EndpointRejected("model not found")

    async def delete_model(self, user_id: str, model_id: str) -> None:
        """删除一个自定义模型（内置模型不可删）。"""
        user_id = user_id.strip()
        if not is_custom_model_id(model_id):
            raise EndpointRejected("built-in models cannot be deleted")
        cursor = await self._db.execute(
            "DELETE FROM user_custom_models WHERE user_id = ? AND model_id = ?",
            (user_id, model_id),
        )
        await self._db.commit()
        if cursor.rowcount == 0:
            raise EndpointRejected("model not found")

    async def provider_base_url(self, user_id: str, provider_id: str) -> str:
        """取自定义供应商的端点地址；不存在时抛 EndpointRejected。"""
        cursor = await self._db.execute(
            "SELECT base_url FROM user_custom_providers WHERE user_id = ? AND provider_id = ?",
            (user_id.strip(), provider_id.strip()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise EndpointRejected("provider not found")
        return row["base_url"]
