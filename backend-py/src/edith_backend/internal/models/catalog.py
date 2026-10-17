"""模型目录：内置模型定义 + 用户自定义的叠加层。

内置部分（本文件的常量）是**只读权威源**，存放模型参数里的代码级知识；
用户自定义部分存在数据库（见 `custom.CustomModels`）。两者的 ID 空间以前缀
隔开（`types.CUSTOM_MODEL_PREFIX`），所以合并就是一次集合并集，
调用方不需要（也不应该）判断「这个模型是不是内置」。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types import CUSTOM_MODEL_PREFIX, ModelDefinition, ModelInfo, ProviderInfo

if TYPE_CHECKING:
    from .custom import CustomModels

# 供应商稳定 ID
DEEPSEEK_PROVIDER_ID = "deepseek"
MINIMAX_PROVIDER_ID = "minimax"
STEPFUN_PROVIDER_ID = "stepfun"
STEPFUN_PLAN_PROVIDER_ID = "stepfun_plan"
XIAOMI_PROVIDER_ID = "xiaomi"
# 新增
BAILIAN_PROVIDER_ID = "qwen"

# 模型稳定 ID
DEEPSEEK_V4_FLASH_ID = "deepseek.v4.flash"
DEEPSEEK_V4_PRO_ID = "deepseek.v4.pro"
MINIMAX_M3_ID = "minimax.m3"
STEP_37_FLASH_ID = "stepfun.step-3.7-flash"
STEP_35_FLASH_ID = "stepfun.step-3.5-flash"
STEP_PLAN_37_FLASH_ID = "stepfun_plan.step-3.7-flash"
STEP_PLAN_35_FLASH_ID = "stepfun_plan.step-3.5-flash"
XIAOMI_MIMO_V25_PRO_ID = "xiaomi.mimo-v2.5-pro"
XIAOMI_MIMO_V25_ID = "xiaomi.mimo-v2.5"
DEFAULT_MODEL_ID = MINIMAX_M3_ID
QWEN_36_FLASH_ID = "qwen3.6-flash"
QWEN_36_PLUS_ID = "qwen3.6-plus-2026-04-02"
QWEN_37_FLASH_ID = "qwen3.7-flash-2026-07-15"
QWEN_38_FLASH_ID = "qwen3.8-flash"
QEWN_38_MAX_ID = "qwen3.8-max-0902"
QWEN_DEEPSEEEK_ID = "deepseek-v4.1-flash"
QWEN_KIMI_ID = "kimi-k3"
QWEN_GLM_ID = "glm-5.3"


# 上下文窗口
_CONTEXT_WINDOW_1M = 1_000_000
_CONTEXT_WINDOW_256K = 256_000

# 各供应商的 OpenAI 兼容 base_url
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"
_STEPFUN_BASE_URL = "https://api.stepfun.com/v1"
_STEP_PLAN_BASE_URL = "https://api.stepfun.com/step_plan/v1"
_XIAOMI_BASE_URL = "https://api.xiaomimimo.com/v1"
_BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def provider_definitions() -> list[ProviderInfo]:
    return [
        ProviderInfo(id=DEEPSEEK_PROVIDER_ID, name="DeepSeek"),
        ProviderInfo(id=MINIMAX_PROVIDER_ID, name="MiniMax"),
        ProviderInfo(id=STEPFUN_PROVIDER_ID, name="阶跃星辰"),
        ProviderInfo(id=STEPFUN_PLAN_PROVIDER_ID, name="阶跃星辰 (Step Plan)"),
        ProviderInfo(id=XIAOMI_PROVIDER_ID, name="小米 MiMo"),
        ProviderInfo(id=BAILIAN_PROVIDER_ID, name="Qwen"),
    ]


def model_definitions() -> list[ModelDefinition]:
    return [
        ModelDefinition(
            id=DEEPSEEK_V4_FLASH_ID,
            provider_id=DEEPSEEK_PROVIDER_ID,
            name="DeepSeek V4 Flash",
            context_window=_CONTEXT_WINDOW_1M,
            api_model="deepseek-v4-flash",
            base_url=_DEEPSEEK_BASE_URL,
        ),
        ModelDefinition(
            id=DEEPSEEK_V4_PRO_ID,
            provider_id=DEEPSEEK_PROVIDER_ID,
            name="DeepSeek V4 Pro",
            context_window=_CONTEXT_WINDOW_1M,
            api_model="deepseek-v4-pro",
            base_url=_DEEPSEEK_BASE_URL,
        ),
        ModelDefinition(
            id=MINIMAX_M3_ID,
            provider_id=MINIMAX_PROVIDER_ID,
            name="MiniMax M3",
            context_window=_CONTEXT_WINDOW_1M,
            api_model="MiniMax-M3",
            base_url=_MINIMAX_BASE_URL,
            vision=True,
            extra_fields={"reasoning_split": True},
        ),
        ModelDefinition(
            id=STEP_37_FLASH_ID,
            provider_id=STEPFUN_PROVIDER_ID,
            name="Step 3.7 Flash",
            context_window=_CONTEXT_WINDOW_256K,
            api_model="step-3.7-flash",
            base_url=_STEPFUN_BASE_URL,
            vision=True,
        ),
        ModelDefinition(
            id=STEP_35_FLASH_ID,
            provider_id=STEPFUN_PROVIDER_ID,
            name="Step 3.5 Flash",
            context_window=_CONTEXT_WINDOW_256K,
            api_model="step-3.5-flash",
            base_url=_STEPFUN_BASE_URL,
            vision=True,
        ),
        ModelDefinition(
            id=STEP_PLAN_37_FLASH_ID,
            provider_id=STEPFUN_PLAN_PROVIDER_ID,
            name="Step 3.7 Flash (Step Plan)",
            context_window=_CONTEXT_WINDOW_256K,
            api_model="step-3.7-flash",
            base_url=_STEP_PLAN_BASE_URL,
            vision=True,
        ),
        ModelDefinition(
            id=STEP_PLAN_35_FLASH_ID,
            provider_id=STEPFUN_PLAN_PROVIDER_ID,
            name="Step 3.5 Flash (Step Plan)",
            context_window=_CONTEXT_WINDOW_256K,
            api_model="step-3.5-flash",
            base_url=_STEP_PLAN_BASE_URL,
        ),
        ModelDefinition(
            id=XIAOMI_MIMO_V25_PRO_ID,
            provider_id=XIAOMI_PROVIDER_ID,
            name="MiMo v2.5 Pro",
            context_window=_CONTEXT_WINDOW_1M,
            api_model="mimo-v2.5-pro",
            base_url=_XIAOMI_BASE_URL,
        ),
        ModelDefinition(
            id=XIAOMI_MIMO_V25_ID,
            provider_id=XIAOMI_PROVIDER_ID,
            name="MiMo v2.5",
            context_window=_CONTEXT_WINDOW_1M,
            api_model="mimo-v2.5",
            base_url=_XIAOMI_BASE_URL,
            vision=True,
        ),
        # 新增qwen3.6-flash
        ModelDefinition(
            id=QWEN_37_FLASH_ID,
            provider_id=BAILIAN_PROVIDER_ID,
            name="qwen3.7-flash",
            context_window=_CONTEXT_WINDOW_1M,
            api_model="qwen3.7-flash",
            base_url=_BAILIAN_BASE_URL,
        ),
        ModelDefinition(
            id=QWEN_38_FLASH_ID,
            provider_id=BAILIAN_PROVIDER_ID,
            name="qwen3.8-flash",
            context_window=_CONTEXT_WINDOW_1M,
            api_model="qwen3.8-flash",
            base_url=_BAILIAN_BASE_URL,
        ),
        ModelDefinition(
            id=QEWN_38_MAX_ID,
            provider_id=BAILIAN_PROVIDER_ID,
            name="qwen3.8-max-0902",
            context_window=_CONTEXT_WINDOW_1M,
            api_model="qwen3.8-max-0902",
            base_url=_BAILIAN_BASE_URL,
        ),
        ModelDefinition(
            id=QWEN_DEEPSEEEK_ID,
            provider_id=BAILIAN_PROVIDER_ID,
            name="deepseek-v4.1-flash",
            context_window=_CONTEXT_WINDOW_1M,
            api_model="deepseek-v4.1-flash",
            base_url=_BAILIAN_BASE_URL,
        ),
        ModelDefinition(
            id=QWEN_KIMI_ID,
            provider_id=BAILIAN_PROVIDER_ID,
            name="kimi-k3",
            context_window=_CONTEXT_WINDOW_1M,
            api_model="kimi-k3",
            base_url=_BAILIAN_BASE_URL,
        ),
        ModelDefinition(
            id=QWEN_GLM_ID,
            provider_id=BAILIAN_PROVIDER_ID,
            name="glm-5.2",
            context_window=_CONTEXT_WINDOW_1M,
            api_model="glm-5.2",
            base_url=_BAILIAN_BASE_URL,
        ),
    ]


class Catalog:
    """内置模型目录 + 用户自定义叠加层。

    标记为内置/自定义的差异只体现在「存在哪里」，读取路径完全相同：
    所有对外查询都返回合并后的结果，运行时装配不需要分支。
    """

    def __init__(self, custom: CustomModels | None = None) -> None:
        self._providers = provider_definitions()
        self._definitions = model_definitions()
        self._by_id = {definition.id: definition for definition in self._definitions}
        self._custom = custom

    def providers(self) -> list[ProviderInfo]:
        """返回复制后的内置供应商目录（无 user 上下文的场景使用）。"""
        return list(self._providers)

    def models(self) -> list[ModelInfo]:
        """返回复制后的内置模型目录。"""
        return [definition.to_info() for definition in self._definitions]

    def definitions(self) -> list[ModelDefinition]:
        """返回内置完整定义列表（含 context_window / base_url 等内部字段）。"""
        return list(self._definitions)

    async def all_providers(self, user_id: str) -> list[ProviderInfo]:
        """内置供应商 ∪ 该用户的自定义供应商，各自带**合并后**的模型数量。

        模型数用来驱动设置页卡片上的角标，必须把自定义模型算进去——
        否则用户在内置供应商下加了模型，卡片仍然显示旧数字。
        """
        custom_providers: list[ProviderInfo] = []
        custom_definitions: list[ModelDefinition] = []
        if self._custom is not None and user_id.strip():
            custom_providers = await self._custom.providers(user_id)
            custom_definitions = await self._custom.definitions(user_id)

        counts: dict[str, int] = {}
        for definition in (*self._definitions, *custom_definitions):
            counts[definition.provider_id] = counts.get(definition.provider_id, 0) + 1

        merged = [
            provider.model_copy(update={"model_count": counts.get(provider.id, 0)})
            for provider in (*self._providers, *custom_providers)
        ]
        return merged

    async def all_definitions(self, user_id: str) -> list[ModelDefinition]:
        """内置定义 ∪ 该用户的自定义定义。"""
        merged = list(self._definitions)
        if self._custom is not None and user_id.strip():
            merged.extend(await self._custom.definitions(user_id))
        return merged

    async def all_models(self, user_id: str) -> list[ModelInfo]:
        """内置模型 ∪ 该用户的自定义模型（浏览器可见投影）。"""
        return [definition.to_info() for definition in await self.all_definitions(user_id)]

    def builtin_base_url(self, provider_id: str) -> str:
        """取内置供应商的端点地址（同一供应商的所有模型共用同一 base_url）。

        内置端点只存在于代码常量里，不落库；用户在设置页往内置供应商下加模型时，
        需要它来快照一份到自定义模型记录上。
        """
        for definition in self._definitions:
            if definition.provider_id == provider_id:
                return definition.base_url
        return ""

    async def resolve_base_url(self, user_id: str, provider_id: str) -> str:
        """解析任意供应商的端点：内置查常量，自定义查库，都没有则返回空串。"""
        builtin = self.builtin_base_url(provider_id)
        if builtin:
            return builtin
        if self._custom is None or not user_id.strip():
            return ""
        try:
            return await self._custom.provider_base_url(user_id, provider_id)
        except Exception:  # noqa: BLE001 - 供应商不存在与端点缺失对调用方是同一件事
            return ""

    async def find_for(self, user_id: str, model_id: str) -> ModelDefinition | None:
        """按稳定模型 ID 查找定义（内置优先，自定义按需查库）。

        非自定义 ID 不会触达数据库：内置模型是绝大多数请求的路径，
        为它们多跑一次查询没有意义。
        """
        builtin = self._by_id.get(model_id)
        if builtin is not None or not model_id.startswith(CUSTOM_MODEL_PREFIX):
            return builtin
        if self._custom is None or not user_id.strip():
            return None
        return await self._custom.find(user_id, model_id)

