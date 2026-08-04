# IMA→Obsidian launchd 后台 AX 死结修复设计（v2 · 方向③：先实测 A）

> 日期：2026-07-23（v2）
> 状态：设计稿，待用户 review → writing-plans
> 上游 PRD：`MEMORY/WORK/20260722-213540_p01-launchd-nav-deadlock-plan/PRD.md`
> v1 审查：15 条发现（见 §9 对照）；核心证伪「B 触发依赖失效的同源 osascript Automation，A+B 不独立」

## 0. v2 修订说明

- v1 为 A+B 叠加。审查 #2 证明 B 回退的 `osascript 'tell Terminal do script'` 与 A 失效同源
  （均归因到 launchd 后台发起方的 Automation 权限），A 破产时 B 触发不了；#1/#4 进一步证明 B 会
  三重静默。→ B 在当前形态不成立。
- 用户决策（2026-07-23）：**方向③**——先搭 A 最小版实测，用数据回答「A 是否单独恢复 AX」，再决定要不要 B。
- 本 spec 只覆盖**阶段 1**（A 最小版 + 独立告警 + 实测决策门）。阶段 2 由实测数据触发。

## 1. 背景与问题

launchd `com.ima2obsidian.update`（每日 11:50）连续多日 100% 失效，DB 停在 100 篇。
证据：`log.1` 导航失败 189 次 / AX 0 元素 623 次；`error.log` `activate_ima()` osascript 5s 超时；
`a93f214`(7/15) 后仍 100% 失败；交互手动跑可用 → 故障仅限 launchd 后台。

## 2. 根因闭环（核心判断 = 待实测假设）

```
launchd 后台 osascript 'activate' 失效（Automation 归因到 updater，后台上下文不生效，5s 超时）
   → IMA 未被激活到前台
   → cua-driver 读 IMA 窗口 AX = 空壳（AX 0 元素，间接症状）
   → navigate_to_kb 全失败 → extractor 0 产出
```

**关键假设**：「AX 0 元素」是 IMA 非前台的**间接症状**，不是 cua-driver 的 Accessibility 问题
（手动跑可读 → Accessibility 已授权，归因到 `~/.local/bin/cua-driver`，对所有启动方式生效）。
**此假设是整个方案的前提，方向③即实测验证它**（§3.4）。

## 3. 方案：A 最小版 + 独立告警 + 实测决策门

### 3.1 A 最小版

| 组件 | 改动 | 解决审查 |
|------|------|------|
| `Ima2ObsidianUpdater.app` | 新增，`LSUIElement=true` 后台 agent | — |
| bundle ad-hoc 签名 | `codesign -s -`，稳定 code signing identity | #7 |
| `com.ima2obsidian.update.plist` | `ProgramArguments` 改为 exec `<bundle>/Contents/MacOS/<stub>`，**不用 `open -a`**（保退出码透传） | #6 |
| stub | **手写 shell stub 直接 `exec python`**（退出码天然透传），**不用 osacompile**（其 `do shell script` 把 exit1 转 AppleScript error，不显式转发则失真） | #6 |
| `activate_ima` | 下沉到 `ima_common.py`，增量+提取器共用一份 | #15 |
| daemon | `start_daemon`/`ensure_daemon` 下沉到 `ima_common.py`；**为避免循环依赖，`log()` 一并下沉到 ima_common**（当前 `start_daemon` 调 `log()` 而 ima_common 无 `log`，原样下沉会 NameError） | #5 |

### 3.2 `ax_context_ok` 轻量探测（不复用 `wait_for_ax_ready`）

- **自写**：短超时轮询（3×2s）取 `get_window_state`，判 `element_count`，**对齐 extractor L671 的 `>=100`**（它才是真正提取前的门），而非 AXStaticText≥5。 → #8 #9
- **不复用 `wait_for_ax_ready`**：它超时返回 False 时日志写「降级继续」、且必然阻塞满 30s，与「探测后回退/fail」语义相反。 → #8
- **失败行为（无 B 回退）**：写失败状态 + 独立告警 + `exit1`。
- **探测覆盖**：实测期先用单点（KB 循环内本就有 `navigate_to_kb` 的多次 activate+重试兜底）；若实测发现 KB 间 IMA 失焦漂移，再加密为「每 KB 前探测」。 → #10

### 3.3 独立告警通道（不依赖 osascript / 通知中心 / 勿扰）

- **主：写状态文件** `~/.ima2obsidian/last_run.json`（`timestamp, status, ax_ok, new, saved, failed, error`）。
- **辅：失败时 `say`**（`/usr/bin/say` 语音，独立二进制，绕过通知中心与勿扰/聚焦模式）。 → #4 #14
- **保留**：launchd `exit1` 告警（A 路径退出码透传）。
- terminal-notifier 勿扰下静默（#14），仅作可选补充，不作唯一通道。

### 3.4 实测决策门（`launchctl kickstart` 跑 1-2 天）

| 观测 | 结论 | 阶段 2 动作 |
|------|------|------|
| 新增 N>0 + 无「AX 0 元素」 | A 单独有效，§2 假设成立 | B 不要，spec 定稿，收尾 |
| 仍「AX 0 元素」/ 0 产出 | A 无效，瓶颈是 cua-driver 的 Accessibility | 给 cua-driver 二进制单独授权/签名（非 osascript B） |

## 4. A 基石加固（解决 #7）

- bundle 必须 ad-hoc 签名（`codesign -s -`，统一 identity），TCC 按 code signing identity 归因。
- **首跑授权双重验证**：① 双击 `.app` 一次触发 TCC 授权对话框（控制 `ima.copilot` / `System Events`）；
  ② `launchctl kickstart` 实测验证 launchd exec 身份下授权同样生效——双击走 LaunchServices、
  launchd exec 绕过 LaunchServices，二者 TCC 归因身份可能不同，**kickstart 才是真实上下文**。

## 5. 部署流程

1. 建 bundle + `codesign -s -`。
2. 双击 `.app` 一次 → 授权 TCC。
3. `launchctl unload` → 改 plist → `launchctl load`。
4. `launchctl kickstart gui/$UID/com.ima2obsidian.update` → 进入实测期。

## 6. 错误处理（无 B 回退）

| 场景 | 行为 |
|------|------|
| `ax_context_ok` 失败（activate 仍无效） | 状态文件 `status=failed_ax` + `say` + `exit1` |
| cua-driver 拉不起 | `ensure_daemon` False → 状态文件 + `say` + `exit1` |
| IMA 窗口 off-screen | 沿用现有 `restart_ima()` |
| 首跑未授权 | kickstart 时 activate 失败 → `ax_context_ok` 失败 → 告警提示「请双击 .app 授权」 |

`ensure_daemon` 改进（#11）：握手超时 `10s→5s`；`Popen` 的 stderr 不再 `DEVNULL`，写日志（保留
cua-driver 启动错误可见，避免独立调 extractor 时「静默 Popen 失败 → 阻塞 10s 才报超时」）。

## 7. 测试口径

- **回归**：现有 pytest 全过。
- **新增单测（mock，不依赖真 GUI）**：
  - `ax_context_ok` 轻量探测（element_count 阈值、短超时、不阻塞 30s）。
  - `activate_ima` / `ensure_daemon` 下沉后增量与提取器行为一致。
  - `ensure_daemon` 快速失败路径（握手超时）。
  - `last_run.json` 状态写入格式。
- **端到端（手动，实测期）**：kickstart + log 判定 + 决策门。

## 8. 实测期端到端验证

1. `launchctl kickstart gui/$UID/com.ima2obsidian.update`。
2. 看 `incremental_update.log`：新增 N、AX 元素数、有无「AX 0 元素」。
3. 看 `~/.ima2obsidian/last_run.json` 的 `status` / `ax_ok`。
4. 按 §3.4 决策门选阶段 2。

## 9. 审查 15 条处置对照

| # | v1 问题 | v2 处置 |
|---|------|------|
| 1 | 文件锁竞态 | B 暂不建，无交接，消失 |
| 2 | A+B 不独立 | B 暂不建，消失；方向③实测验证 A |
| 3 | 防递归变量名拼写 | B 暂不建，无该变量，消失 |
| 4 | B 路径双重静默 | B 暂不建；告警改 `say` + 状态文件（§3.3） |
| 5 | daemon 下沉循环依赖 | `log` 随之下沉到 ima_common（§3.1） |
| 6 | 退出码透传 | 手写 shell stub 直接 exec python（§3.1） |
| 7 | TCC 归因基石 | ad-hoc 签名 + kickstart 验归因（§4） |
| 8 | `wait_for_ax_ready` 复用偏差 | 自写轻量探测，不复用它（§3.2） |
| 9 | 阈值不一致 | 对齐 extractor `element_count>=100`（§3.2） |
| 10 | 单点探测覆盖不足 | 实测期单点 + KB 内现有重试；漂移则加密（§3.2） |
| 11 | `ensure_daemon` 阻塞吞错 | 握手 5s + 保留 stderr（§6） |
| 12 | B 回退命令占位 | B 暂不建，消失 |
| 13 | B 抢焦点侵入 | B 暂不建，消失 |
| 14 | notify 勿扰静默 | 告警改 `say` + 状态文件（§3.3） |
| 15 | `activate_ima` 双副本 | 下沉到 ima_common 统一（§3.1） |

## 10. 关键文件

| 文件 | 改动 |
|------|------|
| `Ima2ObsidianUpdater.app/`（新增） | bundle + `LSUIElement` + ad-hoc 签名 + shell stub |
| `com.ima2obsidian.update.plist` | `ProgramArguments` → exec bundle stub |
| `ima_common.py` | 下沉 `log` / `activate_ima` / `start_daemon` / `ensure_daemon`；新增 `ax_context_ok`、写 `last_run.json`、`say` 告警 |
| `ima_incremental_update.py` | import 公共函数；删本地 `activate_ima`/`start_daemon`/`ensure_daemon`；main 加 `ax_context_ok` 探测 + 告警 |
| `ima_ax_extractor.py` | import 公共 `activate_ima`/`ensure_daemon`（删两处本地副本） |
| `tests/` | 新增 4 组单测 |
