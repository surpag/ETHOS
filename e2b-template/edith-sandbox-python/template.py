from pathlib import Path

from e2b import Template

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 指向当前在维护的 Python 后端；原先指向已废弃的 Go 版 backend-v2，
# 若该目录不随仓库发布，模板构建会因找不到源目录而失败。
SKILLS_SOURCE = "backend-py/src/edith_backend/internal/skills/system"

template = (
    Template(file_context_path=PROJECT_ROOT)
    .from_image("e2bdev/base")
    .make_dir(
        [
            "/home/user/uploads",
            "/home/user/work",
            "/home/user/artifacts",
        ],
        user="user",
    )
    .make_dir(
        [
            "/home/user/skills/system",
            "/home/user/skills/custom",
        ],
        user="root",
    )
    .copy(
        SKILLS_SOURCE,
        "/home/user/skills/system/",
        user="root",
    )
)
