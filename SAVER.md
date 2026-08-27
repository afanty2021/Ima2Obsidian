# 保存器详细文档 — ima_obsidian_saver.py

## 功能说明

从数据库读取文章 URL，通过浏览器 + Obsidian Web Clipper 保存到 Obsidian Vault。

---

## 工作原理

### 1. 日期提取
从微信文章页面 HTML 提取发布日期，支持多种格式：

```javascript
// 方法1: create_time: JsDecode('YYYY-MM-DD HH:MM')
// 方法2: ori_create_time / create_timestamp (Unix 时间戳)
// 方法3: var createTime = 'YYYY-MM-DD HH:MM'
// 方法4: publish_time (URL 编码的 JSON 中)
```

### 2. 保存流程
```
提取日期 → 打开文章 → 验证页检测 → 删除页检测 → 触发 Web Clipper → 等待保存 → 查找文件 → 重命名 → 关闭标签
```

- **验证页**：「当前环境异常」风控页，点确认后继续 clip（可恢复，保持未保存下次重试）
- **删除页**：「文章已被发布者删除」永久不可恢复，标记 `status='deleted'` 短路跳过（不 clip，不计失败）

### 3. 文件重命名
- 格式: `YYMMDD title.md`
- 自动清理非法字符
- 可选移动到指定文件夹

---

## 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `WAIT_PAGE_LOAD_MAX` | 6.0s | 页面加载自适应等待上限（readyState 轮询，微信页通常 2~3s 就绪） |
| `WAIT_CLIP_SAVE` | 1.0s | 落盘首轮轮询前起步间隔；半成品由 `_file_write_settled` 双采样防护 |
| `WAIT_FILE_APPEAR` | 2.0s | 文件出现等待时间 |
| `DEFAULT_LIMIT` | 1300 | 每次最多处理文章数 |

### 性能调优（2026-08-27）

保存循环篇均 ~21s → 目标 ~12s 的五项调整：
1. **跳过日期预取 HTTP**：requests 抓微信精简页对日期正则必失配（每篇白付一次请求），正式跑直接用页面快照的 `publish_time` 与文件内容日期覆盖
2. **readyState 自适应加载**：`wait_page_ready` 轮询 `document.readyState`，AppleScript 失败/慢渲染退化为睡满上限
3. **快照单次往返**：验证页检测、删除判定、发布日期共用一次 AppleScript 调用（原三次）
4. **早轮询 + 稳定性检查**：`find_and_rename_in_vault(..., require_stable=True)` 用双采样 size+mtime 防半成品
5. **AX 就绪预算 30s → 12s**：生产日志显示旧预算天天超时而降级路径正常，只省等不改变行为

---

## 命令行参数

```bash
--limit <数量>         # 每次处理文章数
--dry-run             # 预览模式
--browser <浏览器>     # chrome/edge/safari
--mode <模式>          # quick/clipper
--des <文件夹>         # 目标文件夹名称
```

---

## 环境变量

| 变量 | 默认 | 语义 |
|------|------|------|
| `IMA_DEBUG_BODY_LEN` | 未设置等同 `"1"`（**默认开启**） | 每篇文章处理时向 stdout 打印 `[debug] len(body)=N` 日志 |

- **用途**：spec §5 风险表的渐进验证缓解措施——确认 `<60 字` 阈值对真实屏蔽/违规页是否有效（防止 `_deleted_reason` 漏检导致 bug 静默存留）。
- **opt-out**：设 `IMA_DEBUG_BODY_LEN=0` 关闭（仅识别字符串 `"0"`，其他任何值视为开启）。
- **退场**：首篇屏蔽/违规 URL 命中并完成实证后，可移除该 print 与门控（参见 `ima_obsidian_saver.py` 中对应 `TODO(渐进验证)` 注释）。

---

## 环境预检与漂移预警（2026-08-27 新增）

背景：2026-08-12 换 Chrome 登录账号后自动化标签页落到新 Profile（扩展不在那里），加上中文输入法拦截 ⌥ 组合键，保存器 0 落盘且 rc=0 无声，**静默失败 15 天**。为此引入三道防线：

### 1. 启动预检（saver `preflight_clipper_env`，fail-closed）

实跑前显式断言环境前提，任何硬性缺失直接终止并给出修复提示：

| 检查项 | 判定 | 修复提示 |
|---|---|---|
| 激活 Profile 装有 Web Clipper | 读 `Local State` → `Secure Preferences` | 在**该 Profile** 重装扩展（扩展按 Profile 隔离） |
| 扩展已启用 | `state≠0` 且无 `disable_reasons` | chrome://extensions 开启 |
| 快捷键已绑定且键位一致 | clipper 模式查 `_execute_action`，quick 模式查 `quick_clip`，且与 saver 实际发送的键位（Command+Shift+O / Alt+Shift+O，由常量推导）比对 | chrome://extensions/shortcuts 改回键位，或同步修改 saver 常量 |
| 输入法兼容 | quick 模式 + 中文输入法 → **fail-closed**（⌥+字母 被输入法层拦截，按键永远到不了扩展） | 改用 `--mode clipper`（⌘⇧O 不受影响） |

`--skip-preflight` 可强制跳过；dry-run 只提示不阻断。Local State 读不到时仅警告放行。

### 2. 弹窗回执 + AX 优先（clipper 模式 `trigger_clipper_with_receipt`）

旧流程是"热键 fire-and-forget + 25s 文件轮询"，命令层没响应也要空等。新流程：

1. 触发 ⇧⌘O 前记 Chrome 窗口基线，触发后轮询 `list_windows`（窗口层，不受 Chromium 渲染器 AX 按需开启影响）等待**新弹窗窗口**出现——命令层回执；
2. 弹窗出现后优先 **AX 点击** 'Add to Obsidian'（cua-driver element_index，语义动作）；
3. AX 树不含按钮（Chromium 常态：渲染器 AX 超时关闭，常只回菜单栏）→ 回退回车键（弹窗默认按钮即 Add，2026-08-27 实测可落盘）；
4. 弹窗未出现 → **快速失败**（跳过 25s 轮询），签名 `popup_missing`。

### 3. 同签名熔断（`ConsecutiveFailureBreaker`，阈值 3）

同一失败签名连续 3 次说明是系统性环境故障而非单篇问题（扩展被禁用/输入法拦截/击键被 TCC 丢弃），熔断剩余批次并按签名打印排查指引——2026-08 曾一天 94 篇 × ~90s 全失败空转 1.5 小时。签名：`popup_missing`（命令层未响应）/ `file_not_found`（触发成功但未落盘）/ `exception`。成功或 deleted 重置计数。

### 4. 环境漂移快照（incremental 主流程，`ima_common.environment_snapshot`）

每次增量更新把 `{激活 Profile, 扩展版本/启用, 输入法}` 写入 `environment_snapshot.json`（已 gitignore），与上一份对比，漂移当天在日志告警：

```
⚠️  环境漂移（自动化前提可能失效，今日保存异常请优先排查此项）:
     chrome_profile_dir: 'Default' → 'Profile 1'
```

2026-08 的两个根因（换 Profile、输入法拦截）有此快照均可在发生当天暴露。

---

## Web Clipper 自动化依赖

`save_one_article` 通过模拟快捷键触发 Chrome 的 Obsidian Web Clipper 扩展（quick 模式 Option+Shift+O / clipper 模式 Cmd+Shift+O，见上节）。该机制有 **双层 TCC 要求**（缺一不可）：

### 1. cliclick 二进制（PR #7）

`send_keystroke` 用 `cliclick`（[CGEventPost](https://developer.apple.com/documentation/coregraphics/1455361-cgeventpost)）替代 `osascript`（Apple Event），绕过 macOS TCC AppleEvents 限制（osascript 报错 1002「"osascript"不允许发送按键」）。

**安装**：

```bash
brew install cliclick
```

`saver` 启动时通过 `_find_cliclick()` 检测常见路径（`/opt/homebrew/bin/cliclick` → `/usr/local/bin/cliclick` → `/usr/bin/cliclick` → `shutil.which` fallback），不依赖 launchd PATH 含 Homebrew（launchd 启动的进程默认 PATH 仅 `/usr/bin:/bin:/usr/sbin:/sbin`）。

### 2. cliclick Accessibility 授权（必装步骤）

⚠️ **launchd 启动的 cliclick 不继承用户 GUI session 的 Accessibility 授权**——CGEventPost 事件会被 TCC 默默 drop（`cliclick` 命令 rc=0 但事件无效，Web Clipper 不响应）。这与在 iTerm/Terminal 里跑 `cliclick` 不同（交互式终端已有了 Accessibility，会让人误以为 launchd 也能用）。

**必须手动添加 cliclick 到辅助功能**：

1. 打开 **系统设置 → 隐私与安全性 → 辅助功能**
2. 解锁左下角锁图标（如需要）
3. 点 **+** 号
4. 按 **Cmd+Shift+G** 输入路径：`/opt/homebrew/bin/cliclick`（Intel Mac 用 `/usr/local/bin/cliclick`）
5. 选中 cliclick → 添加
6. **确保开关打开**（蓝色）

**验证**：`launchctl start com.ima2obsidian.update` 后看 `incremental_update.log` 是否出现「移动(新文件)」+「✅ 完成」。

**已实证**（2026-07-28 18:30 launchctl start 跑）：
- 修复前（cliclick 未在辅助功能里）：0 成功 / 18 失败
- 用户手动添加 cliclick 到辅助功能后：38+ 成功 / 2 失败（独立原因）

### 故障排查

| 现象 | 根因 | 修复 |
|---|---|---|
| `⚠️ send_keystroke 失败：cliclick 未安装` | `_CLICLICK_PATH` 解析失败（None） | `brew install cliclick` |
| `osascript 报错 1002「不允许发送按键」` | 旧版本（PR #7 之前） | 升级到 PR #7+（改用 cliclick） |
| quick_clip 触发 + Chrome 在前台 + 但 Web Clipper 不响应 | **cliclick 缺 Accessibility 授权** | 上方「cliclick Accessibility 授权」步骤添加 |
| `[诊断] quick_clip 触发时前台应用='ima.copilot'` | `activate_browser` 时机问题（部分文章） | 独立问题，不阻塞主流程 |

---

## 浏览器快捷键

| 浏览器 | Quick Clip | Clipper |
|--------|-----------|---------|
| Chrome | Option+Shift+O | Cmd+Shift+O |
| Edge | Option+Shift+O | Cmd+Shift+O |
| Safari | Option+Shift+O | Cmd+Shift+O |

---

## 依赖条件

1. **浏览器** 已安装 Obsidian Web Clipper 扩展
2. **Obsidian** 应用运行并打开目标 Vault
3. **Web Clipper** 已连接到 Obsidian

---

## 文件查找策略

### 第一步：精确匹配
文件名与标题匹配的最近创建文件

### 第二步：新文件检测
不存在于保存前快照中的新文件

---

## 常见问题

**未找到保存的文件**
- 检查 Obsidian 是否运行
- 确认 Web Clipper 已连接
- 增大 `WAIT_CLIP_TOTAL`（落盘轮询总预算，默认 25s）

**文件名过长**
- 自动截断到 100 字符
- 非法字符替换为 `-`

**目标文件夹不存在**
- 自动创建

**文章已被发布者删除**
- saver 检测到删除页（`该内容已被发布者删除` / `此内容因违规已删除`）自动标记 `status='deleted'`
- 永久跳过，不再重试，不计失败（避免反复打开已删文章 + 触发上游告警）
- 统计行「累计已删除(永久跳过)」可见数量；日志经 `incremental_update.log` 落盘
- 如需重试：`UPDATE articles SET status='success' WHERE id=?`
