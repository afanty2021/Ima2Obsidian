# 修复计划：IMA→Obsidian 三个独立问题

## Context

2026-07-14 排查"上午修复是否生效 + 保存全失败"，系统化调试锁定 **saver launchd 进程无 `~/Documents` TCC 读权限**为保存失败根因（已加 fail-loud 检测 + 回收 26 篇滞留）。随后 kickstart 端到端验证时 fail-loud 成功触发，连带暴露**三个互相独立**的问题，本计划逐个修复：

- **C**：saver launchd 无 Documents 权限 → 保存全失败（用户已选**方案1：搬 Vault**）
- **A**：`cmd_w_close(None)` 退化 → IMA 标签堆积（用户最初看到的）
- **B**：提取器 URL 提取失败 → 新文章进不了库（冷启动 System Events 限制，探索性）

三者独立，C 最直接解决"保存失败"，先做。

---

## C — Vault 移出 ~/Documents（根治 TCC）

**根因**：`~/Documents` 受 macOS TCC 保护，launchd 后台 python 无读权限。给 `/usr/bin/python3` 加 FDA 对终端生效但对 launchd 无效（stub 透传到 Xcode python3.9，TCC 归因不一致）。家目录其他子目录不受 TCC 保护。

**改动**：仅 `ima_obsidian_saver.py:48` 一行（`CLIPPINGS_DIR` 派生、`reclaim_clippings.py` 经 import 自动跟随；DB 在项目目录不受影响）：
```python
VAULT_DIR = Path(os.path.expanduser("~/ObsidianVault"))
```

**执行步骤**（迁移期避免双写）：
1. `launchctl unload ~/Library/LaunchAgents/com.ima2obsidian.update.plist` 停定时任务
2. `mv "~/Documents/Obsidian Vault" ~/ObsidianVault`（同盘秒级改名，2340 篇 + .obsidian 配置整体搬）
3. 改 `ima_obsidian_saver.py:48`
4. Obsidian：「Open folder as vault」→ `~/ObsidianVault`（`.obsidian/` 随目录搬走，插件/设置保留）
5. 验证 Web Clipper 扩展仍连到 vault（配置在 vault 内 `.obsidian/plugins/`，跟随搬走；只需确认浏览器扩展侧"默认 vault"偏好）
6. `launchctl load ...` 重启定时任务

---

## A — cmd_w_close(None) 标签堆积

**根因**（agent 已精确定位）：`ima_ax_extractor.py` `cmd_w_close` 的内嵌 `still_open()` 在 `article_url=None` 时（L337-338）短路 `return False` → 跳过校验+激活重试，只发一次后台 Cmd+W。而文章由 cua-driver **后台**点击打开（焦点不在 IMA）→ Cmd+W 失效 → 标签堆积。URL 提取失败（article_url=None）的标签全漏关。

**修复**：`extract_url_ax()` 本身就是"还有任意文章标签吗"的探测器（扫描所有窗口 AXDocument，任一 http 即返回）。把 None 分支改成复用它：
```python
# still_open() 内，L336-338
def still_open() -> bool:
    time.sleep(WAIT_AFTER_CLOSE)
    if article_url:
        return extract_url_ax() == article_url      # 已知 URL：精确比对
    return extract_url_ax() is not None              # 未知 URL：仍有任意文章标签 → 未关
```
None 路径自动获得"校验 + 激活 IMA 重试"全套兜底，与有 URL 路径行为一致。

**改动**：`ima_ax_extractor.py` `cmd_w_close` 的 `still_open`（~3 行）。

**注**：判据 `extract_url_ax() is not None` 在 B 修好后更可靠；但 A 改进**独立有效**（比现状"完全不校验、一定堆积"强），可先于 B 落地。

---

## B — 提取器 URL 提取失败（探索性，需实测）

**根因**：`extract_url_ax` 用 System Events 读窗口 `AXDocument`（c2b67f7 已证伪 cua-driver 替代——地址栏 AXTextField 只显示域名）。冷启动/后台时 System Events 读 AXDocument 不可靠（日志：窗口标题空 + 每篇"未提取到 URL"）。`wait_for_ax_ready` 判据是 AXStaticText 数（1023 就绪），但 AXDocument（System Events）就绪更晚。

**约束**：`extract_url_ax` 必须用 System Events（cua-driver tree_markdown 无 AXDocument，只有 AXWindow/AXStaticText 等树结构）。

**修复方向**（实测验证有效性，非确定方案）：
1. 提取每篇文章**前 `activate_ima()`** 确保 IMA 前台（System Events 读前台窗口 AXDocument 更可靠）
2. `extract_url_ax` 重试循环穿插 `activate_ima`（当前只 sleep 重试）
3. `wait_for_ax_ready` 达标后、提取前，增加"AXDocument 可读"探测门控

**优先级**：中。影响新文章入库；C+A 先做（清晰且解决用户直接关切），B 跟进且需运行时实测确认哪个方向有效。

---

## 顺序与依赖

1. **C（搬 Vault）** — 独立，解决保存失败，**先做**
2. **A（标签关闭）** — 小改动，C 后做；B 修好后判据更准，但独立有效
3. **B（URL 提取）** — 探索性，C+A 后跟进，实测驱动

## 验证

- **C**：`launchctl kickstart` 跑定时任务 → `incremental_update.log` 出现 `保存到 Obsidian: N 篇`（N>0）、**无** fail-loud"无权限读取"
- **A**：`/usr/bin/python3 ima_ax_extractor.py --src <KB>` 跑提取 → 观察 IMA 标签是否关闭（不堆积）、`cmd_w_close` 日志出现校验/重试
- **B**：跑提取器 → `未提取到 URL` 比例下降/消失
- **回归**：`/opt/homebrew/bin/python3 -m pytest tests/`（当前 94 项全过）

## 关键文件

| 文件 | 位置 | 问题 |
|------|------|------|
| `ima_obsidian_saver.py` | L48 `VAULT_DIR` | C |
| `ima_ax_extractor.py` | `cmd_w_close` L317-357（`still_open` L336-340） | A |
| `ima_ax_extractor.py` | `extract_url_ax` L239-276 | B |
| `ima_incremental_update.py` | `wait_for_ax_ready` | B |
