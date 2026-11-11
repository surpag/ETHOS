import os

from e2b import Template, default_build_logger
from template import template

from build_common import load_build_env, report, resolve_template_name

load_build_env()


if __name__ == "__main__":
    if not os.getenv("E2B_API_KEY"):
        raise RuntimeError("E2B_API_KEY is required in .env")

    # 开发构建固定 `-dev` 后缀；后端要连它就把 EDITH_E2B_TEMPLATE 设成带 -dev 的名字
    template_name = f"{resolve_template_name()}-dev"
    print(f"building template: {template_name}")
    build = Template.build(
        template,
        template_name,
        on_build_logs=default_build_logger(),
    )
    report(build, template_name)
