"""构建脚本共用的配置加载、名字解析与结果上报。

单独成文件而不是复制两份：模板名漂移过两次（构建建到 `edith-v0-1-5`、
后端找 `edith-sandbox-python`），这种规则只该有**一个**实现。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


def load_build_env() -> None:
    """只读仓库根 `.env` —— 与后端 `config.load_environment()` 读的是同一份文件。

    原先这里是裸 `load_dotenv()`：python-dotenv 从**调用者所在目录**向上找第一个
    `.env`，在本目录（`e2b-template/edith-sandbox-python/`）执行时会先命中
    `e2b-template/.env`（未入库、内容是脚手架时代的 `edith-v0-1-5`），于是模板被
    建到 `edith-v0-1-5` 名下，而后端仍在找 `edith-sandbox-python` —— 沙箱永远 404，
    且构建日志一片「成功」（2026-11-11 事故，两次）。

    `override=False`：显式传进来的环境变量优先级更高，方便临时覆盖。
    """
    if not ENV_FILE.exists():
        raise RuntimeError(
            f"未找到 {ENV_FILE}：E2B_API_KEY 与 EDITH_E2B_TEMPLATE 必须与后端来自同一份配置"
        )
    load_dotenv(ENV_FILE, override=False)


def resolve_template_name() -> str:
    """模板名必须显式给出，且与后端 `EDITH_E2B_TEMPLATE` 逐字相同。

    这里曾用 `os.getenv("EDITH_E2B_TEMPLATE", "edith-v0-1-5")` 兜底：一旦构建时
    没带上这个变量，模板会安静地建到 `edith-v0-1-5` 名下，后端仍在找
    `edith-sandbox-python`。名字对不上属于「构建成功但完全不可用」，
    必须在构建时就失败。
    """
    name = os.getenv("EDITH_E2B_TEMPLATE", "").strip()
    if not name:
        raise RuntimeError(
            "EDITH_E2B_TEMPLATE 未设置。模板名必须与后端 .env 里的取值逐字一致，例如：\n"
            "  EDITH_E2B_TEMPLATE=edith-sandbox-python python build_prod.py"
        )
    return name


def report(build, template_name: str) -> None:
    """打印构建产物标识与下一步验证命令，避免「建完不知道建成了什么」。"""
    for field in ("template_id", "build_id", "name"):
        value = getattr(build, field, None)
        if value:
            print(f"{field}: {value}")
    print(
        f"env: {ENV_FILE}\n"
        "done. 下一步用同一把 key 验证，再重启后端（模板名是启动时读入的）：\n"
        f"  python -c \"from e2b import Template; print(Template.exists('{template_name}'))\"\n"
        "  cd ../.. && docker compose restart backend"
    )
