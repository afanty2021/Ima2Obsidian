# Obsidian AppNap 冻结致 Web Clipper 写盘延迟 — 修复设计

> 创建：2026-08-03（v4：策略 A 实测有效，方向 1 从 per-article activate 简化为系统级禁用）
> 故障日期：2026-08-02
> 涉及组件：`ima_obsidian_saver.py`、`reclaim_clippings.py`、`ima_incremental_update.py`

---

## 0. 修订摘要

### v6（相对 v5）：吸收 v5 code-review 5 条（无严重，全部采纳）

v5 review 确认「v5 已达到可实施状态」，5 条均为中低危文档修正：

1. **附录 B caffeinate 事实错误（#1）**：`-w <pid>`（等 PID 退出，对 AppNap
   无效）→ `-u -t <N>`（declare user active，与 wake_screen 一致）。
2. **ensure_appnap_disabled 日志统一（#2）**：False 三态（刚写入/失败/超时）
   的日志移入函数内部，调用点 2 不重复打印——消除"一个说失败一个说刚写入"矛盾。
3. **JSON 输出补可运行版本（#3）**：CLIPPINGS_DIR 缺失路径的 f-string 嵌套 +
   占位符 → 标准 dict + json.dumps。
4. **位置说明修正（#4）**：改动(a)「reclaim_all() 内」→「main() 内 line 104
   之后、line 111 之前」（v5 不重构，保留 main() 结构）。
5. **Phase 1 raise 局限（#5）**：补文档说明——reclaim BaseException handler
   raise 后 JSON 输出达不到，saver 解析空字典；launchd 无 Ctrl+C 极不可能触发。

### v5（相对 v4）：吸收 v4 code-review 12 条

v4 review 发现 3 个架构问题 + 遗漏，本版核心修正：

1. **循环引用（#1，方案 A）**：v4 设计 saver `from reclaim_clippings import
   reclaim_all` 会循环（reclaim_clippings 已从 saver 导入）。改为 **subprocess
   调用 + JSON 输出**——reclaim_clippings.py 末尾打印 `RECLAIM_RESULT: {json}`，
   saver subprocess 解析。无循环引用，reclaim_clippings.py 不重构。
2. **ensure_appnap_disabled 漏调用（#2）**：`ensure_obsidian_ready` 在 Obsidian
   已运行时短路，首次部署最常见场景下 ensure 永不调。改为两个入口都调。
3. **实现 bug 修复**：defaults write 加 returncode 检查（#3）、subprocess 加
   try/except（#6）、补具名导入（#5）、md5 用 normalize_stem（#4）、aborted_reason
   初始化（#9）、CLI sys.exit(1)（#8）、日志按场景措辞（#7）。

### v4（相对 v3）：策略 A 实测有效，方向 1 大幅简化

**2026-08-03 实测**（spec §6.0）：设置 `defaults write md.obsidian
NSAppSleepDisabled -bool YES` + 重启 Obsidian + 后台最小化 5 分钟 + 触发
quick_clip → **触发后 2s 落盘**。对比 8/2 故障（同场景未设置 → 17 分钟延迟）。

**结论**：策略 A（一次性系统级禁用 AppNap）有效。方向 1 从 v3 的"per-article
bring_to_front"简化为"确保 NSAppSleepDisabled 已设置"——**零运行时开销**，
saver 改动仅启动时检查 defaults（idempotent），§4.1 的 bring_to_front /
caffeinate / WAIT_OBSIDIAN_WAKEUP 等全部不落地（移至附录 B 备选）。

### v3（相对 v2）：吸收 v2 code-review 7 条 + 策略评估

- md5 检测致命 bug 修正（改用 `f.stem` 而非 frontmatter title）
- `run_cua` 导入 + timeout、`sys.exit` 迁移、`rollback_failures` 入 dict
- 新增 §4.0 策略评估（系统级 / caffeinate / per-article），用户指示先实测 A

### v2（相对 v1）：吸收 v1 code-review 14 条

- 方向 1 osascript → bring_to_front（Obsidian 是 Electron）
- 方向 4 砍新写，复用 reclaim_clippings.py

---

## 1. 故障根因（systematic-debugging Phase 1 结论）

### 现象

2026-08-02 launchd 11:50 触发增量更新，saver 11:58~12:36 跑 23 篇文章，
**0 成功 / 23 失败**，全部「⚠️ 未找到保存的文件」。三批 KB 全军覆没
（AI 12、Invest 5、皮皮鲁的知识库 6）。

### 证据链

| 时刻 | 事件 | 证据 |
|------|------|------|
| 11:50:04 | launchd 触发 incremental_update | incremental_update.log L18094 |
| 11:58:39 | `open -a Obsidian` 启动 Obsidian（**不 activate 到前台**） | ima_incremental_update.py:609 |
| 11:58:45 ~ 12:36:50 | saver 跑 23 篇，每篇 `[诊断] 前台应用='Google Chrome'`，轮询 25s 全部「未找到文件」 | incremental_update.log L18202-18880 |
| **11:50 ~ 12:54** | **无任何 `UserIsActive` 事件**（用户离开电脑 64 分钟） | `pmset -g log` |
| **12:54:17** | UURemoteServer 触发 `UserIsActive`（用户活动恢复） | `pmset -g log` |
| **12:54:22** | 23 个文件**同秒批量落盘**到 `Clippings/`（恢复后 5 秒） | `stat -f "%Sm"` |

### 已排除的环节

- ✅ `obsidian://` 协议已注册（`lsregister -dump` 确认）
- ✅ Vault 配置正确（`/Users/berton/Obsidian Vault`, `open=True`）
- ✅ Web Clipper 扩展装着（v1.7.1，ID `cnjifjpddelmedmihgijeibhnjfabmlf`）
- ✅ Chrome 确实在前台（日志诊断行）
- ✅ Obsidian 进程在运行
- ✅ 落盘的 23 个文件**内容全部有效**（真实文章正文，非错误页/模板）

### 链路断点定位

```
saver → cliclick → Chrome 收键 → Web Clipper 扩展 → obsidian:// → Obsidian 写盘
```

前 4 段都通。断在末段：**Obsidian 进程虽运行，但因用户长时间无活动 + 窗口不在
前台，被 macOS AppNap 冻结**，无法及时处理 `obsidian://` 写盘请求。Web Clipper
端积压 23 个请求，用户活动恢复后 5 秒内批量 flush。

### 批量 flush 错乱（关键副发现）

23 个滞留文件 **md5 完全相同**（`6494d071a27035e8c94369a6134d735e`）——23 个
不同文件名（不同文章标题），内容却都是同一篇文章（"要拿英语考试拿高分"，
saver 跑的最后一篇）。原因：Web Clipper 批量 flush 积压请求时，抓取的是当时
Chrome 活跃标签的内容，23 个请求都用同一份内容生成文件，文件名仍按各请求
记录的标题命名。

**影响**：23 个文件是垃圾副本，不能被认领；8/2 的 23 篇有效内容根本没被
Web Clipper 抓到，修复后必须重跑 saver。方向 4 的 md5 去重防御专门识别此模式。

---

## 2. 设计层面的根因

`launch_obsidian()` 用 `open -a Obsidian`，**只启动不 activate**；saver 全程只
activate browser，**从不抑制 Obsidian 的 AppNap**。用户一旦长时间离开，Obsidian
沦为后台非活跃应用，写盘链路被 macOS AppNap 挂起。

> Obsidian 是 Electron 应用（bundle 内含 `Electron Framework.framework`），
> 与 IMA 同架构，受 AppNap 影响显著。

---

## 3. 修复方向

用户选定 **方向 1 + 方向 4（B）**：

- **方向 1**：禁用 Obsidian 的 AppNap（NSAppSleepDisabled），解冻写盘链路（**主力修复，实测有效**）
- **方向 4**：下次 saver 跑开头扫 `Clippings/` 认领有效滞留（**兜底**，收窄范围）

---

## 4. 详细设计

### 4.1 方向 1：禁用 Obsidian AppNap（NSAppSleepDisabled）

#### 实测证据（2026-08-03）

| 场景 | NSAppSleepDisabled | 后台时长 | quick_clip 后落盘延迟 |
|------|-------------------|---------|----------------------|
| 8/2 故障 | 未设置 | 64 分钟 | **17 分钟**（用户恢复后 5s 批量 flush） |
| 8/3 实测 | =1（已设置） | 5 分钟 + 最小化 | **2 秒** |

差距悬殊，策略 A 有效。日志见 `/tmp/obsidian_appnap_test.log`。

#### 新增函数 `ensure_appnap_disabled()`

```python
def ensure_appnap_disabled() -> bool:
    """确保 Obsidian 的 AppNap 已禁用（NSAppSleepDisabled=1）。

    实测（2026-08-03）：设置后 Obsidian 后台 5 分钟 + 最小化，quick_clip 触发后
    2s 落盘（对比 8/2 未设置时 17 分钟延迟）。一次性系统级设置，零运行时开销。

    幂等：已设置时 no-op 返回 True；未设置时 defaults write 返回 False。

    Returns:
      True = defaults 已是 1（之前就设了）
      False = 本次刚写入 或 写入失败（调用方按场景决定是否需提示重启）
    """
    try:
        result = subprocess.run(
            ["defaults", "read", "md.obsidian", "NSAppSleepDisabled"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip() == "1":
            return True  # 已设置
        # 未设置：写入（review v4 #3：检查 returncode，不静默吞失败）
        write_result = subprocess.run(
            ["defaults", "write", "md.obsidian", "NSAppSleepDisabled", "-bool", "YES"],
            capture_output=True, text=True, timeout=5,
        )
        if write_result.returncode != 0:
            err = write_result.stderr.strip() or f"exit {write_result.returncode}"
            print(f"❌ defaults write NSAppSleepDisabled 失败: {err}", flush=True)
            return False
        # review v5 #2：刚写入成功的日志在函数内统一打印，调用方不重复
        print("⚠️ NSAppSleepDisabled 本次刚写入，当前 Obsidian 进程未带标志。", flush=True)
        print("   建议重启 Obsidian（quit + open）让本次 saver 也安全；否则靠 reclaim 兜底。", flush=True)
        return False
    except subprocess.TimeoutExpired:  # review v4 #6：cfprefsd 锁等导致超时
        print("⚠️ defaults read/write 超时（cfprefsd 锁？）", flush=True)
        return False
```

#### 调用点：两个入口都要调（review v4 #2 修正）

**定义位置**：`ima_common.py`（共享模块）。

**导入**（review v4 #5）：`ima_incremental_update.py:27-30` 的具名导入列表补
`ensure_appnap_disabled`：
```python
from ima_common import (
    CUA_DRIVER, IMA_APP_NAME, run_cua, is_daemon_running,
    get_ima_main_window, ensure_appnap_disabled,  # ← 新增
)
```

**调用点 1：`launch_obsidian()` 启动新进程前**——写入 defaults 后紧接着
`open -a Obsidian` 启动新进程，新进程自动读取已写入的 defaults，**本次 saver
即生效**，无需警告：

```python
def launch_obsidian(timeout: int = 30) -> bool:
    log("启动 Obsidian 应用...")
    ensure_appnap_disabled()  # 写入 defaults，紧接着 open 的新进程自动带标志
    subprocess.run(["open", "-a", "Obsidian"], capture_output=True, timeout=10)
    ...
```

**调用点 2：`ensure_obsidian_ready()` Obsidian 已运行时**（review v4 #2 修复）——
现有 line 624 `if is_obsidian_running(): return True` 短路会跳过 ensure。改为：

```python
def ensure_obsidian_ready() -> bool:
    """确保 Obsidian 已运行（未运行则自动启动），供保存器前置检查使用。"""
    if is_obsidian_running():
        # Obsidian 已运行也要确保 AppNap 禁用（review v4 #2：否则首次部署
        # Obsidian 已在跑时 ensure 永不调用，defaults 不写入，8/2 故障复现）。
        # review v5 #2：日志由 ensure_appnap_disabled 内部统一打印（区分
        # 已设置/刚写入/失败/超时），调用方不重复——避免"一个说失败一个说刚写入"矛盾
        ensure_appnap_disabled()
        return True
    return launch_obsidian()
```

**日志语义**（review v4 #7 修正）：调用点 1（launch_obsidian）不打印"需重启"
警告——新进程自动生效，本次安全；调用点 2（ensure_obsidian_ready，Obsidian
已运行）才在 `ensure_appnap_disabled()` 返回 False 时提示重启。

#### 已知局限

- **首次部署需一次手动重启**：用户首次安装本修复后，`ensure_appnap_disabled`
  写入 defaults 但当前 Obsidian 进程未带上标志。用户需手动重启 Obsidian 一次。
  之后所有新起的 Obsidian 进程都自带禁用，永久生效。
- **`NSAppSleepDisabled` key 的 macOS 版本兼容性**：实测在 Darwin 25.6.0
  （macOS 26）有效。若未来 macOS 版本不再尊重此 key，降级到附录 B 的备选方案。

#### 不再需要的设计（v3 的 bring_to_front 等全部砍掉）

v3 §4.1 的以下设计**全部不落地**（策略 A 有效，无需 per-article activate）：
- ~~`activate_obsidian()` 函数~~
- ~~`_get_obsidian_pid()` 函数~~
- ~~`WAIT_OBSIDIAN_WAKEUP` 常量~~
- ~~`run_cua` 导入 + `is_daemon_running` 短路~~
- ~~每篇 clip 前的 activate → sleep → 诊断 → activate_browser 时序改动~~
- ~~AppNap 重冻结风险（系统级禁用不存在单篇周期内重冻结问题）~~

这些移至**附录 B**作为"策略 A 未来失效时的备选"，不在本轮实施。

---

### 4.2 方向 4：复用 reclaim_clippings.py + md5 去重防御

#### 设计依据

v1 计划新写 reclaim，遗漏了已存在的 `reclaim_clippings.py`（267 行成熟实现）：
- **事务性 + 回滚**：Phase 1 per-row rename+UPDATE 失败回滚 + Phase 2 commit
  失败全量回滚，5 类失败分类（a-e）
- **dry-run 门控**：默认 dry-run，`--apply` 才实际执行
- **published_date 降级**：content_date → mtime_yymmd → COALESCE 保护 DB 已有值
- **KB 文件夹缺失**：`folder.is_dir()` + no_folder 跳过
- **冲突跳过**：`target.exists()` → conflict 分类，不覆盖
- **normalize_stem + sanitize 双索引标题匹配**

本 spec **不重新实现这些**，改为：(a) 加 md5 去重防御 + glob→rglob；(b) 重构
为可导入；(c) saver main() 调用。

#### 改动 (a)：md5 去重防御（用 f.stem 判定）

> **为什么用 `f.stem`（文件名）而非 frontmatter title**：8/2 的 23 个文件
> **md5 相同 = 内容字节相同 = frontmatter title 相同**。v2 读 frontmatter title
> → 23 个文件返回同一值 → 检测对设计目标场景**完全无效**。
>
> 批量 flush 错乱的特征是：**同 md5（内容相同）+ 不同文件名**（Web Clipper
> 按 23 次触发时各标签标题命名）。用 `f.stem` 判定，与现有 reclaim_clippings.py
  匹配逻辑一致。

```python
# reclaim_clippings.py main() 内 line 104（clip_files = ...）之后、
# line 111（for f in clip_files:）之前插入（review v5 #4：v5 不重构为
# reclaim_all()，保留 main() 结构）：

import hashlib
md5_groups = {}
for f in clip_files:
    try:
        digest = hashlib.md5(f.read_bytes()).hexdigest()
    except OSError:
        continue
    md5_groups.setdefault(digest, []).append(f)

# 批量错乱判定：md5 重复（>1）且组内文件名（normalize_stem 后）各不相同 → 8/2 故障模式
# review v4 #4：用 normalize_stem(f.stem) 而非原始 f.stem——合法重 clip 产生
# "Title" + "Title 1"（Web Clipper 加序号后缀），原始 stem 不同会误判为批量错乱；
# normalize_stem 剥尾部 " <数字>" 后缀，与 line 112 现有匹配逻辑一致
batch_corrupt_skipped = set()
corrupt_group_count = 0
for digest, files in md5_groups.items():
    if len(files) > 1:
        stems = {normalize_stem(f.stem) for f in files}
        if len(stems) > 1:  # 不同文章 + 同内容 → 批量 flush 错乱
            corrupt_group_count += 1
            batch_corrupt_skipped.update(files)

if batch_corrupt_skipped:
    print(f"md5 去重：跳过 {corrupt_group_count} 组批量错乱副本"
          f"（{len(batch_corrupt_skipped)} 个文件）")
```

`for f in clip_files:` 循环开头加 `if f in batch_corrupt_skipped: continue`。
汇总输出加 `print(f"批量错乱副本跳过: {len(batch_corrupt_skipped)}")`。

#### 改动 (b)：glob → rglob

`reclaim_clippings.py:104`：`CLIPPINGS_DIR.glob("*.md")` → `CLIPPINGS_DIR.rglob("*.md")`。
理由：Web Clipper 偶发畸形嵌套文件（bug `id=2913`），glob 只扫顶层漏深处。
与 `find_and_rename_in_vault` 对 Clippings 用 rglob 保持一致。

#### 改动 (c)：reclaim_clippings.py 加 JSON 结果输出（review v4 #1 方案 A）

> **为什么不重构为可导入 `reclaim_all()`**：reclaim_clippings.py:26 已
> `from ima_obsidian_saver import ...`，saver 若反向 import 会**循环引用**
> （saver 作 `__main__` 跑时，触发 reclaim_clippings → `from ima_obsidian_saver
> import` → saver.py 作为模块重新加载，模块级代码执行两次）。改为 **subprocess
> 调用 + JSON 输出**——saver 不 import reclaim_clippings，无循环。

reclaim_clippings.py **保留现有 `main()` 结构**（含 `sys.exit(1)` CLIPPINGS_DIR
缺失处理——review v4 #8：CLI 正常退出码语义），仅在 main() 末尾**追加一行
machine-readable JSON**：

```python
# reclaim_clippings.py main() 末尾，现有汇总打印之后追加：

import json
# review v4 #9：aborted_reason 显式初始化为 None
aborted_reason = None  # 正常完成；中途异常时赋值（现有 5 类失败分类已打印）

# CLIPPINGS_DIR 缺失的 sys.exit(1) 路径前，也先打印 JSON（让 saver subprocess
# 能解析到 aborted 原因，而非只看 returncode）
result = {
    "matched": len(matched), "moved": moved, "marked": marked,
    "no_match": len(no_match), "no_folder": len(no_folder),
    "conflict": len(conflict),
    "batch_corrupt_skipped": len(batch_corrupt_skipped),
    "rollback_failures": [(str(d), str(s), e) for d, s, e in rollback_failures],
    "aborted": aborted_reason,
}
print(f"RECLAIM_RESULT: {json.dumps(result, ensure_ascii=False)}")
```

CLIPPINGS_DIR 缺失路径（现有 line 73-75）改为先打印 JSON 再 sys.exit(1)
（review v5 #3：补可直接运行版本，避免 f-string 嵌套 + 占位符）：
```python
if not CLIPPINGS_DIR.exists():
    result = {
        "matched": 0, "moved": 0, "marked": 0,
        "no_match": 0, "no_folder": 0, "conflict": 0,
        "batch_corrupt_skipped": 0, "rollback_failures": [],
        "aborted": f"CLIPPINGS_DIR not found: {CLIPPINGS_DIR}",
    }
    print(f"RECLAIM_RESULT: {json.dumps(result, ensure_ascii=False)}")
    sys.exit(1)  # review v4 #8：CLI 退出码让运维/launchd 监控知道
```

#### 改动 (d)：saver main() subprocess 调用（无循环引用）

调用点：**`init_database()` 之后、`get_stats()` 之前**。

```python
import json as _json
import sys as _sys

# review v4 #1 方案 A：subprocess 调用避免循环引用
# review v4 #2 dry-run 门控：apply = not args.dry_run
cmd = [_sys.executable, str(Path(__file__).parent / "reclaim_clippings.py")]
if not args.dry_run:
    cmd.append("--apply")

try:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
except subprocess.TimeoutExpired:
    proc = None
    print("⚠️ reclaim 超时（120s），跳过本次认领")

if proc:
    # 透传 reclaim 输出（用户看到正常进度 + 汇总）
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    # 解析 RECLAIM_RESULT JSON 行
    reclaim_stats = {}
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RECLAIM_RESULT: "):
            try:
                reclaim_stats = _json.loads(line[len("RECLAIM_RESULT: "):])
            except _json.JSONDecodeError:
                pass
            break
    # 摘要打印
    if reclaim_stats.get("matched", 0) > 0:
        print(f"认领 {reclaim_stats['matched']} 个滞留文件"
              f"（移动 {reclaim_stats.get('moved', 0)}，"
              f"标记 {reclaim_stats.get('marked', 0)}）")
    if reclaim_stats.get("batch_corrupt_skipped"):
        print(f"跳过 {reclaim_stats['batch_corrupt_skipped']} 个批量错乱副本")
    for item in reclaim_stats.get("rollback_failures") or []:
        print(f"⚠️ reclaim 回滚失败（文件位置不可知）：{item}")
    if reclaim_stats.get("aborted"):
        print(f"⚠️ reclaim 中止：{reclaim_stats['aborted']}（剩余滞留下次再认领）")
```

> saver 不因 reclaim 的非零退出码崩溃——subprocess returncode 仅作信号，
> JSON 里的 `aborted` 字段才是 saver 决策依据。reclaim 失败时 saver 继续
> 处理 unsaved 文章（reclaim 是兜底，不应阻塞主流程）。
>
> **已知局限（review v5 #5）**：reclaim_clippings.py Phase 1 的 BaseException
> handler（line 224-231）回滚后 `raise` → 进程立即非零退出，main() 末尾的
> JSON 输出达不到 → saver 解析不到 JSON 行，reclaim_stats 保持空字典（所有
> `.get()` 返回 0）。launchd 环境无 Ctrl+C，极不可能触发；若需覆盖，在
> reclaim 的 `raise` 前先打印含 `aborted` 的 JSON。

---

## 5. 数据流（修复后）

```
incremental_update launch_obsidian():
  [新增] ensure_appnap_disabled()  ← 一次性检查 + 必要时 defaults write
  open -a Obsidian
  等 Vault 加载
↓
saver main 启动
↓
VAULT 可读校验（fail-loud）
↓
init_database()
↓
[新增] subprocess 调 reclaim_clippings.py [--apply]  ← 避免循环引用
  ├─ md5 去重：跳过批量错乱副本（8/2 的 23 副本）
  ├─ rglob 扫 Clippings（含畸形嵌套）
  ├─ 标题匹配 DB unsaved article
  ├─ 事务性 rename + UPDATE + 回滚
  └─ JSON 输出统计（RECLAIM_RESULT 行，saver 解析）
↓
get_stats（reclaim 标的 saved 已计入）
↓
get_unsaved_articles
↓
处理每篇文章（不变）：
  activate_browser (Chrome 打开文章)
  read_page / 验证页 / 日期检测
  activate_browser (Chrome 前台)
  trigger_quick_clip (Alt+Shift+O)
  轮询 25s 找文件（Obsidian 不再被冻结，即时落盘）
```

---

## 6. 验证计划

### 6.1 策略 A 实测（已完成 ✅）

2026-08-03 实测：`NSAppSleepDisabled=1` + 后台 5 分钟 + 最小化 → quick_clip 后
**2s 落盘**（对比 8/2 的 17 分钟）。日志 `/tmp/obsidian_appnap_test.log`。
策略 A 有效。

### 6.2 md5 去重 + rglob（对当前 Clippings/ 实测）

手动跑 `python3 reclaim_clippings.py`（dry-run）：
- 8/2 的 23 副本（md5 相同 + 23 个不同 f.stem）→ 应打印「跳过 1 组批量错乱副本（23 个文件）」
- 畸形嵌套文件（若 Clippings 深处有）→ rglob 能扫到

### 6.3 launchd 实测（最终验证）

`launchctl start com.ima2obsidian.update` 触发完整增量更新，观察
`incremental_update.log`：
- launch_obsidian 行后有 ensure_appnap_disabled 检查
- saver「✅ 完成」计数符合预期（不再 0 成功）
- 次日 saver 跑开头 reclaim_all 认领数 = 0（无滞留 = 健康）

---

## 7. 8/2 故障的事后处理

- **23 个垃圾副本**：手动删除（不进自动化逻辑，一次性操作）
- **8/2 的 23 篇文章**：修复部署后手动重跑 saver 重新保存
  （`obsidian_saved` 保持 0，saver 自然会取到）

---

## 8. 不做的事（YAGNI）

- **不落地 per-article activate / caffeinate**：策略 A（NSAppSleepDisabled）实测有效，
  零运行时开销，无需逐文章创可贴。备选方案留附录 B。
- **不在 saver 里新写 reclaim 逻辑**：复用 reclaim_clippings.py。
- **不改 `launch_obsidian()` 的 `open -a Obsidian`**：只在其前面加 ensure_appnap_disabled。
- **不处理短链文章的 URL 精确匹配**：reclaim_clippings.py 按标题匹配已覆盖大部分。
- **不自动删除批量 flush 副本**：md5 去重只「跳过」不「删除」——保守，避免误删。
- **不加 title sanity check**：标题匹配精度由 normalize_stem + sanitize 双索引保证。

---

## 9. review 处置对照

### v1 review 14 条

| # | 处置 | 体现在 |
|---|------|--------|
| 1 | 采纳：Obsidian 是 Electron | §2 标注 |
| 2-14 | 采纳/部分采纳（详见 v2 对照） | §4.2 |

### v2 review 7 条

| # | 处置 | 体现在 |
|---|------|--------|
| v2-1 | **采纳（致命）**：md5 检测改用 f.stem | §4.2 改动(a) |
| v2-2 | 采纳：run_cua 导入 | （附录 B，本轮不落地） |
| v2-3 | 采纳：run_cua timeout | （附录 B，本轮不落地） |
| v2-4 | 采纳：sys.exit 处置 → JSON 输出 aborted 字段 | §4.2 改动(c) |
| v2-5 | 采纳：rollback_failures → JSON 字段 | §4.2 改动(c)(d) |
| v2-6/7 | 通过 v2-1 修复消除 | §4.2 改动(a) |
| v2-alt | 采纳：先实测系统级方案 → **A 有效，方向 1 简化** | §0 v4、§4.1 |

### v4 review 12 条

| # | 严重度 | 处置 | 体现在 |
|---|--------|------|--------|
| v4-1 | 🔴 | **采纳（方案 A）**：循环引用 → subprocess + JSON 输出 | §0 v5、§4.2 改动(c)(d) |
| v4-2 | 🔴 | 采纳：ensure_obsidian_ready 也调 ensure | §4.1 调用点 2 |
| v4-3 | 🔴 | 采纳：defaults write 加 returncode 检查 | §4.1 函数体 |
| v4-4 | 🟠 | 采纳：md5 用 normalize_stem(f.stem) | §4.2 改动(a) |
| v4-5 | 🟠 | 采纳：补 incremental_update 具名导入 | §4.1 导入说明 |
| v4-6 | 🟠 | 采纳：subprocess.run 加 try/except TimeoutExpired | §4.1 函数体 |
| v4-7 | 🟡 | 采纳：日志按调用场景措辞 | §4.1 调用点 1/2 |
| v4-8 | 🟡 | 采纳：CLI main() aborted 时 sys.exit(1) | §4.2 改动(c) |
| v4-9 | 🟡 | 采纳：aborted_reason = None 初始化 | §4.2 改动(c) |
| v4-10 | 🟢 | YAGNI：Clippings <50 文件，双倍 I/O 可忽略 | — |
| v4-11 | 🟢 | YAGNI：saver 每天跑一次，md5 开销极小 | — |
| v4-12 | 🟢 | 不改：Path __hash__/__eq__ 正常工作 | — |

### v5 review 5 条（无严重，全部采纳）

| # | 严重度 | 处置 | 体现在 |
|---|--------|------|--------|
| v5-1 | 🟡 | 采纳：附录 B caffeinate `-w` → `-u -t` | 附录 B 策略 B |
| v5-2 | 🟡 | 采纳：ensure 日志移入函数内部，调用点不重复 | §4.1 函数体 + 调用点 2 |
| v5-3 | 🟢 | 采纳：JSON 输出补可运行 dict 版本 | §4.2 改动(c) |
| v5-4 | 🟢 | 采纳：reclaim_all() → main() line 104/111 | §4.2 改动(a) |
| v5-5 | 🟢 | 采纳：补 Phase 1 raise 局限说明 | §4.2 改动(d) |

---

## 附录 B：备选方案（策略 A 未来失效时启用）

若未来 macOS 版本不再尊重 `NSAppSleepDisabled`，按以下顺序降级：

**策略 B：saver 运行期 caffeinate**
- 复用 `ima_incremental_update.py:267` 的 `wake_screen()` 模式
- ⚠️ **必须用 `-u`（declare user active），不是 `-w <pid>`**——`-w` 只等 PID 退出、
  不声明用户活跃，对单应用 AppNap 无效；`-u` 才让系统认为用户在场（review v5 #1 修正）
- saver 跑期间 `caffeinate -u -t <N>`（N = saver 预计耗时秒数，如 1800）
- 风险：caffeinate -u 主要抑制显示器睡眠 + 声明用户活跃，对单应用 AppNap 有效性未实测

**策略 C：per-article bring_to_front**
- v3 §4.1 的完整设计（cua-driver `bring_to_front`，每篇 clip 前调用）
- 已有现成代码设计，照抄即可
- 代价：每篇 ~1s 开销（23 篇 23s）；需 cua-driver daemon 在跑
