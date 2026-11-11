from pathlib import Path

from e2b import Template

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 指向当前在维护的 Python 后端；原先指向已废弃的 Go 版 backend-v2，
# 若该目录不随仓库发布，模板构建会因找不到源目录而失败。
SKILLS_SOURCE = "backend-py/src/edith_backend/internal/skills/system"

# 构建上下文 = 整个仓库，而真正的输入只有 SKILLS_SOURCE 一个目录。
# 不加这个清单，`.env`（含 E2B_API_KEY / CLERK_SECRET_KEY）、`node_modules`、
# `.git`、SQLite 库、工作区卷都会被上传到第三方构建服务——既是密钥外泄面，
# 也让每次构建白传几百 MB。这里只做黑名单，漏掉新目录顶多变慢，不会构建失败。
IGNORE_PATTERNS = [
    # 密钥与本地配置
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "*.code-workspace",
    # 版本控制 / IDE / 助手缓存
    ".git",
    ".git/**",
    ".workbuddy",
    ".workbuddy/**",
    ".idea",
    ".idea/**",
    # 依赖与构建产物
    "**/node_modules",
    "**/node_modules/**",
    "**/.venv",
    "**/.venv/**",
    "**/__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/.next",
    "**/.next/**",
    "**/dist/**",
    "**/build/**",
    # 数据与用户内容（模板不需要，且体积大）
    "*.db",
    "*.db-*",
    "*.db.*",
    "workspace-volumes/**",
    "user-skills/**",
    "project-skills/**",
    "docs/**",
    # 其它应用与历史目录：只有 backend-py 的 skills/system 是输入
    "web",
    "web/**",
    "web-v1",
    "web-v1/**",
    "web-v1-tmp",
    "web-v1-tmp/**",
    "backend",
    "backend/**",
    "backend-v1",
    "backend-v1/**",
    "backend-v2",
    "backend-v2/**",
    "backend-py-tmp",
    "backend-py-tmp/**",
    "edith-studio",
    "edith-studio/**",
    "e2b-template",
    "e2b-template/**",
]

template = (
    Template(file_context_path=PROJECT_ROOT, file_ignore_patterns=IGNORE_PATTERNS)
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
