---
name: skill-creator
description: 创建或更新 EDITH Skill，把可复用的工作方法、领域知识和资源组织成结构清晰的 Skill。当用户要求新增 Skill、修改已有 Skill、为某类任务沉淀流程时使用。
---

# EDITH Skill 创建指南

Skill 用来描述 Agent **如何完成一类任务**；Tool 用来执行具体动作。

```text
Skill = 工作方法、规则、判断流程
Tool  = 文件操作、进程操作、外部 API 等执行能力
```

## 存放位置

EDITH 有三个作用域，区别是**在多大的范围里可见**：

```text
skills/system/<skill-name>/                      平台 Skill：平台提供，只读
skills/custom/<skill-name>/                      用户 Skill：该用户所有工作区共享
skills/project/<workspace-id>/<skill-name>/      项目 Skill：只在当前工作区生效
```

### 作用域判定

**作用域由「用户希望它在哪些范围生效」决定，不由「Skill 内容涉及什么」决定。**
内容与某个项目有关（例如总结这个仓库的代码审查方法），不等于作用域属于这个项目。

| 用户表达 / 实际意图 | 目标根 |
| --- | --- |
| 「我的 Skill」「以后都用」「其他项目也能用」 | `skills/custom/` |
| 沉淀可复用流程、领域知识、工作方法，但没说范围 | `skills/custom/` |
| 「只在当前项目用」「仅对这个仓库生效」「绑定这个工作区」 | `skills/project/<workspace-id>/` |
| 只是拿「这个项目」「这个仓库」当任务背景 | 默认 `skills/custom/` |

同样的背景，两种意图落到不同层：

- 「总结这个仓库的代码审查方法，以后其他项目也能用」→ 用户级
- 「建一个只适用于当前仓库的代码审查 Skill」→ 项目级
- 「分析这个仓库的部署方式」→ 先判断用户是否真的要求沉淀 Skill，多数情况下不是

两种解读都说得通时选**用户级**：覆盖面更大，项目级可以日后再从用户级派生，反过来不行。

### 作用域约束

1. `skills/system/` 只读，任何情况下都不能修改。
2. 用户级写入 `skills/custom/<skill-name>/`。
3. 项目级写入 `skills/project/<当前 workspace-id>/<skill-name>/`。
4. 项目级必须有真实的当前 workspace 上下文；**没有工作区时不允许创建项目级**。
5. workspace-id 只能取自 System Instruction 给出的真实值。不要猜测、编造，也不要把 `<workspace-id>` 之类的占位符拼进路径——路径校验会拒绝，随后「换个能写的路径重试」往往正好落错层。
6. 同名时**项目级只在当前工作区覆盖用户级**：用户级那条仍然存在、不被删除，删掉项目级就自动恢复。
7. EDITH Skill 存储与项目自己的 `workspace/<workspace-id>/skills/` 是**两个命名空间**。项目里的 `skills/` 属于该项目，不是 EDITH Skill 的存放位置，写进去不会报错但永远不会被加载。

### 路径写法

两条规则作用在不同层，别混用：

| 场合 | 写法 | 例 |
| --- | --- | --- |
| Sandbox 文件工具（`sandbox_read_file` / `write_file` / `make_directory` / `list_files`） | 相对 Sandbox 根 `/home/user`；首段必须是 `uploads` / `work` / `artifacts` / `workspace` / `skills` 之一 | `skills/custom/demo/SKILL.md` |
| 在 shell 里跑脚本（`init_skill.py --path`、`quick_validate.py`） | 真实绝对路径 | `/home/user/skills/custom` |

禁止：`..`、别人的 workspace ID、以及上面两层映射之外的目录。
Skill 内部引用自己的 `references/`、`scripts/`、`assets/` 时，用相对于 Skill 根目录的路径。

索引（`overview.md`）由服务端扫描目录派生，**不需要也不应该手动维护**：改了 Skill 目录就等于改了索引。

## 目录结构

最小 Skill 只需要一个 `SKILL.md`：

```text
<skill-name>/
├─ SKILL.md          必须：摘要和完整规则
├─ edith.yaml        可选：EDITH 页面展示信息
├─ references/       可选：按需读取的详细资料
├─ scripts/          可选：需要稳定执行的脚本
└─ assets/           可选：模板、图片等输出资源
```

`SKILL.md` 必须以 YAML 头部开始，并且只保留 `name` 和 `description`：

```markdown
---
name: daily-summary
description: 帮助用户创建每日总结任务；当用户提到日报、每日总结或定时提醒时使用。
---

# 每日总结

这里写 Agent 执行这类任务时必须遵守的规则和步骤。
```

`name` 使用小写字母、数字和连字符，目录名与 Skill 名保持一致；`description` 同时说明能力和触发场景。

`edith.yaml` 只负责页面展示，不参与 Agent 的触发判断：

```yaml
display_name: 每日总结
short_description: 创建和管理每日总结任务。
```

## 编写原则

1. **只写模型不知道的内容**：删除常识和空泛背景，优先保留规则、边界和决策条件。
2. **先写稳定流程**：把必须遵守的步骤写清楚；可变化的部分给 Agent 合理选择空间。
3. **保持短小**：`SKILL.md` 只放主流程，详细资料放到 `references/`，脚本放到 `scripts/`。
4. **渐进加载**：先让 Agent 看到摘要，只有任务需要时才读取正文和资源。
5. **一层引用**：`SKILL.md` 直接链接参考文件，不要继续嵌套多层引用。
6. **不要堆无关文件**：不要创建 README、安装说明、变更日志等辅助文档。

## 创建流程

按下面顺序完成，不要只写一个空目录：

```text
确定 Skill 的任务边界和触发场景
        ↓
判断用户希望它在哪些范围生效（见「作用域判定」）
        跨项目复用 / 未限定项目  → skills/custom/
        明确限定当前工作区      → skills/project/<当前 workspace-id>/
        ↓
确认目标根目录；项目级把 <当前 workspace-id> 换成 System Instruction 里的**真实 ID**
（拿不到真实 ID 就退回用户级，不要把占位符写进路径）
        ↓
确定 name、description 和目录名
        ↓
检查 <目标根>/<skill-name>/ 是否已存在
        ↓
创建目录并写入 SKILL.md
        ↓
按需添加 references、scripts 或 assets
        ↓
重新读取并检查格式、路径和规则是否完整
        ↓
确认目标文件真实存在且内容正确后，才向用户报告创建成功
```

使用 Sandbox 文件工具创建 Skill：

1. 用 `sandbox_make_directory` 创建目录。
2. 用 `sandbox_write_file` 写入 `SKILL.md` 和 `edith.yaml`。
3. 用 `sandbox_read_file` 重新读取文件，检查 YAML 头部、正文和展示字段。
4. 用 `sandbox_list_files` 确认目录只包含实际需要的文件。

不需要手动更新 `overview.md`：服务端读目录生成索引，写入后下一轮自动生效。

Skill 自带三个可复用脚本：

```text
skills/system/skill-creator/scripts/init_skill.py
skills/system/skill-creator/scripts/quick_validate.py
skills/system/skill-creator/scripts/sync_overview.py
```

`init_skill.py` 建骨架，`--path` 用绝对路径：

- 用户级用默认值 `/home/user/skills/custom`，可以省略 `--path`。
- 项目级**必须显式传** `--path /home/user/skills/project/<真实 workspace-id>`；没有工作区就不要建项目级。
- 它只检查目标目录是否存在（已存在直接失败，不会静默合并），**不校验目标是否在
  `skills/custom/` 或当前工作区的 `skills/project/<ws>/` 下**。所以作用域必须先自己判定，
  别把脚本默认值当成结论。

`quick_validate.py` 用于自查格式。`sync_overview.py` 生成的是**指定目录**的临时预览
（默认用户级 `skills/custom/`）：一次只看一层，不做用户级与项目级的合并视图，
也不同步回服务端。索引始终以服务端扫描结果为准，不要为了「让索引生效」去跑它。
这些脚本只使用 Python 标准库，不依赖额外安装包。

## 更新已有 Skill

- 先读取现有 `SKILL.md`，理解原有触发条件和规则。
- 只修改 `skills/custom/` 或 `skills/project/<当前 workspace-id>/` 里的内容，不动 `skills/system/`。
- **改作用域不是改内容**：用户说「改这个 Skill」就改它原本所在的层。不要因为它的内容跟某个项目有关，
  就顺手把它搬到项目级；要搬得用户明确要求。
- 同名 Skill 可能两层都有一份，默认改**用户级**那份；只有用户指明「这个项目里的」才动项目级。
- 保留稳定的 `name`；除非用户明确要求，不要随意改名。
- 修改后重新读取完整文件，确认摘要仍准确、正文没有互相矛盾的规则。

## 运行时边界

```text
Skills 摘要 ──► AgentRun ──► ManagedRunner
完整正文 ────► Agent 按需调用 Sandbox 文件工具读取
```

不要把完整 Skill 正文无条件复制到每次请求的指令中；不要把 Skill 当成 Tool，也不要在 Skill 中伪造用户身份或越过 Sandbox 的路径限制。
