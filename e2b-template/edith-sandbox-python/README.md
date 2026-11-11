# edith-sandbox-python - E2B Sandbox Template

这是一个 E2B sandbox template，允许你在受控环境中运行代码。

## 前置条件

开始之前，请确保你具备：
- 一个 E2B 账号（在 [e2b.dev](https://e2b.dev) 注册）
- 你的 E2B API key（从 [E2B dashboard](https://e2b.dev/dashboard) 获取）
- 已安装 Python

## 配置

1. 在项目根目录创建 `.env` 文件，或设置环境变量（两个都必须有）：
   ```
   E2B_API_KEY=your_api_key_here
   EDITH_E2B_TEMPLATE=edith-sandbox-python
   ```

`EDITH_E2B_TEMPLATE` **不是可选参数**：它就是模板在 E2B 账号里的名字，后端用同一个
变量去 `AsyncSandbox.create(template=...)`。两边取值必须逐字相同，否则模板建成功、
沙箱却永远 404 `template 'xxx' not found`（2026-11-11 事故）。

## 安装依赖

```bash
# 本机
pip install "e2b>=2.35.0" python-dotenv

# 服务器（Ubuntu，无 Windows 的 .venv/Scripts/ 结构）
python3 -m venv .venv && . .venv/bin/activate
pip install -U "e2b>=2.35.0" python-dotenv
```

## 构建 Template

```bash
# 开发环境（名字会加 -dev 后缀）
make build-dev

# 生产环境
make build-prod
```

没有 make 时等价于 `python3 build_prod.py`。**注意**：目标名不能带冒号——旧的
`make e2b:build:prod` 在 GNU make 下会报 `target pattern contains no '%'` 并中止。

构建会用**当前 key 所属的 E2B 团队**落库。换 key（换账号/团队）之后模板即不可见，
必须用新 key 重建；模板名在客户端不变。

构建完成后用同一把 key 自检，再重启后端（模板名是后端启动时读入的）：

```bash
python -c "from e2b import Template; print(Template.exists('edith-sandbox-python'))"
docker compose restart backend
```

`Template.exists()` 返回 `False` 就是「这个账号里没有这个名字」；key 本身无效时它会
直接抛 `AuthenticationException`，不会静默返回 `False`，所以两条信息可以互证。

## 在 Sandbox 中使用 Template

Template 构建完成后，即可在你的 E2B sandbox 中使用：

```python
from e2b import Sandbox

# 创建一个新的 sandbox 实例
sandbox = Sandbox.create('edith-sandbox-python')

# 你的 sandbox 已就绪！
print('Sandbox created successfully')
```

## Template 结构

- `template.py` - 定义 sandbox template 配置
- `build_dev.py` - 构建开发环境 template
- `build_prod.py` - 构建生产环境 template

Template 创建的 Sandbox 会预置：

```text
/home/user/
├─ uploads/
├─ work/
└─ artifacts/

/home/user/skills/
├─ system/    ← 从 backend-v2/internal/skills/system 复制的公共 Skills（root 所有）
└─ custom/    ← 用户级 Skills Volume 的挂载点
```

`EDITH_E2B_TEMPLATE` 同时控制生产版本名和开发版本前缀（开发版多加 `-dev`）。
本仓库当前使用 `edith-sandbox-python`（后端根 `.env` 取值须一致）。

## 下一步

1. 在 `template.py` 中按需定制 template
2. 使用上述方法之一构建 template
3. 在你的 E2B sandbox 代码中使用该 template
4. 查阅 [E2B 文档](https://e2b.dev/docs) 了解更多高级用法
