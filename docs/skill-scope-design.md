# 项目级 Skill 设计稿：作用域优先级与同名冲突裁决

> 状态：**最终决策版**（D1–D11 已批准，作为实施依据；本文件本身不含代码改动）
> 范围：作用域划分与优先级、同名冲突裁决、存储与权限边界、两级预算、迁移协议
> 前置阅读：`backend-py/src/edith_backend/internal/skills/README.md`（当前实现）、`docs/p0-project-memory-design.md`（项目级记忆，粒度参考）
> 交付节奏：阶段 A（用户级存储，**已完成**）→ B0（收敛解析，行为逐字节不变）→ B1（项目级存储与权限）→ B2（Resolver 与预算）→ B3（迁移工具），见第十一节

---

## 一、结论

| # | 结论 | 依据 |
| --- | --- | --- |
| 1 | 作用域三档：**平台级 / 用户级 / 项目级**，不做会话级 | 见第三节 |
| 2 | 优先级 `项目 > 用户 > 平台`，但**平台命名空间对自定义 Skill 保留**，自定义同名在创建期即拒绝 → 实际有效裁决链只有 `项目 vs 用户` 一级 | 见 5.1 |
| 3 | 优先级由**解析阶段**裁决，产物是「一个 name 最多一条记录」；注入文本里**不允许出现同名两条**。顺序、措辞、提示语都不承担优先级职责 | 见第六节 |
| 4 | 项目级隔离维度是 `(user_id, workspace_id)`；沙箱路径**自带 workspace_id**：`skills/project/{workspace_id}/{name}/` | 见第七节 |
| 5 | 被覆盖（shadow）的用户级 Skill 不删除、不合并，但必须在 HTTP / trace / 日志三处**同源可见** | 见 5.2 |
| 6 | 存量 `user-skills/{user_id}/` **一律不自动迁移**，语义永久保持用户级 | 见第九节 |

### 1.1 决策状态（D1–D11，2026-11-06 冻结，已批准进入实施准备）

| # | 决策 | 状态 |
| --- | --- | --- |
| D1 | `U` 与 `J` 同名 → **J 覆盖 U**。覆盖**仅指当前 Workspace 的 Discovery 结果**，不删除、不改写 U 的原始文件；项目 A 的覆盖不影响项目 B，也不影响用户级原件 | 已批准 |
| D2 | 平台命名空间**不可被自定义覆盖**（新写入拒绝；存量处置见 D8） | 已批准 |
| D3 | 项目级删除后**自动恢复**用户级同名 Skill；`shadows` 每次 Discovery 重算、**不持久化**（见 5.5） | 已批准 |
| D4 | 项目级**独立存储根** `project-skills/{user_id}/{workspace_id}/`，不得与用户级合并（见 7.1） | 已批准 |
| D5 | 沙箱项目路径**包含 `workspace_id`**：`skills/project/{workspace_id}/{name}/`（见 7.2） | 已批准 |
| D6 | **不设会话级** Skill（见第三节） | 已批准 |
| D7 | **Scope 隔离 + 同步状态隔离**：`state_key` / 冲突基线 / 待回拉集合都必须带 `scope`（与 `workspace_id`），不得共用（见第八节） | 已批准 |
| D8 | 存量平台保留名冲突采用 **2-A**：解析期淘汰 + `conflicts` 上报；**不重命名、不删除、不修改**用户原件；新建在创建期拒绝，且**服务端必须二次校验**，不只信任沙箱内 `quick_validate.py`（见 13.2-2） | 已批准 |
| D9 | 编辑入口采用 **3-B**：首期只经 Agent 操作，不新增 HTTP 写接口与设置页；pull 时服务端校验格式/保留名/数量/体积；不合格不进索引并记录原因；**校验必须先于权威副本更新**（见 7.4） | 已批准 |
| D10 | 预算采用 **4-A + 4-A-1**：L1 Discovery 4000 字符（1000/1500/1500）+ L2 单 run 3 个 `SKILL.md` / 60KB；工具层软提醒 + 硬拒绝；拒绝用结构化 Tool Error（见第十节） | 已批准 |
| D11 | 迁移采用 **5-A + 5-B**：单条命令 + `migration.yaml` 批量；默认 dry-run；**复制而非移动**；源永不自动删除；目标同名拒绝覆盖；排除启发式（见 9.1 / 13.2-5） | 已批准 |

> **编号以本节为准。** 本文件早期版本曾用 D4/D5/D6/D7 表示"迁移/预算/编辑入口/独立根"，含义已重排；文中所有 `Dn` 引用均指本表。

两条必须写死的不变量，任何实现都不得违反：

1. **D1 的"覆盖"是视图级的，不是数据级的。** 不得以"用户级已被项目级覆盖"为由删除、移动或改写 `user-skills/{user_id}/` 下的文件。
2. **`conflicts` / `shadows` 是派生结果，不是授权。** 它们由每次 Discovery 重算得出，既不能当作删除原始文件的依据，也不能持久化为"永久覆盖关系"。

---

## 二、现状事实（代码核对）

| 事实 | 证据 |
| --- | --- |
| Skill 存储只有 `user_id` 一个隔离维度 | `skills/`（含 `storage/`）内 `workspace_id` / `session_id` 出现次数 = **0** |
| 后端抽象签名只收 `user_id` | `skills/storage/base.py:91-119`：`ensure(user_id)` / `mount_spec(user_id)` / `read_index(user_id)` / `push/pull(user_id, sandbox)` |
| 注入是两段拼接，天然按作用域分段 | `agentrun/configuration.py:332` `skill_instruction()`：`"可用公共 Skills："` + `"可用用户 Skills："` |
| 索引里的路径行已参数化（B1-1 起） | `skills/storage/local.py:151`：``  - 路径：`{base_path}/{dir_name}/SKILL.md` ``（`base_path` 参数化，用户级 `skills/custom`、项目级 `skills/project/{ws}`；路径用**目录名**而非 frontmatter name） |
| 索引由目录扫描自愈生成 | `skills/storage/local.py:246` `read_index` → `:297` `_scan`（只扫一层，跳过无 `SKILL.md` 的目录与 `.` 开头目录） |
| 沙箱按 `(user_id, session_id)` 复用 | `sandbox/service.py:138` `_load_sandbox_id(user_id, session_id)` |
| push 调用点不携带 `workspace_id` | `sandbox/service.py:488`、`:556`（`_push_skills`，`:322`）；`sandbox/tools.py:550-556` |
| 会话 ↔ 工作区**不是**严格 1:1 | `session/schema.py:126-128` 只约束「一个 workspace 同时只有一个活跃 session」；`session/store.py:123` 用 `COALESCE` 保留首个 `workspace_id`，但每条 `chat_messages` 仍带自己的 `workspace_id` |
| 路径守卫原本只对 `workspace/` 做归属校验，`skills/` 完全不校验（**B1-1 已修**） | `sandbox/workspace.py:21` `workspace_path`：`workspace` 分支（`:58`）判第二段；新增 `skills/project` 分支判第三段，两条规则同构 |

两条容易被忽略的推论：

1. **沙箱是 per session 的，不是 per workspace 的。** 所以项目级 Skill 若用一个固定沙箱路径（如 `skills/project`），跨项目串味的风险不是理论上的 —— 同一 session 的两条消息可以携带不同 `workspace_id`（事实表第 8 行）。
2. **`workspace_path()` 对 `skills/` 不做归属校验**（事实表第 9 行）。一旦项目级路径里出现 `workspace_id`，必须同步给 `skills/project/` 补上同样的第二段校验，否则设计自相矛盾。

---

## 三、为什么不做会话级

会话级在技术上"能实现"（沙箱正好是 per session），但**它是错的抽象**，四条理由按重要性排序：

| # | 理由 | 说明 |
| --- | --- | --- |
| 1 | **Skill 的价值在复用，会话是"用一次就丢"的容器** | 只在单次会话内有效的规则 = 临时指令，那属于约束层已有的 session scope（`session/constraint_store.py:325-335` 明确按 user / workspace / session 三档取约束）。同一需求已有权威载体，再叠一层是重复实现 |
| 2 | **直接打击前缀缓存** | 本项目的跨 run 缓存命中率实测 94.8% / 95.4%，前提是 system prompt 前缀稳定。user 级索引对同一用户跨 run 稳定；session 级索引**每个会话都不同** → 每个会话都是冷启动。这不是"稍微贵一点"，是整条成本模型失效 |
| 3 | **没有生命周期承载面** | 无 UI、无 API、无"我上次那个 Skill 在哪"的查询入口。会话结束后索引指向的目录已随沙箱休眠，用户无法确认它是否还存在 |
| 4 | **不可解释** | 同一句话在两个会话里行为不同，且差异来源不是用户可见的任何设置。这与本项目"任何『这一轮发生了什么』的结论必须三处同源"的约束直接冲突 |

> 一句话：**项目级是"事实的持久化"（这个仓库的规矩），会话级是"上下文的临时偏移"（这一轮的要求），后者归约束层，不归 Skill 层。**

---

## 四、作用域与解析点

### 4.1 单一解析点

```text
platform   源码 system/（只读，随产品发布）
user       user-skills/{user_id}/
project    project-skills/{user_id}/{workspace_id}/
               │
               ▼
       SkillResolver.resolve(user_id, workspace_id)   ← 纯函数，无 IO
               │
               ▼
       ResolvedCatalog（唯一权威产物）
         entries[]     name 唯一，每条带 scope + sandbox_path
         shadows[]     被项目级覆盖的用户级条目
         conflicts[]   无法裁决的条目（保留名 / 同域重名）
         status{}      每层各自的 ok|empty|partial|unavailable
               │
       ┌───────┴────────┐
       ▼                ▼
 skill_instruction()   trace / HTTP / 日志
 只消费 entries        消费 shadows / conflicts / status
```

**解析是纯函数**（输入三份索引 + workspace_id，输出 ResolvedCatalog，无 IO），因此优先级规则可以用单测穷举断言，不需要"跑一遍看看模型听谁的"。

### 4.2 规则集（完整枚举）

| 编号 | 条件 | 裁决 |
| --- | --- | --- |
| R0 | `workspace_id` 为空（纯聊天会话） | project 层视为 `empty`，不参与解析。**不报错**——没有工作区不是故障 |
| R1 | name 只在一个作用域出现 | 直接注入该条 |
| R2 | name 命中平台保留名（`system/` 下已存在的 name） | 该 name 的**全部自定义版本**（user 与 project）都不注入，进 `conflicts`。不与 U/J 之间谁优先纠缠——平台保留名优先于一切自定义 |
| R3 | name 在 U 与 J 同时存在，且不是保留名 | **J 胜**。U 版本不注入，进 `shadows` |
| R4 | 同一作用域内两个目录解析出同一个 name | 按 `(目录名)` 升序取第一个，其余进 `conflicts`，`status=partial` |
| R5 | 某一层的 status 是 `unavailable` | 该层按"未知"处理：**只注入能读到的层，不猜测抵消关系**，并把该层 status 原样上报（详见 5.3） |
| R6 | `status=partial`（该层部分无效目录） | 已解析出的条目照常参与解析；无效目录进 `conflicts`。失败**不放大**为整层不可用 |
| R7 | 平台层任何 Skill 格式错误 | 维持现状：启动即失败（`skills/catalog.py:38` 抛 `ValueError`）。这是产品缺陷，不是运行期降级项 |

---

## 五、同名冲突矩阵

### 5.1 跨作用域（3 层，7 种组合）

`P` = 平台级，`U` = 用户级，`J` = 项目级；✅ 注入 / ❌ 不注入 / — 该层不存在此 name。

| 场景 | name 存在于 | P | U | J | 裁决理由 | 可见性 |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | P | ✅ | — | — | 唯一 | — |
| C2 | U | — | ✅ | — | 唯一 | — |
| C3 | J | — | — | ✅ | 唯一 | — |
| C4 | P + U | ✅ | ❌ | — | **平台保留名**：`system/` 是产品语义（如 `skill-creator`），允许覆盖会造成「同名两种行为」，且用户无法从路径推断是哪一个在生效 | `conflicts[{name, scopes:[platform,user]}]` |
| C5 | P + J | ✅ | — | ❌ | 同 C4 | `conflicts[{name, scopes:[platform,project]}]` |
| C6 | U + J | — | ❌ | ✅ | **J 更具体**：项目级的存在理由就是"这个仓库要不一样的做法"。若 U 胜，项目级将毫无意义 | `shadows[{name, scope:user, shadowedBy:project}]` |
| C7 | P + U + J | ✅ | ❌ | ❌ | R2 先行：平台保留名淘汰**全部**自定义版本，不与 C6 叠加 | `conflicts[{name, scopes:[platform,user,project]}]` |

矩阵的三个性质（必须被测试覆盖）：

- **完备**：7 种组合覆盖 2³−1，无遗漏，无"其他情况"分支。
- **确定性**：任一组合的产物与遍历顺序、字典序、并发无关（R4 的 tie-break 是显式的）。
- **幂等**：同一输入多次 resolve 得到逐字节相同的 entries。

### 5.2 为什么 R3 选"覆盖"而不是"拒绝创建"或"双列"

| 方案 | 优点 | 致命问题 |
| --- | --- | --- |
| **J 覆盖 U**（采纳） | 心智模型单一："越具体越优先"；一个 name 只对应一个文件，模型不可能选错路径 | 用户的 U 版本在该项目内静默失效 → 必须用 `shadows` + 注入一行提示补偿 |
| 创建期拒绝（要求全局唯一） | 零歧义 | 直接掐死项目级的主要用例：`project A` 需要与 `user` 同名但内容不同。用户只能靠改名绕过，等于把命名负担转移给用户 |
| 双列（两条都注入，靠顺序/措辞） | 信息最全 | 正是要否掉的方案，见第六节 |

补偿措施（有 shadow 时才出现，不影响常态 token）：

```text
（本项目中 `deploy` 由项目级 Skill 提供，与你个人版本同名；如需使用个人版本，请重命名为 deploy-personal。）
```

### 5.3 某一层不可用时的裁决（R5 展开）

`status ∈ {ok, empty, partial, unavailable}`，两层组合共 16 种，归为 4 类：

| 用户层 | 项目层 | 注入 | status 上报 |
| --- | --- | --- | --- |
| ok / empty / partial | ok / empty / partial | 按 R1–R4 正常裁决 | 各自原值 |
| ok | **unavailable** | 只注入 U（**fail-open**） | `projectStatus=unavailable` + reason |
| **unavailable** | ok | 只注入 J | `customStatus=unavailable` + reason |
| **unavailable** | **unavailable** | 都不注入，指令里明确"Skill 索引不可用" | 两者都 `unavailable` |

**为什么 fail-open 而不是 fail-closed**：裁决需要两层的完整名单。项目层读不到时，"U 里哪些被 J 覆盖"是**未知**，无法做减法。此时唯一可执行且诚实的规则是：给出能读到的部分 + 把未知明确上报。fail-closed（因读不到就不注入）会让一次存储抖动把用户所有 Skill 一起抹掉，代价远大于"可能用了旧版本"。

> 注意这与"绝不静默降级"不冲突：降级发生了，只是它**必须出现在 status 里**。

### 5.4 同作用域内重名（R4）

`skills/storage/local.py:296` 的 `_scan` 以**目录名**为 id、frontmatter 的 `name` 为键，两者不一致时（`skills/system/skill-creator/scripts/quick_validate.py` 只在创建时校验，存量不校验）可能出现两个目录同 `name`。

| 处置 | 理由 |
| --- | --- |
| 按目录名升序取第一个 | 确定性；与 `iterdir` 顺序无关（当前 `_scan` 已 `sorted`，但仍需显式 tie-break 规则，否则将来换实现就漂） |
| 其余进 `conflicts`，`status=partial` | 通常是"复制目录忘了改名"，必须看见 |
| 不做大小写折叠 | `skills/system/skill-creator/scripts/quick_validate.py:48` 已限定 name 为小写字母/数字/单连字符，大写不可能出现，折叠是多余规则 |

### 5.5 项目级删除后，用户级是否恢复（D3）

| 问题 | 结论 |
| --- | --- |
| 是否恢复 | **是，自动恢复**。`shadows` 是每次 `resolve()` 的派生结果，不是持久化的覆盖关系；J 消失 → 同名 U 自动回到 `entries`，不需要任何清理动作 |
| 是否持久化覆盖关系 | **不落任何"U 被 J 覆盖"的记录**。`.conflicts/<ts>/` 只存**内容快照**（用于找回被覆盖的本地文件），与覆盖关系无关 |
| 删除生效的判据 | 以**权威副本目录消失**为准（host 侧 `project-skills/{user_id}/{workspace_id}/{name}/` 被删）。索引读的是权威副本，因此下一轮 Discovery 立刻正确 |
| 沙箱残留是否危险 | **不危险**。沙箱里可能还留着 `skills/project/{workspace_id}/{name}/`，直到下一次 `workspace()` 的 push 做删除传播清掉；但新索引指向 `skills/custom/{name}/`，**路径不同 → 不会误读**。这是 7.2「路径带作用域」的第二个收益 |
| 恢复的**前提** | 项目级 `state_key` 必须独立（D7 / 第八节）。若与用户级共用共识集合，"删项目级"会被推演成"用户级也删了" —— 那时就**没有源可恢复** |
| `shadows` 生命周期 | 每次 `resolve()` 由当前权威副本重算；**不写库、不写文件、不跨 run 复用**。它是"这次 Discovery 中谁被压住了"，不是"谁永久被禁用" |
| `conflicts` 生命周期 | 同上，每次重算。承载两类：平台保留名冲突（D8）、同作用域重名（R4）。**不进入 `entries`**，只走 trace / HTTP / 日志 |
| 能否作为删除依据 | **不能，任何情况下都不能。** 两者的存在只影响"这一次注入什么"，不影响磁盘上有什么 |

> 因此第八节的 `state_key` 隔离不是性能优化项，而是 D3 的**前置条件**：恢复能力依赖删除传播不越界。

---

## 六、为什么优先级不能靠注入顺序

| 论点 | 说明 |
| --- | --- |
| **顺序是最弱的信号** | 两条同名条目只差一个路径，模型没有任何可靠依据判断"上面那条更新"。实测里它会被措辞、描述长度、出现位置同时影响 → 行为不可复现 |
| **不可断言** | "只有一条"可以直接 `assert len(entries) == 1`；"两条但上面那条优先"只能靠 prompt 评估，回归成本高一个数量级 |
| **和缓存冲突** | 两条都在意味着索引更长、且顺序一旦调整（比如按 scope 重排）就改变前缀 → 无谓的缓存失效 |
| **和本项目已定约束冲突** | `agentrun` 的既有原则是"结论必须单点派生、三处同源"（`derive_outcome` 那类）。优先级是**结论**，不是**提示**，必须由代码派生 |

因此：

- 注入列表里 name **唯一**，出现在列表里就意味着"已胜出"。
- 每条记录的 `scope` 标签只做**解释**（让人和模型知道它从哪来、去哪读），**不做裁决**。
- `shadows` / `conflicts` **不进 entries**，只进 status 侧信道（trace / HTTP / 日志）。

---

## 七、存储与路径

### 7.1 权威副本布局

```text
backend-py/
├── user-skills/{user_id}/{skill}/SKILL.md          # 用户级（现状，不变）
└── project-skills/{user_id}/{workspace_id}/{skill}/SKILL.md   # 项目级（新增，独立根）
```

**为什么必须是独立根，而不是嵌进 `user-skills/{user_id}/{workspace_id}/`**：

| 风险 | 说明 |
| --- | --- |
| push 会连带推走 | user 的 push 是"整个 root → `skills/custom`"（`skills/storage/local.py:327`）。把项目目录嵌进去，项目 Skill 会被一起推到 `skills/custom/{workspace_id}/...`，路径错乱且无法区分 |
| 索引扫描污染 | `_scan` 只扫一层，`{workspace_id}` 目录无 `SKILL.md` 会被静默跳过 —— 看着能用，但只要某个项目名与某个 Skill 名相同就互相吞掉 |
| 删除传播互相误伤 | `state_key` 是删除传播的共识集合键（`skills/storage/local.py:208`）。同根共用一套基线 → 删一个项目 Skill 可能推演出"用户 Skill 也消失了" |

独立根带来的是三件隔离：**独立目录、独立 `state_key`、独立 push 目标**。同步函数已按 `mount` + `state_key` 参数化（`workspace/sync.py`），代价只是多一次调用，不是新造轮子。

### 7.2 沙箱路径为什么必须带 workspace_id

```text
用户级： /home/user/skills/custom/{name}/                 （现状，不动）
项目级： /home/user/skills/project/{workspace_id}/{name}/ （新增）
```

沙箱是 per session 而不是 per workspace（第二节事实 7）。若项目级用固定路径 `skills/project/{name}/`，同一个沙箱可能先后服务两个 workspace，就必须"先清空再推送"，而推送发生在**工具调用时**（`sandbox/service.py:556`），指令注入发生在**更早的 `configuration.load`**（`agentrun/configuration.py:172`）——两者之间存在窗口，模型可能拿着 A 的索引去 B 的目录里读文件。

把 `workspace_id` 写进路径后，这个问题**在结构上不可表达**：两个项目的内容永远在不同的目录，残留目录只会占空间，不会被误读。这是"用结构消灭状态机"而不是"用流程防住竞态"。

配套的两个必须同步修改点：

1. ~~`sandbox/workspace.py` 的归属校验要扩展到 `skills/project/`~~ **已在 B1-1 完成**：第三段必须等于 `current_workspace_id`，且无 workspace 上下文时一律拒绝。

   **2026-11-09 修订（线上故障）**：原实现「缺 ID 段 → `ValueError`（必须补 `workspace_id`）」与「给了 ID 段但调用点没带上下文 → `PermissionError`（没有 workspace 上下文）」互相矛盾 —— 文件面板展开 `skills/project` 撞前者，模型写 `skills/project/{ws}/…` 撞后者（`_op_*` 没收到 `execute()` 里的 `workspace_id`），**任何路径都走不通**，项目级 Skill 在对话里等于不可创建。现在 `workspace/` 与 `skills/project/` 统一为「虚拟命名空间」：缺 ID 段 → 按当前会话补全；ID 段不匹配 → 越权；**没有上下文 → 拒绝（唯一硬拒绝）**。补全不会多出一条浏览别人项目的路（这段永远等于当前会话的工作区）。同步修掉两个入口：`tools.py` 的文件类工具把 `workspace_id` 交给 `_op_*`；`sandbox/http.py` 从 `session_meta` 取会话工作区（文件面板只拿得到 `sessionId`）。顺带关掉一处 fail-open：以前不传 `current_workspace_id` 就跳过校验。
2. ~~`sandbox/tools.py` 的 `_SKILL_PATH_PREFIX` 要改成前缀集合~~ **已在 B2-1 完成**：落到 `_scopes_of_path()` / `_touched_skill_scopes()`（返回 `frozenset` 作用域集合，pull 的目标目录因此只拉命中的那层），并按**目录段**匹配——旧实现的 `str.startswith("skills/custom")` 会把 `skills/custom2/...` 也判成用户级。

### 7.3 后端选择

| 后端 | 用户级 | 项目级 | 说明 |
| --- | --- | --- | --- |
| `local`（默认） | ✅ 现状 | ✅ 首期 | 宿主目录权威副本，复用 `workspace/sync.py` |
| `e2b` | ✅ 现状 | ⚠️ 需 per-workspace 卷 | 现有 `mount_spec(user_id)` 只能挂一个用户卷；项目级要按 `(user_id, workspace_id)` 挂，且 E2B Volumes 未开通（`POST /volumes` 403 `use of volumes is not enabled`） |

结论：**项目级首期只支持 `local`**；`e2b` 下项目层返回 `status=unavailable, reason="project skills require local backend"`，而不是假装为空。

**扩展签名约定（B1 必须遵守）**：**不修改** `SkillStorageBackend` 现有方法签名（全部只收 `user_id`）。B1 新增 `new_scope_backend(scope, kind)` 工厂与一个按 `scope` 参数化的后端实现；`new_skill_backend(kind)` 保留为 `scope=user` 的快捷方式，行为不变。这样 A 阶段的装配点（`main.py:172-174`）无需改动，用户级调用点也不必传假 `workspace_id`。

### 7.4 权威副本的更新事务（D9）

**问题**：现有 pull 是逐文件 `write_bytes`（`workspace/sync.py`）。Agent 在沙箱里把 `SKILL.md` 写坏（少个 `---`、frontmatter 缺 `name`），pull 之后权威副本就坏了 —— 下一轮 Discovery 该 Skill 直接从索引消失。**用户改一次就丢了原来能用的版本**，而 `_scan` 只会把它记成 `invalid`。

**规则**：校验必须发生在权威副本正式更新**之前**，且更新必须是"整体替换"而不是"逐文件覆盖"。

| 步骤 | 动作 | 失败时的行为 |
| --- | --- | --- |
| 1 | pull 到暂存目录 `user-skills/{user_id}/.staging.<ts>/`（项目级同理放在自己的根下） | 丢弃暂存目录，权威副本零变化 |
| 2 | 对暂存目录做完整校验（格式 / 保留名 / 数量 / 体积，见 7.6） | 整份移到 `.rejected/<ts>/`，**旧的有效版本继续服务** |
| 3 | 校验通过 → 目录级原子替换 | 回滚到替换前状态（见下） |

**三个必须写清的实现细节**：

1. **目录替换在 Windows 上不是一次 `os.replace`**。POSIX 是 `rename(2)`，同分区原子；Windows 上目标目录已存在时 `os.replace` 会失败。可行协议是「旧目录 → `.old`，暂存 → 正式，删 `.old`」三步，并且**任一步崩溃都必须能恢复**：下次访问时若正式目录缺失而 `.old` 存在，先回滚 `.old` 再继续。这不是可选的健壮性加分，是"崩溃窗口会不会吃掉全部项目 Skill"的差别。
2. **`.rejected/` 与 `.conflicts/` 语义不同，不能混用**。`.conflicts/<ts>/` 是"覆盖本地之前留存的旧版本"（pull 冲突场景，已有实现）；`.rejected/<ts>/` 是"校验未通过、从未生效的候选版本"。两者都要能被人找到，都不参与同步。
3. **`.edithignore` 必须同步扩充**。现有内容是 `overview.md` + `.edithignore` + `.conflicts`（`skills/storage/local.py:86`）；新增 `.staging*` 与 `.rejected` 后必须一并写入，否则**暂存目录会被 push 进沙箱**，模型会在 `skills/custom/` 下看到一个装满半成品的隐藏目录。`_scan` 会跳过 `.` 开头的目录，所以索引不受影响 —— 但同步不受 `_scan` 约束。

### 7.5 项目级权限校验（上线硬门槛）

**结论**：归属校验必须在**服务端存储层**判，不能靠沙箱内路径。`workspace_path()` 做的是"路径合法性与当前上下文一致"，不是"这个 workspace 属于这个 user"。

| 项 | 要求 |
| --- | --- |
| 唯一入口 | `assert_access(user_id, workspace_id)`，所有作用域操作先过它 |
| 判定依据 | `workspaces` 表 `WHERE user_id = ? AND workspace_id = ?`。落地为 `WorkspaceStore.exists()`（`workspace/store.py:137`，`SELECT 1 ... LIMIT 1`）：只回 bool，**不区分「不存在」与「不属于你」**，避免提供「这个 id 是否存在」的探测器。刻意没复用 `get_local_root()`——普通沙箱工作区的 `local_root` 为 NULL，用它当判据会把它们全判成无权 |
| 必须校验的四个点 | ① `resolve()` 读索引 ② `push` ③ `pull` ④ `delete` / 迁移执行 |
| 为什么路径校验不够 | 沙箱内 `run_command` 可以 `cp`/`mv`/`rm`，完全不过工具层的路径校验；因此路径校验只是纵深防御的第二层，**第一层必须在存储层** |
| 校验失败的表现 | 读 → `status=unavailable, reason="workspace not accessible"`；写 → 拒绝且**不创建任何目录**（不能先建再删，会留下空壳） |
| 是否缓存归属 | **不缓存**。每 run 一次 `SELECT 1` 在 SQLite 上是微秒级；进程内缓存会在权限收回后继续放行，且多副本不一致 |

### 7.6 同步前校验清单（D9 的四类校验）

| 维度 | 规则 | 违规处理 |
| --- | --- | --- |
| 格式 | `SKILL.md` 有闭合 frontmatter，`name` / `description` 非空；`name` 匹配 `^[a-z0-9]+(-[a-z0-9]+)*$`；**目录名 == `name`** | 整份 reject |
| 保留名 | `name` 不在平台 Skill 名单内（D2 / D8） | 整份 reject |
| 数量 | 单 scope 的 Skill 数不超过上限 | 整份 reject，`reason` 写"超出上限 N"；**不静默只收前 N 个** |
| 体积 | 单个 `SKILL.md` 与整个 Skill 目录不超过上限 | 同上 |

阈值是**存储侧护栏**，不属于 D1–D11 已批准内容，给定为建议默认值，实施 B1 开工前需确认：

| 参数 | 建议默认 | 依据 |
| --- | --- | --- |
| 单 `SKILL.md` | 32KB | L2 单 run 60KB 预算下，单份超过 32KB 基本等于"把数据塞进正文" |
| 单 Skill 目录 | 2MB | 资源目录（scripts/assets）合理上限 |
| 单 scope Skill 数 | 100 | L1 4000 字符约容 40 条摘要，100 是存储上限而非注入上限 |

> 校验通过与否**不影响已存在的旧版本**：reject 只拒绝"这一次的候选内容"，不删除磁盘上任何已有内容（7.4）。

---

## 八、`state_key` / 基线 / 并发

| 项 | 现状 | 项目级要求 | 不加会怎样 |
| --- | --- | --- | --- |
| 删除传播键 | `skills-{user_id}`（`skills/storage/local.py:208`） | 已实施为 `project-skills-{workspace_id}-{user_id}`（`storage/project.py`），与用户级前缀不同名 | 两个项目的共识集合互相污染 → 删 A 的 Skill 会试图删 B 的 |
| 冲突基线 | `(user_id, sandbox_id)`（`skills/storage/local.py:194`） | `(scope, user_id, workspace_id, sandbox_id)` | 同一 session 跨 workspace 时会把 A 的基线当成 B 的 → 该留的冲突快照没留 |
| 待回拉集合 | `(user_id, session_id)`（`sandbox/service.py:69` / `:375`） | `(user_id, session_id, workspace_id)` | A 项目的挂起回拉会被 flush 到 B 的目录 |
| 锁 | per-user `asyncio.Lock`（`skills/storage/service.py:41`） | per-`(user_id, workspace_id)` | per-user 会把不同项目的同步串行化，纯性能损失，不是正确性问题 |

`_safe_segment()`（`skills/storage/local.py:98`）已同样应用于 `workspace_id`（`storage/project.py` 构造时规范化） —— 它和 `user_id` 一样会被拼进文件系统路径。

---

## 九、存量 Skill 不自动迁移

| 项 | 结论 |
| --- | --- |
| 自动迁移 | **不做**。任何"发现 `user-skills/{user_id}/x` 像项目 Skill 就搬走"的启发式都是不可逆的数据搬动，判错的代价是用户的 Skill 静默换作用域 |
| 语义 | `user-skills/{user_id}/` 永远 = 用户级，不因新功能而改变 |
| 显式迁移（**必须提供**，见 13.2-5） | 只提供脚本，且必须是 **copy → 校验内容一致 → 由用户确认后才可以删源**；绝不隐式删除 |
| 新建默认作用域 | 通过 `skill-creator` 新建时**显式要求选择作用域**，不静默猜。允许的预选值：当前会话带 `workspace_id` → 项目级，否则用户级；预选值必须写进注入指令，让模型知道自己正在往哪一层写 |

### 9.1 批量迁移的执行协议（D11）

`5-B` 的 `migration.yaml` 必须**先整体预检查、后执行**，且执行阶段仍逐条独立记录。

| 阶段 | 动作 | 失败处理 |
| --- | --- | --- |
| 0 解析 | 校验 YAML schema：`user` / `skill` / `to`（`project:<ws>` 或 `user`）必填；**未知字段报错而非忽略**（拼错字段名静默失效是最难查的一类） | 退出码 2，不写入 |
| 1 全局预检查 | 对**整个文件**一次做完：① 用户存在 ② workspace 属于该 user（7.5 的 `assert_access`）③ 源 Skill 存在且格式有效 ④ 目标是否已存在（存在 → 该条记 `skipped`，不是 `failed`）⑤ 保留名冲突 ⑥ **批内重复**：两条迁移写到同一目标 → 整体拒绝（顺序会决定结果，不能靠"后者覆盖前者"） | 任一**硬失败** → 整批不执行，退出码 2 |
| 2 执行 | 逐条 `copy → 逐字节校验一致 → 记录`；**每条执行前重新做一次自身检查** | 该条记 `failed`，继续下一条 |
| 3 汇总 | 每条输出 `{index, action: migrated / skipped / failed, reason}` + 三计数 | 有 `failed` → 退出码 1；否则 0 |

两条原则：

- **不做整体回滚。** 迁移是 copy，源还在；自动回滚意味着"删掉已成功的目标副本"，与"源永不自动删除"对称地危险。责任边界是**逐条记录 + 可重复执行**（重复跑同一份 YAML，已迁的条会因"目标已存在"变成 `skipped`，天然幂等）。
- **预检查与执行之间必须重查。** 全局预检查到逐条执行之间隔着一个窗口，期间 skill 可能被 Agent 改掉、workspace 可能被删。预检查是"快速失败"，不是"授权凭证"。

实现落点：`skills/migrate.py`（命令 `edith-skill-migrate`），协议到代码的映射与三处刻意的实现取舍见第十一节「阶段 B3」。

---

## 十、预算与可观测性（D10）

预算必须**两层**，只做一层都能被绕过：只限索引 → 模型随后把正文全读一遍；只限加载 → 首轮 prompt 就已经爆。

### 10.1 第一层：Discovery 索引预算（进 System Prompt 的摘要）

| 项 | 内容 |
| --- | --- |
| 现状（设计时） | **无任何上限**。每条约 100 字符，200 个 Skill ≈ 20k 字符直接进 system prompt，且还没算正文、其他 System Instruction 与工具定义 |
| 机制 | `SKILL_INDEX_BUDGET_CHARS`（默认 4000），按 scope 分配份额：platform 1000 / project 1500 / user 1500 |
| 截断策略 | 按 `(scope, name)` 确定性截断；**不按使用频率排序**——那会引入跨 run 非确定性，直接打击前缀缓存 |
| 可见性 | 指令里明写"已省略 N 个"，trace 记 `omitted`；两者缺一即视为"静默丢内容" |

**已实施（B2-3）**，落点 `skills/budget.py: plan_index()` + `agentrun/configuration.py: render_skill_instruction()`：

| 细节 | 实现 | 为什么必须这样 |
| --- | --- | --- |
| 份额是**下限**不是上限 | 两轮分配：先各层按份额保底，再把没用完的额度汇成公共池、按固定顺序（平台 → 项目 → 用户）补给被截断的层 | 只有份额时，"用户 50 个 Skill、平台 1 个"会把用户截到 ~15 条，其余 2500 字符直接浪费。而公共池按优先级降序补给，与裁决方向一致——不该出现"平台还有没列出的，用户第 30 条却进了 prompt" |
| 截断以**条目**为单位 | 单条成本超过预算 → 整条省略 | 半个摘要比没有更糟：模型会以为描述就到那里为止 |
| 成本 = 渲染后的整行 | 含 scope 标签、name、description、以及目录名（仅在不等于 name 时） | 用"条目数"当初量会让长描述悄悄撑爆；成本必须与被计量的东西同源 |
| 溢出方向固定 | `INDEX_ORDER = (platform, project, user)` | 换个顺序，同样的数据会截出不同结果 → 前缀缓存每次都在变 |

### 10.2 第二层：Skill 加载预算（单 run 实际读的正文）

**计量口径**（必须按此实现，否则"3 个 / 60KB"会有多种解释）：

| 指标 | 限制 | 计量方式 |
| --- | --- | --- |
| Skill 数量 | 3 个 | 单 run 内**成功加载的不同 Skill 数**（同一 Skill 重复读不重复计数） |
| 正文体积 | 60KB | 单 run 内**累计向模型返回**的正文字节数；**重复读同一 Skill 也累计**（否则可以反复读取绕过上下文预算） |

由此推出两个实现细节：

- **按"返回字节"计，不按"文件大小"计。** `sandbox_read_file` 有 `offset`/`limit`（默认 200 行），一个 100KB 的 `SKILL.md` 分 5 次读完，累计必须是 100KB 而不是 100KB×5，也不能因只读了片段就按整份文件计入。
- **分页读同一 Skill：数量算 1，体积按实际累加。** 这是"数量"与"体积"必须分开计的根本原因——只数调用次数会让分页读取被误杀，只数文件会让反复读取绕过预算。

**软提醒与硬拒绝的分界**（不依赖模型自觉）：

| 维度 | 预警（返回剩余额度，继续放行） | 硬拒绝（返回 error） |
| --- | --- | --- |
| 数量 | 加载第 **3** 个时（成功后剩余 0） | 第 **4** 个 |
| 体积 | 累计达 **48KB**（60KB × 0.8，比例可配） | 累计达 **60KB** |

| 项 | 内容 |
| --- | --- |
| 现状（设计时） | **完全无约束**：`sandbox_read_file` 对 `skills/**/SKILL.md` 没有任何配额，模型可以在一轮内把全部正文读进上下文 |
| 失败形态 | 必须是 **tool error 的结构化返回**（走 `tool_error_handling` 那套），不能抛异常——否则模型无法自救（例如改读 `references/` 的片段，或转为只依赖 L1 摘要） |
| 计数点 | `sandbox/tools.py` 的 `sandbox_read_file` 分支，命中 `skills/**/SKILL.md` 时累加；计数作用域是**单 run**，run 结束即清零（不跨 run 累计，否则长会话会逐渐锁死） |
| trace | 记 `loadedSkills`（不同 Skill 列表）与 `loadedBytes`；被拒绝时额外记 `denied` 次数，便于发现"模型在硬撞配额" |

**已实施（B2-3）**，落点 `skills/budget.py: SkillLoadBudget` + `sandbox/tools.py: _charge_skill_body`：

| 决策点 | 实现 | 理由 |
| --- | --- | --- |
| 账本作用域 | `ContextVar`（`bind_skill_load_budget` 在 `agentrun/execution.chat` 入口调用），**不用进程级 dict** | 作用域天然等于 run 所在的 task（`create_task` 复制上下文），并发 run 互不可见；run 结束随 task 消失，不需要 TTL/回收——进程级 dict 漏一次回收就是"上个 run 的额度被下个 run 继承"，极难复现 |
| 未绑定账本时 | 不计量（`charge_skill_load` 返回 `None`） | 预算是产品约束、不是安全边界；绑定点只有一处，由测试钉住，不在每个调用点兜底 |
| "正文"的判定 | `skill_body_key()`：只认 `skills/{system,custom}/{dir}/SKILL.md` 与 `skills/project/{ws}/{dir}/SKILL.md` | `references/`、`scripts/` 与 `skills/custom2/` 都不算；宽一点就会把正常使用误伤，且前缀相似目录会被误判 |
| 键用**目录**不用 name | `skills/project/ws-1/deploy` | 同名的平台版与项目版是两个 Skill，按 name 合并会把两个算成一个 |
| 被拒时**不记账** | `charge()` 在拒绝分支不改 `loaded` | 否则"撞额度 → 重试 → 撞得更狠"会越撞越小 |
| 拒绝不抛异常 | 返回 `{error, kind: command_error, retryable: false, skill, budget, suggestion}` | 抛异常会落进 `tool_failure`（"系统坏了，别重试"），与"额度用完，换策略"是相反的指令 |
| 提前告知 | 注入文本末尾写明"同轮最多 3 个 / 60KB" | 真正的拦截在工具里；这句只是省掉无谓的撞墙（每次撞墙都是一轮工具调用 + 一条错误结果） |

被否掉的两种形态：只写提示词"最多读 3 个"（无强制力）、只计量不阻断（防不住，只是事后知道爆了）。

**已知残余风险（明确接受，不假装不存在）**：`references/` 与 `scripts/` 不计入体积预算。放宽是刻意的——它们是按需片段，计入会把正常使用误伤；代价是理论上可以用大量小文件绕过上下文控制。缓解不是把预算扩到全目录，而是：L1 摘要 + 数量上限 + trace 里的 `loadedBytes` 可发现异常模式。

### 10.3 可观测性

| 项 | 现状 | 要求 |
| --- | --- | --- |
| 状态字段 | `customStatus` / `customReason`（`skills/types.py:83-84`） | ✅ 已加 `projectStatus` / `projectReason` / `shadows` / `conflicts`（B2-2） |
| trace | `skills.summaries` 记 `system/user/userStatus/userReason/userHealed`（`agentrun/configuration.py:180-205`），B2-2 起追加 project 三项与 `shadows`/`conflicts` | ✅ `skills.inject` 记 `injected`/`omitted`/`omittedNames`/`budgetChars`；`completion` 记 `loadedSkills`/`loadedBytes`/`denied`（B2-3） |
| 日志 | 门面单行同步日志（`skills/storage/service.py`） | 同步行必须带 `scope` 与 `workspace`，否则多项目并发时无法归因 |

---

## 十一、分期

阶段划分以 D1–D11 为界：**A 只做用户级存储（已完成）**，**B 才引入 scope**。B 内部再按"先收敛、后扩展"切四步。

### 阶段 A（已完成，待验收）：用户级 Skill 存储

| 交付项 | 落点 |
| --- | --- |
| Local / E2B 后端抽象 | `skills/storage/base.py`（`SkillStorageBackend` + `new_skill_backend`） |
| 本地持久化 | `skills/storage/local.py`（权威副本 `user-skills/{user_id}/`，索引自愈、原子写、冲突快照） |
| Sandbox 同步 | `workspace/sync.py` 参数化（`mount` / `stats` / `verify_content`）+ `sandbox/service.py` 三个触发点 |
| 跨 Session 读取 | `tests/test_skill_cross_session.py`（12 例） |
| 同步失败与状态可观测 | `SkillIndex.status/reason`、`customStatus/customReason`、trace `skills.summaries` |

### A 阶段是否为 B 预留了空间（逐点核对）

| 扩展点 | A 阶段的现状 | B 的扩展方式 | 是否被设计死 |
| --- | --- | --- | --- |
| 后端签名 | 只收 `user_id` | **新增**按 scope 选后端的工厂（`new_scope_backend(scope, kind)`），**不给现有方法加 `workspace_id` 参数** | 否。给用户级调用点传假 workspace 是更坏的设计 |
| 返回结构 | `SkillIndex` / `SkillSyncResult` 是 dataclass | 加字段（`scope` / `shadows` / `conflicts`），不改既有字段语义 | 否 |
| 门面 | `SkillStorageService` 只依赖抽象 | 新增一个 backend 实现 + 工厂分支 | 否 |
| 同步状态键 | `state_key = "skills-{user_id}"`、`_skills_pending` 键 `(user_id, session_id)` | 加前缀 / 加维度 | 否，但**必须**按 D7 隔离 |
| **索引渲染** | `render_overview()` 已参数化为 `(entries, base_path)`；与沙箱内 `sync_overview.py` **逐字节同构** | 项目级索引传入 `skills/project/{ws}`（B1-1 已完成，两处同步改） | 曾经是唯一的硬耦合，现已按「同一格式、参数化根」解决；维护约束仍在：改格式必须两处同时改 |

> 结论：A 的结构没有被设计死，五个扩展点里四个是"加字段/加分支"。唯一需要警惕的是 `render_overview()` ↔ `sync_overview.py` 的同构约束 —— 它在 A 阶段是正确性保证，在 B 阶段会变成"必须同步修改两处"的成本。

### 阶段 B0（**已实施**，行为逐字节不变）：解析点收敛

| 动作 | 说明 |
| --- | --- |
| 新增 `SkillResolver.resolve(user_id, workspace_id)` + `ResolvedCatalog` | 本阶段**只含 platform + user 两路**，`workspace_id` 仅接收不消费 |
| `skill_instruction()` 改为消费 `ResolvedCatalog` | 输出文本**逐字节等价**（仍是"可用公共 Skills：/ 可用用户 Skills："两段） |
| 不动存储、不动路径、不动 HTTP 字段名、不动同步钩子 | 任何一处改动都会让 B0 变成"顺带重构"，无法独立回滚 |

实施结果：`skills/resolver.py`（`SkillResolver` / `ResolvedCatalog` / `ResolvedEntry` / `ScopeReport` / `ScopeDecision`），`agentrun/configuration.py` 的 `skill_instruction` / `run_instruction` 改吃 catalog；验收用例 `tests/test_skill_resolver_golden.py`（15 例，含把改造前渲染函数**原样冻结**成 oracle 的 9 组形状逐字节比对）。全量 **414 passed**（A 阶段基线 399 + 新增 15）。

三处与设计稿原文的偏差，均已在代码注释与 README 中标注：

1. **`resolve()` 是 `async`**，不是字面意义的"纯函数"——用户层要读存储。这里的"纯"指**只读不写、不缓存、不落库**，每次 run 重算；B1 起 `shadows`/`conflicts` 靠这条性质保证"删项目级即恢复用户级"（D3）。
2. **`ResolvedCatalog` 暂带 `user_overview` 字段**（索引原文）。现有注入把用户索引**原文直铺**（不是从摘要重新渲染），去掉它就无法逐字节不变。B1/B3 改为从 `entries` 渲染时删除该字段，届时注入格式一次性切换。
3. **trace 的 `user` 计数口径微调**：由 `len(index.summaries)` 改为解析出的条数。旧口径在 e2b 后端（只给索引原文、`summaries` 为空）会记 `user=0`，而实际注入的文本里有条目——这正是 A 阶段引入 `userStatus` 想消灭的那类"隐形"。此改动让 trace 与"实际注入了几条"一致。

**为什么 B0 必须在 B1 之前**：B1 引入第二个 scope 后，如果还在"一边加作用域、一边改解析结构"，任何回归都无法判断是作用域逻辑错了还是重构错了。B0 的验收标准是**旧行为逐字节不变**，这让 B1 的回归面收窄到"新逻辑本身"。

### 阶段 B1：项目级存储与权限

独立根（D4）→ `state_key`/基线/待回拉键隔离（D7）→ 沙箱路径含 `workspace_id`（D5）→ 路径守卫扩展（D5）→ 归属校验（7.5）→ 更新事务与校验（D9 / 7.4 / 7.6）。

**B1-1 已实施**（存储与守卫，18 例，`tests/test_skill_project_storage.py`）：

| 交付项 | 落点 | 关键性质 |
| --- | --- | --- |
| 独立根（D4） | `storage/project.py` 的 `ProjectSkillBackend` | `project-skills/{user_id}/{workspace_id}/`，与 `user-skills/` 平级；机制全部继承 `local`，只换根目录/沙箱路径/状态键前缀 |
| 状态键隔离（D7） | 同上 + `local.py` 参数化 `state_prefix` | `project-skills-{ws}-{user}` vs `skills-{user}`：两套共识集合不共用。否则「删项目级」会被推演成「用户级也删了」，D3 就没有源可恢复 |
| 沙箱路径（D5） | `sandbox_root = /home/user/skills/project/{ws}`、`index_base_path = skills/project/{ws}` | 两条路径同源计算，索引里的路径行不可能与真实目录不一致 |
| 路径守卫（D5） | `sandbox/workspace.py` 的 `workspace_path` | 与 `workspace/` **同规则**：第三段必须等于 `current_workspace_id`；**无 workspace 上下文一律拒绝**（此时项目级 Skill 本就没被注入，放行等于多一条绕过归属的路） |
| 作用域分发 | `storage/scopes.py` 的 `new_scope_backend` / `SkillScopeStores` | 未知 scope 与 `e2b`+project 返回 `unavailable` + 原因，**不降级成用户级、不假装为空**；项目级门面按 workspace 缓存（否则基线与快路径每次归零，冲突快照不再产生） |
| 顺带修掉的缺陷 | `_scan` / `render_overview` / `sync_overview.py` | 索引的「路径」行原来用 frontmatter 的 `name` 拼目录；目录名 ≠ name 时写出的是不存在的路径，模型读到的是「空 Skill」而不是报错。现在显示名用 `name`、路径用真实目录名，服务端与沙箱脚本**同步修改**，仍逐字节同构 |

**B1-2 已实施**（归属校验 / 更新事务 / 四类校验，32 例，`tests/test_skill_delivery_transaction.py`）：

| 交付项 | 落点 | 关键性质 |
| --- | --- | --- |
| 归属校验（7.5） | `storage/access.py` + `ProjectSkillBackend.assert_access` | 唯一入口 `assert_access(user_id, workspace_id)`；判定用 `WorkspaceStore.exists`（`SELECT 1`，新方法）；**判定在存储层**，沙箱 `run_command` 绕过路径校验也拦得住 |
| fail-closed | 同上 | 未注入判定器 = **拒绝**（`workspace access check not configured`），不是放行；空 id 同样拒绝 |
| 校验先于更新（7.4） | `storage/transaction.py` | pull 落 `.staging-<ts>/` → 校验 → 按**顶层 Skill 目录**粒度整体替换（`.conflicts/`、`.rejected/`、`overview.md` 留在原地） |
| 崩溃可恢复 | 同上 `recover()` | `.old-<ts>` 残留由 `ensure()` 每次访问前处理：正式目录缺失则搬回、两边都在则删旧残留；`commit` 失败**不清理 `.old`**（那是唯一副本） |
| 被拒候选不消失 | 同上 `reject()` | 整份移进 `.rejected/<ts>/`，`SkillSyncResult.rejected` 上报；与 `.conflicts/`（覆盖前留存的旧版）语义分开 |
| 四类校验（7.6） | `storage/validate.py` | 格式（frontmatter / name 正则 / 目录名==name）、保留名（现扫平台 Skill，不缓存）、数量 100、体积 32KB/2MB；**errors 整份拒绝、warnings 放行上报** |
| 不全不替换 | `local.pull` | 单文件失败 / 熔断 / `list` 失败 → 丢弃候选并**还原共识集合**；沙箱返回空树而本地有 Skill → 拒绝（否则替换会删光）；单文件超 2MB 被跳过 → 拒绝（少回拉一个文件 = 替换后少一个文件） |
| 删除传播 | `transaction.commit(allow_delete=)` | 权威有而候选没有的**目录**随之删除；根目录下的文件（`overview.md` 等）不参与删除。**删除需要授权**：只有「本地与该沙箱上次同步后指纹没变」（`in_sync`）才允许，否则只做同名覆盖、保留旧目录，并在 `SkillSyncResult.reason` 里报 `N deletion(s) suppressed`。没有这条，一个内容过期的沙箱回拉一次就会把权威副本裁剪掉（实测 14 → 13） |
| 取消也要收尾 | `local.pull` | 暂存目录建立到 `commit` 之间只有 `_pull_impl` 一个 await，取消只可能落在那里；`CancelledError` 不是 `Exception`，必须单独 `except` 后 `discard(staging)` 再 `raise`，否则 `.staging-*` 永久残留 |
| 暂存不外泄 | `.edithignore` + `is_ignored` 尾部通配 | 新增 `.rejected` / `.staging-*` / `.old-*`；`is_ignored` 支持 `前缀*`（时间戳目录名写不成固定名） |

B1-2 的两处**主动偏离**（均已在代码注释与本文件写明）：

1. **`_touches_user_skills` 返回作用域一项未做**，留给 B2（已在 B2-1 补齐，见下）。它必须与 `_skills_pending` 的键一起改（`(user_id, session_id)` → 加 `workspace_id`），而现在项目级后端还没有接进 `SandboxService`——提前把返回值改成 `"project"` 只会产生一段无人消费的分支。
2. **目录名 ≠ frontmatter name 定为硬错误**（7.6 原文如此）。这让「历史遗留的错名 Skill」在下次 pull 时整份被拒（旧版本继续服务，不是丢数据），修法是让 Agent 把目录改名。若你更希望降级成 warning，需要新开决策编号——理由见 13.4。

### 阶段 B2：Resolver 与预算

R2 / R3 / R4 冲突裁决（D1 / D2 / D8）→ `shadows` / `conflicts` 上报与 trace（5.5 / 10.3）→ L1 索引预算（10.1）→ L2 加载预算（10.2）→ 注入格式**一次性全量切换**。

**这次切换的代价必须说清**：注入格式从"两段"改为"带 scope 标签的统一列表"会改变 system prompt 前缀 → 每个用户的跨 run 缓存失效一次（94.8% 短暂掉到 0% 后爬回）。所以**一次性全量，不做按用户灰度**，否则会长时间维持"两套格式并存"的窗口。

B2 拆成三步，前一步不产出可见变化、但后一步缺它无法开工：

| 子步 | 内容 | 对外可见变化 |
| --- | --- | --- |
| **B2-1（已实施）** | 同步钩子接线：`SkillScopeStores` 装配进组合根、`SandboxService` 双作用域 push/pull、待回拉键带 `workspace_id`、工具层判定返回作用域 | **无**（项目级文件进沙箱但不进 prompt） |
| **B2-2（已实施）** | Resolver 读项目层 + R2/R3/R4 裁决 + `shadows`/`conflicts` 上报 | trace / 诊断接口多出项目层与覆盖记录；**注入文本仍不变**（见下「过渡性质」） |
| **B2-3（已实施）** | L1 索引预算、L2 加载预算、注入格式一次性切换 | **system prompt 前缀变更**（见上段代价） |

**B2-1 已实施**（同步钩子接线，8 例，`tests/test_skill_scope_wiring.py`）：

| 交付项 | 落点 | 关键性质 |
| --- | --- | --- |
| 装配 | `main.py:178-191` | `SkillScopeStores(..., user_service=skill_storage, access_checker=workspace_access_checker(workspaces))` 后只把**分发点**交给 `SandboxService`。收一个门面而不是两个：`SandboxService` 的参数从 `skills: SkillStorageService` 改为 `skill_scopes: SkillScopeStores`，「什么时候算项目级」于是只有一处规则 |
| 双作用域同步 | `sandbox/service.py` 的 `_push_scope` / `_pull_scope` | 两个作用域**各自独立 try**：目录与共识集合本就隔离，共用 try 会让「项目级路径出错」变成「用户级也不推」。顺序仍是先 pull（吸收沙箱改动）再 push（权威覆盖） |
| 作用域取值 | `_scope_service(scope, workspace_id)` | 两种「不成立」都返回 `None` 而不是空存储：未注入作用域存储、项目级没有工作区上下文。**结构性不可用**（`e2b` + 项目级）同样返回 `None`——那种后端每次调用返回 `unavailable` 而不抛错，接在这里等于每次工具调用空跑一次并留一行日志 |
| 待回拉键 | `_skills_pending: set[(user, session, workspace)]` | 键里带 `workspace_id` 是因为**会话可以换绑工作区**：用兑现时刻的上下文去猜会拉错项目。`flush_skill_sync(user, session)` 因此不需要新参数——挂起标记里已经记了当时的工作区 |
| 工具层判定 | `tools.py` 的 `_touched_skill_scopes` | 返回值从 `bool` 改为**作用域集合**（`frozenset`）。命中就只拉命中的那层（写用户级 Skill 不该顺带比对项目级整棵树）；判断不出则挂起，两种作用域一起兜底 |
| 路径前缀改按目录段 | `_scopes_of_path` | 旧实现用 `str.startswith("skills/custom")`，会把 `skills/custom2/...` 也判成用户级 Skill；现在要求 `== CUSTOM_PATH` 或 `CUSTOM_PATH + "/"`，且常量取自 `skills/types.py` 而非再写一遍字面量 |

B2-1 的两处刻意保留（第 1 条已在 B2-2 兑现）：

1. ~~`IMPLEMENTED_SCOPES` 仍是 `(platform, user)`~~ → B2-2 起为 `(platform, user, project)`。翻它的前提是裁决先落地：否则项目层被读进 `entries` 后同名 Skill 会在注入里出现两次。B0 那条不变量断言 `test_b0_keeps_duplicate_names_across_scopes` 已按计划改写为 `test_duplicate_names_across_scopes_are_now_adjudicated`（保留而非删除——它是「解析层语义变过」的唯一记录点）。
2. **`SkillScopeStores.project_supported()`**：`e2b` 下的项目级是 `_UnavailableBackend`，调用点无法用「失败」判断该不该继续，所以给一个显式的配置结论。「为什么不可用」仍由 resolver 的 `ScopeReport(status=unavailable, reason=...)` 上报——同步钩子不该成为第二个（更吵的）真相来源。

**B2-2 已实施**（裁决 + 上报，15 例，`tests/test_skill_scope_resolution.py`）：

| 交付项 | 落点 | 关键性质 |
| --- | --- | --- |
| 裁决唯一落点 | `resolver.py: _adjudicate` | 顺序**不可交换**：先同层去重（R4）、再淘汰保留名（R2）、最后 J 覆盖 U（R3）。R2 先于 R3 的意义是 C7 里项目级不该因为「更具体」而幸存 |
| `entries` 是裁决结果 | `ResolvedCatalog.entries` | name 全局唯一——优先级因此是**数据结构性质**，可以直接 assert，不靠注入顺序推断 |
| 状态侧信道 | `resolver.py: ScopeDecision` | `shadows`（规则内覆盖）与 `conflicts`（需人介入）刻意分开：合并会让「项目覆盖用户」这条正常行为把告警永久点亮 |
| 同层重名的 tie-break | `_dedupe` | 按**目录名**升序，不按 `iterdir` 顺序 / frontmatter 顺序；同时把该层状态改成 `partial`（R6），否则「复制目录忘了改名」只在 `conflicts` 里能找到而 `status` 还说 ok |
| 不可用的层 | `_read_project` + R5 | `unavailable` 的层天然是空列表 → 「只注入读得到的层」不需要额外分支，也**不会凭空产生 shadow**（未知 ≠ 未覆盖） |
| 路径同源 | `project.py: project_sandbox_root / project_index_base` | 索引的「路径」行与 `push` 真正写入的目录由同一个纯函数推导。两边各算一次，规范化差一个字符就是「模型读到空 Skill」——它不报错 |
| 诊断出口 | `configuration.py` trace、`skills/http.py` | trace 记 `project/projectStatus/projectReason/projectHealed` + `shadows/conflicts`；HTTP **只加字段不改语义**：`custom` 仍是「你拥有的」，不随 `workspaceId` 变、也不做裁决（否则设置页会像丢数据），要知道「此刻哪条生效」看 `shadows` |

**B2-3 已实施**（预算两项 + 注入格式一次性切换，26 例，`tests/test_skill_budget.py`）：

| 交付项 | 落点 | 关键性质 |
| --- | --- | --- |
| L1 索引预算 | `skills/budget.py: plan_index()` | 份额是**下限**：先各层保底，未用完的额度汇成公共池按优先级降序补给被截断的层（否则"用户 50 个、平台 1 个"会白扔 2500 字符）。截断以**条目**为单位，成本 = 渲染后整行长度 |
| L2 加载预算 | `skills/budget.py: SkillLoadBudget` | 数量按**不同 Skill 数**、体积按**累计返回字节**（分页读同一 Skill：数量算 1、体积累加）。被拒时**不记账**，否则"撞额度→重试"会越撞越小 |
| 账本作用域 | `ContextVar`，在 `agentrun/execution.chat` 入口绑定 | 作用域天然等于 run 的 task；run 结束随 task 消失，不需要 TTL/回收。进程级 dict 漏一次回收就是"上个 run 的额度被下个 run 继承" |
| 正文判定 | `sandbox/tools.py: skill_body_key()` | 只认三种 `skills/**/{dir}/SKILL.md`；`references/`、`scripts/`、`skills/custom2/` 都不算。键用**目录**（同名平台版与项目版是两个 Skill） |
| 拒绝形态 | `_charge_skill_body()` 返回结构化错误 | `kind=command_error` + `retryable=false` + `suggestion`（改读片段 / 依据摘要）。抛异常会变成「系统坏了别重试」，与「额度用完换策略」相反 |
| 注入格式 | `agentrun/configuration.py: render_skill_instruction()` | 唯一来源是 `entries`（裁决后、name 唯一）；每条带 scope 标签；目录名≠name 时显式标注（`sandbox_path` 用目录名拼的，不写出来模型会去读不存在的目录） |
| 过渡态收尾 | `ResolvedCatalog.user_overview` **已删除** | B2-2 留下的「压住的那条还列在文本里」在结构上不再可能：注入与裁决同源 |

**这次切换的代价（已发生一次，不可回滚）**：system prompt 前缀变了 → 每个用户的跨 run 缓存失效一次。这是刻意的**一次性全量**（不分用户灰度），否则会长时间维持"两套格式并存"的窗口，而两套格式的差异（有没有 scope 标签、路径说明怎么写）根本没法用一套断言覆盖。

### 阶段 B3：迁移工具与前端

`5-A` 单条命令 → `5-B` 批量驱动 + 预检查（9.1）→ （可选）设置页管理入口。

**5-A / 5-B 已实施**（`skills/migrate.py`，命令 `edith-skill-migrate`，16 例，`tests/test_skill_migrate.py`）：

```bash
# 默认 dry-run：只打印计划，一个文件都不写
edith-skill-migrate --user user_x --skill deploy-notes --to project:ws-abc
edith-skill-migrate --file migration.yaml          # 批量
edith-skill-migrate --file migration.yaml --apply  # 真正执行
```

```yaml
# migration.yaml —— 字段严格：多一个、少一个都拒绝执行（退出码 2）
migrations:
  - user: user_x
    skill: deploy-notes
    to: project:ws-abc
  - user: user_x
    skill: scratch
    to: user            # 「确认留在用户级」，记 skipped，不是失败
```

| 交付项 | 落点 | 关键性质 |
| --- | --- | --- |
| 单一判定函数 | `migrate.py: _check()` | 预检查与「执行前重查」共用同一个函数。两处各写一份规则必然有一处漏改，而漏改的表现是「预检查说能迁、执行时写进无权目录」 |
| 写入粒度 | `transaction.new_staging()` + 整目录 rename | 候选先落 `.staging-<ts>/<name>`（前缀已在 `.edithignore` 里 → 不会被扫成 Skill、不会被同步进沙箱），校验通过才 rename。逐文件写会留下「一半新一半旧」的中间态 |
| 内容校验 | `_digest()`（相对路径 → sha256） | **不按 size 判等**：KB 级 markdown 改一个词，字节数常常不变，size 相等恰好是「内容已变」最典型的样子 |
| 幂等 | 目标已存在 → `skipped` | 重跑同一份 YAML 全 skipped、退出码 0。否则「确认全部到位」会永远返回失败 |
| 退出码 | 2 / 1 / 0 | 2 = schema 或预检查失败（**整批未执行**，一个文件都没写）；1 = 有 `failed`（其余已迁）；0 = 全部 migrated/skipped |
| 索引即时刷新 | `project.read_index()` | 迁完立刻重建 `overview.md`，让人当场核对。失败只附 warning 不回滚——文件已就位，下次读取会自愈 |

三处刻意的实现取舍（都在代码注释里写明，避免下次被当 bug 改掉）：

1. **「用户存在」放宽到「`users` 有行 **或** 磁盘上有该用户目录」**。这个工具存在的意义正是处理库与磁盘不同步的存量数据（库重建过、Skill 由运维直接拷入），严格要求 `users` 行会让它在最需要的时候恰好不可用。而拼错的 `user_id` 既没有行也没有目录，仍然失败——放宽不漏「打错字」。
2. **数量上限按「现有 + 1」判**，不是「现有是否已超限」。`validate_skill_tree` 只在 `> 100` 时报警，照着用会让第 101 个先写进去再在别处被判死，本次迁移就成了那次污染。
3. **`to: user` 记 `skipped` 而不是 `failed`**。批量里混一条「确认留在用户级」是正常用法，把它判死会让整批被拒。

**前端（设置页管理入口）尚未实施**：当前只能命令行迁移，设置页仍只有用户级列表 + `project*` / `shadows` / `conflicts` 只读字段（B2-2 已就绪，等前端接）。

---

## 十二、明确不做

- 会话级 Skill（第三节）。
- 同名自动合并 / 内容 diff 提示（合并语义无法在不读正文的情况下裁决，且会把"优先级"变成"语义协商"）。
- Skill 跨项目"继承"（`project → parent project`），无需求，且会让解析从 3 层变图。
- 存量自动迁移（第九节）。
- `e2b` 后端的项目级支持（7.3，等 Volumes 开通再评估）。

---

## 十三、决策记录与待批候选

### 13.1 已冻结

见 **1.1 决策状态**（D1–D11）。D1–D11 已全部批准，本文件即为实施依据。本节保留候选方案的原始对比，作用是记录「为什么选它」，防止实施期被「看起来更省事」的替代方案悄悄替换（见 13.4）。

### 13.2 候选与批准结果（D8–D11）

第 1 项（U/J 同名）为 D1；其余四项均采纳推荐候选，编号 D8–D11。

#### 2｜平台保留名：存量冲突的处置 → **D8，批准 2-A**

新写入已定（拒绝）。规则生效前已创建的同名自定义 Skill 有 4 种处置：

| 候选 | 做法 | 代价 | 评价 |
| --- | --- | --- | --- |
| **2-A** | 解析期淘汰自定义版 + 进 `conflicts` 上报 | 用户的 Skill 在列表里消失，但能看到原因 | **推荐**：平台语义完整，且不静默 |
| 2-B | 解析期淘汰，不上报 | 故障隐形 | 违反"绝不静默降级" |
| 2-C | 自动重命名用户的 Skill | 改动用户数据、不可逆 | 否决 |
| 2-D | 启动即失败 | 用户数据不在启动路径上，做不到 | 否决 |

拒绝点有两处：`skills/system/skill-creator/scripts/quick_validate.py`（沙箱内可读 `skills/system/` 目录名，模板已 COPY，见 `template.py:28`）负责创建时拦截；服务端解析期（R2）负责存量兜底。

#### 3｜编辑入口 → **D9，批准 3-B**（含「校验先于更新」约束，见 7.4）

| 候选 | 做法 | 代价 | 评价 |
| --- | --- | --- | --- |
| 3-A | 纯 Agent 沙箱操作，服务端只读 | 零改动；但坏 Skill 要到解析期才被发现 | 不完整 |
| **3-B** | 3-A + pull 时服务端校验（格式 / 保留名 / 数量与体积上限），违规不进索引 | 多一处校验逻辑 | **推荐**：满足"先保证 Agent 链路完整"，补上 3-A 缺的校验，且不需要前端 |
| 3-C | 加 HTTP 写接口 + 设置页 | 与沙箱形成双写 → 版本分裂；契约、鉴权、并发都要新增 | 后续 |

删除链路要求：Agent 删除后必须**立即 pull**（`_touched_skill_scopes` 覆盖 `skills/custom` 与 `skills/project` 两类路径，见 `sandbox/tools.py`），否则索引会比实际慢一个 run。判断不出作用域时退回「挂起 + 下一轮/收尾兜底」，两条路都覆盖删除。

#### 4｜预算（两层） → **D10，批准 4-A + 4-A-1**（计量口径见 10.2）

| 候选 | 做法 | 评价 |
| --- | --- | --- |
| **4-A** | L1 索引 4000 字符（platform 1000 / user 1500 / project 1500）+ L2 单 run 3 个 `SKILL.md` / 60KB | **推荐** |
| 4-B | 只做 L1 | 索引小了，但模型可以随后把 200 个正文全读一遍 |
| 4-C | 只做 L2 | 首轮 prompt 就已经爆 |

L2 的执行形态：

| 候选 | 做法 | 评价 |
| --- | --- | --- |
| **4-A-1** | `sandbox_read_file` 命中 `skills/**/SKILL.md` 计数，软到硬两段（先警告+剩余额度，再拒绝） | **推荐**：可自救、可观测 |
| 4-A-2 | 只在提示词里写"最多读 3 个" | 无强制力 |
| 4-A-3 | 只计量不阻断 | 防不住，只是事后知道爆了 |

配额拒绝必须是 **tool error 的结构化返回**（`tool_error_handling`），不能抛异常。

#### 5｜迁移脚本 → **D11，批准 5-A + 5-B**（执行协议见 9.1）

| 候选 | 做法 | 评价 |
| --- | --- | --- |
| **5-A** | 单条动作：`--user U --skill NAME --to project:WS --dry-run/--apply`，copy → 校验 → 源保留 | **推荐**：原子、可回滚 |
| **5-B** | 5-A 之上的批量驱动：`migration.yaml` 列多条，逐条执行 | **推荐**（与 5-A 一起） |
| 5-C | 启发式自动归类（按路径/内容猜归属） | 否决（不可逆的数据搬动） |

硬约束：默认 dry-run、`--apply` 才写、**源永不自动删**、目标同名已存在则拒绝不覆盖、执行后断言两层 name 集合符合预期。

### 13.3 实施约束索引（本轮新增的四条细节）

| # | 约束 | 落点 |
| --- | --- | --- |
| 1 | L2 计量方式：不同 Skill 数 vs 累计**返回字节**；重复读同一 Skill 计入体积不计数量；第 3 个成功即预警、第 4 个拒绝；48KB 预警、60KB 拒绝 | 10.2 |
| 2 | `conflicts` / `shadows` 生命周期：纯派生、每次重算、**不得作为删除依据**、项目级删除后自动恢复 | 5.5 |
| 3 | 批量迁移安全性：整批 6 项预检查、逐条记录、执行前重查、**不做整体回滚**、幂等可重跑 | 9.1 |
| 4 | 项目级权限校验：判定在**存储层**（非路径层）、四点必查、不缓存、不区分"不存在"与"无权" | 7.5 |
| — | 附加：校验**先于**权威副本更新（暂存目录 + 目录级替换 + Windows 三步协议 + `.rejected/` 留存） | 7.4 / 7.6 |

### 13.4 需要新增决策编号的情形

以下任一变化都必须新开决策编号，不得直接改已冻结条款：

- 允许自定义覆盖平台保留名（改 D2 / D8）；
- 把项目级存进 `user-skills/{user_id}/` 之下（改 D4）；
- 沙箱路径去掉 `workspace_id`（改 D5）；
- 把 `shadows` 持久化（改 D3）；
- 把 L2 预算做成"只提示不强制"（改 D10）；
- 把归属校验放到路径层而非存储层（改 D7 / 7.5）。

---

## 十四、测试要求（按决策编号对应）

散落的验收标准集中在此，实施时逐条对照；缺一条即视为该阶段未完成。

| 决策 | 测试 | 断言 |
| --- | --- | --- |
| D1 | U/J 同名 resolve | `len(entries) == 1` 且 `scope == project`；**U 的文件逐字节未变**（前后哈希一致） |
| D2 / D8 | 保留名冲突 | `entries` 无该 name；`conflicts` 含明确原因；磁盘上自定义文件**仍在** |
| D3 | 删除后重算 | 再 resolve → 同名条目回到 `scope == user`；`shadows` 为空；目录快照 diff 为空（没删任何东西） |
| D4 | 布局隔离 ✅ | 项目级根与 `user-skills/` 互不嵌套（B1-1 已覆盖）；user 的 push 不产生 `skills/project/**` 写入、project 的 push 只触碰 `skills/project/{ws}/**`（待 B1-2 接线后补） |
| D5 | 路径守卫 ✅ | ws=A 的上下文访问 `skills/project/{B}/...` 抛 `PermissionError`；**无 workspace 上下文一律拒绝**；`skills/project` 缺 ID 段抛 `ValueError`；自己的路径通过 |
| D7 | 状态键隔离 ✅ | 用户级 / `ws-alpha` / `ws-beta` 三个 `state_key` 两两不同；同一用户跨 workspace 读不到彼此的 Skill；同一 workspace 的两个用户互相隔离 |
| D9 | 校验先于更新 ✅ | 写入坏 `SKILL.md` 后 pull：权威副本仍是旧内容（逐字节），`.rejected/<ts>/` 存在，索引仍列出该 Skill |
| D9 | 崩溃恢复 ✅ | 构造 `.old` 残留 + 正式目录缺失 → 下次访问回滚成功；两边都在 → 删旧残留（Windows 三步协议） |
| D9 | 暂存不外泄 ✅ | push 后沙箱 `list` 里不出现 `.staging-*`；`.edithignore` 含 `overview.md` / `.edithignore` / `.conflicts` / `.rejected` / `.staging-*` / `.old-*` |
| D7 | 归属校验（7.5）✅ | 无权 → 读 `unavailable` + `workspace not accessible`、写拒绝且**一个目录都不创建**；未注入判定器 → `workspace access check not configured`（fail-closed，不是放行） |
| D9 | 候选不完整 ✅ | 单文件读失败 / 熔断 → 丢弃候选、权威副本逐字节不变、共识集合还原（否则下轮删除传播会基于残缺集合误删） |
| D9 | 超限不静默 | 单文件 > 2MB 被跳过 → 整份拒绝、权威副本保留（否则替换会把这个文件删掉，且没有任何提示） |
| D9 | 空树不覆盖 ✅ | 沙箱返回空树而本地有 Skill → 拒绝替换（等价于旧实现的「整体消失不传播删除」护栏） |
| 7.6 | 四类校验 ✅ | 坏 frontmatter / name 非法 / 目录名≠name / 保留名 / 超量 / 超体积 → 整份拒绝；无 `SKILL.md` 的目录 → warning 放行 |
| D10-L1 | 截断 | 见下方 B2-3 的「预算截断 ✅」（口径按**字符成本**，不是条目数） |
| D10-L2 | 计量口径 | 见下方 B2-3 的「计量口径 ✅」（数量与体积分开计，分页不重复计数量） |
| D10-L2 | 软硬分界 | 见下方 B2-3 的「拒绝形态 ✅」 |
| D11 | 批量预检查 | 6 项任一失败 → 整批零写入，退出码 2 |
| D11 | 幂等 | 同一 YAML 跑两次：第二次全部 `skipped`，无覆盖、无删除 |
| 5.1 | 7 组合矩阵 | `parametrize` 覆盖 C1–C7：entries 唯一 + `shadows`/`conflicts` 归属正确 + 幂等（两次调用逐字节相同） |
| B0 | golden（已归档） | B0 时这里是「新渲染 vs 冻结的旧渲染逐字节相等」的 oracle。B2-3 **刻意**改了输出，该文件已改写为「新格式 golden」（见下方 B2-3 行）；oracle 若跟着改就退化成同义反复，所以它是被**替换**而不是被对齐的 |
| D4/D7 | 双作用域同步 ✅ | `tests/test_skill_scope_wiring.py`：同一沙箱里两层分别回拉到各自的独立根；推送时分别落到 `skills/custom` 与 `skills/project/{ws}`；两个 workspace 互不可见 |
| D5 | 无工作区不参与项目级 ✅ | `workspace_id=""` 时只推用户级，沙箱里不出现 `skills/project/**` |
| 7.3 | 结构性不可用不空跑 ✅ | `e2b` 下 `project_supported()` 为假 → `_scope_service(project, ws)` 返回 None，同步钩子跳过（不是「试一次再记 unavailable」） |
| 7.5 | 接线层仍 fail-closed ✅ | 判定器返回 False → push/pull 后**项目级根目录完全不存在**；`_pending_workspaces` 不受影响 |
| D7 | 待回拉键带 workspace ✅ | 同会话先后挂起两个工作区 → 分别兑现；摘掉后不重复兑现（避免重放早已不存在的写入） |
| D7 | 收尾按挂起时的工作区 ✅ | 会话换绑工作区后 `flush_skill_sync(user, session)` 仍回拉挂起时刻的那个项目 |
| 10.2 | 只拉命中层 ✅ | `pull_skills(..., scopes={user})` 时项目级权威副本目录不被创建/更新（写用户级 Skill 不该顺带比对项目级整棵树） |
| 5.1 | 裁决矩阵 ✅ | `tests/test_skill_scope_resolution.py`：R0（无工作区→项目层不存在）、R1（单层）、C4/C5/C7（保留名，C7 产生**两条**记录）、C6（`shadows` 且 `conflicts` 为空）、R4（目录名 tie-break + 该层 `partial`）、R5（无权→`unavailable` 且用户级照常注入、**无** shadow）、R6（同层丢条不改写成整层不可用） |
| D1 | 覆盖但不擦除 ✅ | `test_shadow_is_not_an_erasure`：resolve 后被压住的用户级目录**逐字节仍在**（否则 D3 恢复永远回不来，且无报错） |
| D3 | 删项目级自动恢复 ✅ | 删掉项目级目录后重算 → 同名条目回到 `scope == user`、`shadows` 为空（覆盖关系不落库，是每次重算的派生结果） |
| 5.1 | 确定性 ✅ | 同一磁盘内容重复 resolve，`(name, scope)` 序列逐项相同；顺序恒为 平台 → 项目 → 用户，层内按 name 升序 |
| 只读不写 | 解析无副作用 ✅ | 两次 resolve 前后，两层 Skill 文件字节完全一致（`overview.md` 是派生缓存，自愈回写不算变更） |
| D10-L1 | 预算截断 ✅ | `tests/test_skill_budget.py`：默认 4000/非法值退回；份额是下限（低优先层不被挤没）；溢出按优先级补给被截断层；单条超预算整条省略；重复渲染逐字符相同 |
| D10-L2 | 计量口径 ✅ | 3 个放行、第 4 个 `skill_count`；分页读同一 Skill 数量算 1、体积累加；60KB 拒绝、48KB 只提醒；被拒不记账；未绑定账本不计量；新 run 从零开始 |
| D10-L2 | 正文判定 ✅ | `skill_body_key`：`references/`、`scripts/`、`workspace/`、`skills/custom2/`、缺层的 `skills/project/{ws}/SKILL.md` 一律不计入 |
| D10-L2 | 拒绝形态 ✅ | 第 4 个 Skill 的读取返回 `kind=command_error` + `retryable=false` + `suggestion`（不是异常、不参与熔断） |
| B2-3 | 注入格式 ✅ | `tests/test_skill_resolver_golden.py`（已改写为新格式 golden）：逐字符比对单层文本、三层顺序与标签、目录名≠name 的标注、空 catalog 输出空串、渲染确定性 |
| B2-3 | 注入与裁决同源 ✅ | 保留名冲突时文本里该 name 只出现一次且是 `[platform]`；索引原文的结构标记（`用户 Skills 总览`、`路径：`）不再出现在 prompt 里 |
| B3 | 迁移协议 ✅ | `tests/test_skill_migrate.py`（16）：schema 严格（未知/缺字段/空列表/坏目标 → 退出码 2）、批内重复整批拒绝、外部 workspace 与保留名整批拒绝、`to: user` 记 skipped、dry-run 连目录都不建、apply 后逐字节一致且源仍在 + 项目索引路径正确、重跑全 skipped 且退出码 0、目标已存在不覆盖、copy 后内容不一致 → failed 且目标不落盘、单条失败不影响后续条、作用域已满在写之前拒绝 |
| B3 | 用户存在性 ✅ | `users` 无行但磁盘有目录 → 允许（库与磁盘不同步的存量数据）；既无行也无目录（拼错字）→ 整批拒绝 |
| 全量 | 回归 | 基线逐阶段抬升：A 阶段 399 → B0 414 → B1-1 432 → B1-2 464 → B2-1 472 → B2-2 487 → B2-3 513 → **B3 后 529**（新增 16）。只许上升（`pytest tests`，且必须带 `--basetemp=.pytest-tmp`） |

运行约束（踩过的坑，不要重试）：pytest 只跑 `tests` 目录（`workspace-volumes/` 会被收集进来）；临时目录必须用 `--basetemp=.pytest-tmp`，否则 Windows 下临时目录 GC 会被沙箱拦。
