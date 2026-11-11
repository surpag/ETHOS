"""构建脚本共用的名字解析与结果上报。

单独成文件而不是复制两份：模板名漂移过一次（构建建到 `edith-v0-1-5`、
后端找 `edith-sandbox-python`），这种规则只该有**一个**实现。
"""

import os


def resolve_template_name() -> str:
    """模板名必须显式给出，且与后端 `EDITH_E2B_TEMPLATE` 逐字相同。

    这里曾用 `os.getenv("EDITH_E2B_TEMPLATE", "edith-v0-1-5")` 兜底：一旦构建时
    没带上这个变量（在别的目录跑、或根 `.env` 里没配），模板会安静地建到
    `edith-v0-1-5` 名下，而后端仍在找 `edith-sandbox-python` —— 每次建沙箱都
    404 `template 'xxx' not found`，所有文件工具连带失效（2026-11-11 事故）。
    「构建成功但完全不可用」必须在构建时就失败。
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
        "done. 下一步用同一把 key 验证，再重启后端（模板名是启动时读入的）：\n"
        f"  python -c \"from e2b import Template; print(Template.exists('{template_name}'))\"\n"
        "  cd ../.. && docker compose restart backend"
    )
