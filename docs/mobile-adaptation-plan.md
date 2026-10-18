# 移动端适配方案（待实施）

> 状态：**方案已定，代码未改**。用户 2026-10-18 要求先存档，等明确要求后再动手。
> 实施时请先重新核对文件行号（下述行号为 2026-10-18 的代码状态）。

## 1. 触发问题

手机上（约 393~412 CSS px 宽）打开 `https://surpag.de5.net/chat`，聊天区被压成几十像素宽，
header 文字竖排，Sandbox 面板占据大半屏，输入框挤成一条。

## 2. 根因（不是样式问题，是三栏固定宽度在窄屏上无处可放）

| 位置 | 现状 | 手机上的后果 |
|---|---|---|
| `components/chat/sandbox-panel.tsx:186` | `<aside style={{ width: panelWidth }}>`，`panelWidth` 默认 **320**，且 `shrink-0` | 393px 屏上占 81%，聊天区只剩几十像素 |
| `components/app-sidebar.tsx:20` | `hidden ... md:flex` | <768px **完全隐藏** → 没有会话列表、没有新建入口、无法切换会话 |
| `components/chat/chat-page.tsx:643` | `<main className="flex h-screen overflow-hidden ...">` | `h-screen` = 100vh，iOS 地址栏/虚拟键盘导致高度算错，输入框被顶出屏幕 |
| `components/chat/model-picker.tsx:64` | 弹层 `absolute bottom-10 left-0 z-20 w-80` | 320px 浮层 + `left-0` 在手机上横向溢出 |
| `components/chat/session-usage.tsx:54` | 弹层 `w-64` | 同上 |
| `components/chat/workspace-switcher.tsx:429` | 弹层 `min-w-64` | 同上 |
| 全项目 | `sm:` 8 处、`md:` 5 处 | 实质没有移动布局 |

结论：**「侧边栏被隐藏」和「Sandbox 占宽度」是同一个错误的两个面 —— 都假设屏幕够宽。**
只修 Sandbox 不修侧边栏，会话切换/新建在手机上仍然消失；只修侧边栏不修 Sandbox，聊天区仍然被压。

## 3. 问题空间枚举（必须全覆盖）

| 维度 | 取值 |
|---|---|
| 宽度 | <640 手机 / 640–1024 平板 / >1024 桌面 |
| 侧边栏 | 展开 / 收起 / 手机上应为抽屉 |
| Sandbox | 关闭 / 桌面并排 / 手机上应为全屏浮层 |
| 键盘 | 收起 / 弹出（弹出时输入框必须可见） |
| 平台 | iOS Safari（`dvh`、安全区、16px 防缩放）/ Android Chrome |

## 4. 方案对比

### 方案 A · 能用（约 1 小时）

- `h-screen` → `h-dvh`
- `SandboxPanel` 在 `md:` 以下改 `fixed inset-0`
- 手机 header 加汉堡按钮，点开侧边栏（`fixed` 覆盖，无动画无手势）
- 弹层宽度 `w-[min(20rem,calc(100vw-2rem))]`

代价：抽屉朴素，键盘弹出可能遮挡输入框。适合「偶尔看进度」。

### 方案 B · 好用（**推荐**，约半天）

| 改动 | 内容 |
|---|---|
| 新增 `components/ui/drawer.tsx` | 遮罩 + 左侧滑入 + 点遮罩/ESC 关闭 + 焦点锁定 |
| `components/app-sidebar.tsx` | 抽出 `SidebarContent`；桌面仍为 `<aside>`，移动端同一内容渲染进 Drawer |
| `components/chat/sandbox-panel.tsx` | `md:` 以下不参与 flex，改 `fixed inset-0 z-40`，顶部「← 返回对话」 |
| `components/chat/chat-composer.tsx` | 底部 `env(safe-area-inset-bottom)`；输入框 `text-base md:text-sm`（16px 是 iOS 不自动缩放的界线） |
| `app/layout.tsx` | 增加 `export const viewport = { viewportFit: "cover", themeColor }` |
| `components/chat/chat-page.tsx` | `h-dvh`；移动端 `sandboxOpen` 默认 `false` |
| `model-picker` / `session-usage` / `workspace-switcher` | 弹层改 `w-[min(20rem,calc(100vw-2rem))]`；手机端 `left-auto right-0` |

**全部走 `md:` 前缀隔离，桌面端零视觉变化，可单独回滚。**

### 方案 C · 完整（约 1–2 天）

B + 底部 tab 导航（对话/文件/设置）+ 手势返回 + PWA（`manifest.json`、添加到主屏、全屏）+ 窄屏细节
（代码块横向滚动、宽表格 `overflow-x-auto`、消息气泡留白）。

## 5. 决策

**先做 B，不做 C。**

1. 手机上的核心场景是「看 Agent 跑到哪了 + 补一句话 + 翻产出文件」，B 覆盖完整；C 属锦上添花。
2. B 走 `md:` 前缀，桌面零风险。
3. 移动端断流已有兜底：`chat-page.tsx` 的 `pendingRuns`（`sessionStorage`）+ 3 秒轮询会把结果补回来，无需改动。

## 6. 实施顺序（建议分两批验）

1. **第一批（影响最大、风险最低）**：`sandbox-panel.tsx` + 侧边栏抽屉（含 `drawer.tsx`）+ `h-dvh`
2. **第二批**：弹层宽度、安全区、viewport 导出、输入框字号

## 7. 真机验收清单

- [ ] 竖屏 375 / 393 / 430 三档：聊天区占满宽度，无横向滚动条
- [ ] 汉堡打开抽屉 → 能看会话列表、能新建会话、点会话能切换并自动关闭抽屉
- [ ] Sandbox 全屏打开 → 「返回对话」可回退，文件树可展开、可下载
- [ ] 键盘弹出时输入框可见（iOS Safari 与 Android Chrome 各测一次）
- [ ] 输入框聚焦不触发页面缩放（字号 ≥16px）
- [ ] iPhone 刘海/底部横条不遮挡内容（`safe-area-inset-*`）
- [ ] 模型下拉、用量浮层、工作区浮层不横向溢出
- [ ] 桌面端（>1024）逐屏对照，无任何视觉变化

## 8. 已知边界（不在本次范围）

- 横屏手机未做专门优化
- 平板 640–1024 沿用桌面并排布局（侧边栏默认展开）
- 桌面端 Sandbox 分栏拖拽（`beginResize`）在移动端不启用
