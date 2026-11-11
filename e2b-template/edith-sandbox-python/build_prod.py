import os

from dotenv import load_dotenv
from e2b import Template, default_build_logger
from template import template

from build_common import report, resolve_template_name

load_dotenv()


if __name__ == "__main__":
    if not os.getenv("E2B_API_KEY"):
        raise RuntimeError("E2B_API_KEY is required in .env")

    template_name = resolve_template_name()
    print(f"building template: {template_name}")
    build = Template.build(
        template,
        template_name,
        on_build_logs=default_build_logger(),
    )
    report(build, template_name)
