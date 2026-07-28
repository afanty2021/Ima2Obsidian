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
| `WAIT_PAGE_LOAD` | 6.0s | 页面加载等待时间 |
| `WAIT_CLIP_SAVE` | 4.0s | Web Clipper 保存等待时间 |
| `WAIT_FILE_APPEAR` | 2.0s | 文件出现等待时间 |
| `DEFAULT_LIMIT` | 1300 | 每次最多处理文章数 |

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

## Web Clipper 自动化依赖

`save_one_article` 通过模拟 Option+Shift+O 快捷键触发 Chrome 的 Obsidian Web Clipper 扩展。该机制有 **双层 TCC 要求**（缺一不可）：

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
- 增加 `WAIT_CLIP_SAVE` 时间

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
