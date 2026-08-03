# Obsidian AppNap 修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 8/2 Obsidian AppNap 冻结致 Web Clipper 写盘延迟（saver 0 成功/23 失败），通过一次性禁用 AppNap + reclaim 兜底防御。

**Architecture:** 方向 1（主力）= `ensure_appnap_disabled()` 在 Obsidian 启动/已运行时确保 `NSAppSleepDisabled=1`（实测有效，2s 落盘 vs 17 分钟）。方向 4（兜底）= saver 启动时 subprocess 调 `reclaim_clippings.py`，认领有效滞留 + md5 去重跳过批量错乱副本。

**Tech Stack:** Python 3.9（launchd 环境）/ 3.14（交互式），stdlib subprocess/json/hashlib/unittest，无新依赖

## Global Constraints

- **Python 3.9 兼容**（launchd 用 Xcode 自带 3.9）：禁用 3.10+ 语法（match 语句、`X | Y` union type、f-string 嵌套）；测试可用 3.14 + pytest 跑
- **分支**：当前在 `main`。**执行前先切 feature 分支** `git checkout -b fix/obsidian-appnap`（用户规则：主分支不直接 commit）
- **提交规则**：每个 Task 末尾 commit 前**必须请示用户批准**（用户全局规则 CRITICAL）；subagent-driven 在 feature 分支 + 测试通过时可直接 commit，但**不得 push**
- **TDD**：可测试逻辑先写失败测试 → 实现 → 通过；集成性改动用手动验证步骤
- **spec 参考**：`Plans/2026-08-03-obsidian-appnap-fix-design.md` v6（6 轮 review 收敛）

## File Structure

```
ima_common.py            (修改) 新增 ensure_appnap_disabled()
ima_incremental_update.py (修改) 具名导入 + launch_obsidian/ensure_obsidian_ready 加调用
reclaim_clippings.py     (修改) _compute_batch_corrupt_skipped + md5 集成 + rglob + JSON 输出
ima_obsidian_saver.py    (修改) main() subprocess 调 reclaim + 解析 JSON
tests/                   (新建)
  __init__.py
  test_ensure_appnap.py  ensure_appnap_disabled 单元测试
  test_reclaim_dedup.py  _compute_batch_corrupt_skipped 单元测试
```

---

## Task 0: 切 feature 分支

**Files:** 无

- [ ] **Step 1: 切分支**

```bash
git checkout -b fix/obsidian-appnap
```

- [ ] **Step 2: 确认分支**

Run: `git branch --show-current`
Expected: `fix/obsidian-appnap`

---

## Task 1: ima_common.py — ensure_appnap_disabled() + 单元测试

**Files:**
- Modify: `ima_common.py`（末尾新增函数）
- Test: `tests/test_ensure_appnap.py`（新建）

**Interfaces:**
- Produces: `ensure_appnap_disabled() -> bool`（True=已设置，False=刚写入/失败/超时）

- [ ] **Step 1: 写失败测试**

`tests/__init__.py` 已存在（项目有 25 个现有测试文件 + conftest.py，review plan #8），无需创建。新建 `tests/test_ensure_appnap.py`：

```python
import unittest
from unittest.mock import patch, MagicMock
from ima_common import ensure_appnap_disabled


class TestEnsureAppNapDisabled(unittest.TestCase):
    @patch("ima_common.subprocess.run")
    def test_already_set_returns_true_no_write(self, mock_run):
        """defaults read 返回 1 → True，不触发 write"""
        mock_run.return_value = MagicMock(returncode=0, stdout="1\n", stderr="")
        result = ensure_appnap_disabled()
        self.assertTrue(result)
        self.assertEqual(mock_run.call_count, 1)  # 只 read，不 write

    @patch("ima_common.subprocess.run")
    def test_value_zero_writes_and_returns_false(self, mock_run):
        """defaults read 返回 0（键存在但值 False）→ write → False（review plan #7）"""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="0\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        result = ensure_appnap_disabled()
        self.assertFalse(result)
        self.assertEqual(mock_run.call_count, 2)

    @patch("ima_common.subprocess.run")
    def test_not_set_writes_and_returns_false(self, mock_run):
        """defaults read 非 1 → write 成功 → False"""
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="not found"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        result = ensure_appnap_disabled()
        self.assertFalse(result)
        self.assertEqual(mock_run.call_count, 2)

    @patch("ima_common.subprocess.run")
    def test_write_failure_returns_false(self, mock_run):
        """write returncode 非 0 → False"""
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="permission denied"),
        ]
        result = ensure_appnap_disabled()
        self.assertFalse(result)

    @patch("ima_common.subprocess.run")
    def test_timeout_returns_false(self, mock_run):
        """subprocess 超时 → False（不抛异常）"""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="defaults", timeout=5)
        result = ensure_appnap_disabled()
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_ensure_appnap.py -v`
Expected: FAIL — `ImportError: cannot import name 'ensure_appnap_disabled' from 'ima_common'`

- [ ] **Step 3: 实现 ensure_appnap_disabled()**

在 `ima_common.py` 末尾新增（文件已 `import subprocess`）：

```python
def ensure_appnap_disabled() -> bool:
    """确保 Obsidian 的 AppNap 已禁用（NSAppSleepDisabled=1）。

    实测（2026-08-03）：设置后 Obsidian 后台 5 分钟 + 最小化，quick_clip 触发后
    2s 落盘（对比 8/2 未设置时 17 分钟延迟）。一次性系统级设置，零运行时开销。

    幂等：已设置时 no-op 返回 True；未设置时 defaults write 返回 False。

    日志统一在函数内部处理（review v5 #2）：已设置=无日志；刚写入=提示重启；
    失败=错误；超时=警告。调用方不重复打印。

    Returns:
      True = defaults 已是 1
      False = 本次刚写入 / 写入失败 / 超时
    """
    try:
        result = subprocess.run(
            ["defaults", "read", "md.obsidian", "NSAppSleepDisabled"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip() == "1":
            return True  # 已设置
        # 未设置：写入（review v4 #3：检查 returncode）
        write_result = subprocess.run(
            ["defaults", "write", "md.obsidian", "NSAppSleepDisabled", "-bool", "YES"],
            capture_output=True, text=True, timeout=5,
        )
        if write_result.returncode != 0:
            err = write_result.stderr.strip() or "exit {}".format(write_result.returncode)
            print("❌ defaults write NSAppSleepDisabled 失败: {}".format(err), flush=True)
            return False
        # review v5 #2：刚写入成功的日志在函数内统一打印
        print("⚠️ NSAppSleepDisabled 本次刚写入，当前 Obsidian 进程未带标志。", flush=True)
        print("   建议重启 Obsidian（quit + open）让本次 saver 也安全；否则靠 reclaim 兜底。", flush=True)
        return False
    except subprocess.TimeoutExpired:  # review v4 #6
        print("⚠️ defaults read/write 超时（cfprefsd 锁？）", flush=True)
        return False
```

> ⚠️ **Python 3.9 兼容**：用 `.format()` 不用 f-string 嵌套；`capture_output=True` 在 3.7+ 可用。

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_ensure_appnap.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add ima_common.py tests/test_ensure_appnap.py
git commit -m "feat(common): add ensure_appnap_disabled for Obsidian AppNap fix

实测 NSAppSleepDisabled=1 后 Obsidian 后台 5 分钟 quick_clip 2s 落盘
（对比 8/2 未设置时 17 分钟延迟）。幂等检查 + returncode 校验 + timeout 保护。"
```

（commit 前请示用户批准——用户全局规则）

---

## Task 2: reclaim_clippings.py — _compute_batch_corrupt_skipped() + 单元测试

**Files:**
- Modify: `reclaim_clippings.py`（新增模块级函数，不动 main()）
- Test: `tests/test_reclaim_dedup.py`（新建）

**Interfaces:**
- Consumes: `normalize_stem(s: str) -> str`（reclaim_clippings.py:32 已有）
- Produces: `_compute_batch_corrupt_skipped(clip_files: list) -> set[Path]`

- [ ] **Step 1: 写失败测试**

`tests/test_reclaim_dedup.py`：

```python
import unittest
import tempfile
from pathlib import Path
from reclaim_clippings import _compute_batch_corrupt_skipped


class TestComputeBatchCorruptSkipped(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.d = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_file(self, name, content):
        f = self.d / name
        f.write_text(content, encoding="utf-8")
        return f

    def test_batch_corrupt_same_md5_different_stems(self):
        """8/2 模式：同内容 + 不同文件名 → 整组跳过"""
        f1 = self._make_file("文章A.md", "same content")
        f2 = self._make_file("文章B.md", "same content")
        f3 = self._make_file("文章C.md", "same content")
        skipped = _compute_batch_corrupt_skipped([f1, f2, f3])
        self.assertEqual(skipped, {f1, f2, f3})

    def test_legitimate_reclip_same_md5_normalized_same_stem(self):
        """合法重 clip：同内容 + 'Title' vs 'Title 1'（normalize_stem 剥序号）→ 不跳过"""
        f1 = self._make_file("文章.md", "same content")
        f2 = self._make_file("文章 1.md", "same content")
        skipped = _compute_batch_corrupt_skipped([f1, f2])
        self.assertEqual(skipped, set())

    def test_unique_md5_not_skipped(self):
        """md5 唯一 → 不跳过"""
        f1 = self._make_file("文章A.md", "content A")
        f2 = self._make_file("文章B.md", "content B")
        skipped = _compute_batch_corrupt_skipped([f1, f2])
        self.assertEqual(skipped, set())

    def test_single_file_not_skipped(self):
        """单文件（组大小=1）→ 不跳过"""
        f1 = self._make_file("文章.md", "content")
        skipped = _compute_batch_corrupt_skipped([f1])
        self.assertEqual(skipped, set())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_reclaim_dedup.py -v`
Expected: FAIL — `ImportError: cannot import name '_compute_batch_corrupt_skipped'`

- [ ] **Step 3: 实现 _compute_batch_corrupt_skipped()**

**先在文件顶部加 `import hashlib`**（review plan #9：模块级导入，与现有标准库
一起）。`reclaim_clippings.py` 现有 import 在 line 16-23，按字母序加在
`import argparse` 之后、`import os` 之前：

```python
import argparse
import hashlib  # ← 新增（review plan #9，_compute_batch_corrupt_skipped 用）
import os
```

然后在 `normalize_stem` 函数（line 32）之后、`mtime_yymmd`（line 39）之前插入：

```python
def _compute_batch_corrupt_skipped(clip_files):
    """计算批量 flush 错乱副本集合（md5 去重防御）。

    8/2 故障模式：Web Clipper 批量 flush 积压请求时，用当时 Chrome 活跃标签
    内容生成所有副本 → 多个文件 md5 相同（内容相同）+ 文件名不同（各请求
    记录的标题）。同 md5 + 不同 normalize_stem → 判为批量错乱，整组跳过。

    合法重 clip（同篇重抓）：同 md5 + normalize_stem 后同名 → 不跳过
    （Web Clipper 加的 ' 1' 序号被 normalize_stem 剥掉）。

    Args:
      clip_files: list[Path]，Clippings 目录的 .md 文件列表
    Returns:
      set[Path]，应跳过的批量错乱副本路径集合
    """
    md5_groups = {}
    for f in clip_files:
        try:
            digest = hashlib.md5(f.read_bytes()).hexdigest()
        except OSError:
            continue
        md5_groups.setdefault(digest, []).append(f)

    skipped = set()
    for digest, files in md5_groups.items():
        if len(files) > 1:
            stems = {normalize_stem(f.stem) for f in files}
            if len(stems) > 1:  # 不同文章 + 同内容 → 批量 flush 错乱
                skipped.update(files)
    return skipped
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_reclaim_dedup.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add reclaim_clippings.py tests/test_reclaim_dedup.py
git commit -m "feat(reclaim): add _compute_batch_corrupt_skipped for md5 dedup

识别 8/2 批量 flush 错乱模式（同 md5 + 不同 normalize_stem），用 f.stem
（非 frontmatter title）判定——v2 review 指出 frontmatter title 对字节
相同的副本无效。"
```

（commit 前请示用户批准）

---

## Task 3: reclaim_clippings.py — 集成 md5 去重 + glob→rglob + JSON 输出

**Files:**
- Modify: `reclaim_clippings.py` main()（4 处改动）

**Interfaces:**
- Consumes: `_compute_batch_corrupt_skipped`（Task 2）

- [ ] **Step 1: glob → rglob（line 104）**

现有：
```python
    clip_files = sorted(CLIPPINGS_DIR.glob("*.md"))
```
改为：
```python
    # rglob 兼容 Web Clipper 畸形嵌套文件（bug id=2913，\n 进文件名 → 嵌套目录）
    clip_files = sorted(CLIPPINGS_DIR.rglob("*.md"))
```

- [ ] **Step 2: md5 去重集成（line 104 之后、line 111 之前）**

在 `clip_files = sorted(...)`（line 104）之后、`print(f"Clippings 文件: ...")`（line 105）之前插入：

```python
        # md5 去重：跳过批量 flush 错乱副本（8/2 故障：23 文件同 md5 不同名）
        batch_corrupt_skipped = _compute_batch_corrupt_skipped(clip_files)
        if batch_corrupt_skipped:
            print("md5 去重：跳过 {} 个批量错乱副本（疑似 Web Clipper 批量 flush 错乱）"
                  .format(len(batch_corrupt_skipped)))
```

- [ ] **Step 3: 循环开头跳过（line 111）**

现有：
```python
        for f in clip_files:
            stem_norm = normalize_stem(f.stem)
```
改为：
```python
        for f in clip_files:
            if f in batch_corrupt_skipped:
                continue
            stem_norm = normalize_stem(f.stem)
```

- [ ] **Step 4: 汇总打印加 batch_corrupt 计数（line 253 之后）**

现有 3 行连续汇总打印（line 251-253）：`未匹配到未保存文章` / `KB 无对应文件夹` /
`目标已存在`。在**最后一行（conflict 计数，line 253）之后**加（review plan #10：
明确不打断摘要块）：

```python
        print(f"批量错乱副本跳过（md5 去重）: {len(batch_corrupt_skipped)}")
```

- [ ] **Step 5: CLIPPINGS_DIR 缺失路径加 JSON + 保留 sys.exit(1)（line 73-75）**

现有：
```python
    if not CLIPPINGS_DIR.exists():
        print(f"❌ Clippings 目录不存在: {CLIPPINGS_DIR}")
        sys.exit(1)
```
改为（review v5 #3：可运行版本，无 f-string 嵌套）：
```python
    if not CLIPPINGS_DIR.exists():
        print(f"❌ Clippings 目录不存在: {CLIPPINGS_DIR}")
        import json as _json
        _result = {
            "matched": 0, "moved": 0, "marked": 0,
            "no_match": 0, "no_folder": 0, "conflict": 0,
            "batch_corrupt_skipped": 0, "rollback_failures": [],
            "aborted": "CLIPPINGS_DIR not found: {}".format(CLIPPINGS_DIR),
        }
        print("RECLAIM_RESULT: " + _json.dumps(_result, ensure_ascii=False))
        sys.exit(1)  # review v4 #8：CLI 退出码让运维/launchd 监控知道
```

- [ ] **Step 6: main() 末尾加 JSON 输出**

在现有汇总打印的末尾（`if rollback_failures:` 块之后、`main()` 函数结束之前）加。需要先在 `with closing(...)` 块之前初始化 `aborted_reason`（review v4 #9）。

在 `clip_files = sorted(...)` 之前（DB 连接打开后）加初始化：
```python
        aborted_reason = None  # review v4 #9：显式初始化
```

在 main() 最末尾（所有汇总打印后）加：
```python
    # JSON 输出供 saver subprocess 解析（review v4 #1 方案 A：避免循环引用）
    import json as _json
    _result = {
        "matched": len(matched), "moved": moved, "marked": marked,
        "no_match": len(no_match), "no_folder": len(no_folder),
        "conflict": len(conflict),
        "batch_corrupt_skipped": len(batch_corrupt_skipped),
        "rollback_failures": [(str(d), str(s), e) for d, s, e in rollback_failures],
        "aborted": aborted_reason,
    }
    print("RECLAIM_RESULT: " + _json.dumps(_result, ensure_ascii=False))
```

**BaseException handler 也输出 JSON（review plan #3/#6）**：现有 line 224-231
的 Phase 1 BaseException handler 回滚后 `raise`，进程退出，main() 末尾的 JSON
输出不可达 → saver 解析空 dict。改为 raise 前赋值 `aborted_reason` + 打印 JSON：

现有（line 224-231）：
```python
            except BaseException as e:
                print(f"  ❌ reclaim 中断（{type(e).__name__}: {e}），回滚已 rename 文件")
                for src, dst in renamed_pairs:
                    rollback_failures.extend(_safe_rename_back(dst, src))
                moved = 0
                marked = 0
                raise
```

改为（raise 前赋值 + 打印 JSON，review plan #3/#6）：
```python
            except BaseException as e:
                print(f"  ❌ reclaim 中断（{type(e).__name__}: {e}），回滚已 rename 文件")
                for src, dst in renamed_pairs:
                    rollback_failures.extend(_safe_rename_back(dst, src))
                moved = 0
                marked = 0
                aborted_reason = "Phase 1 中断: {}: {}".format(type(e).__name__, e)
                # raise 前先打印 JSON，让 saver subprocess 检测到 aborted
                import json as _json_exc
                _exc_result = {
                    "matched": len(matched), "moved": 0, "marked": 0,
                    "no_match": len(no_match), "no_folder": len(no_folder),
                    "conflict": len(conflict),
                    "batch_corrupt_skipped": len(batch_corrupt_skipped),
                    "rollback_failures": [(str(d), str(s), str(e2)) for d, s, e2 in rollback_failures],
                    "aborted": aborted_reason,
                }
                print("RECLAIM_RESULT: " + _json_exc.dumps(_exc_result, ensure_ascii=False))
                raise
```

> ⚠️ `matched`/`no_match`/`no_folder`/`conflict`/`rollback_failures`/`moved`/`marked` 是 main() 内现有变量——实施时确认它们的作用域覆盖 main() 末尾 + BaseException handler（它们在 line 108 定义，with 块内可见）

- [ ] **Step 7: 集成验证（手动跑 dry-run）**

Run: `cd /Users/berton/Github/Myself/Ima2Obsidian && python3 reclaim_clippings.py`

Expected output 包含：
- `md5 去重：跳过 23 个批量错乱副本`（8/2 的 23 个同 md5 文件）
- 末尾有 `RECLAIM_RESULT: {...}` JSON 行
- JSON 里 `"batch_corrupt_skipped": 23`

- [ ] **Step 8: 验证 JSON 可解析**

Run:
```bash
python3 reclaim_clippings.py 2>/dev/null | grep "^RECLAIM_RESULT:" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()[len('RECLAIM_RESULT: '):]); print('batch_corrupt_skipped:', d['batch_corrupt_skipped'])"
```
Expected: `batch_corrupt_skipped: 23`

- [ ] **Step 9: Commit**

```bash
git add reclaim_clippings.py
git commit -m "feat(reclaim): integrate md5 dedup + rglob + JSON output

- main() 集成 _compute_batch_corrupt_skipped，跳过批量错乱副本
- glob→rglob（兼容畸形嵌套文件，bug id=2913）
- main() 末尾输出 RECLAIM_RESULT JSON 行供 saver subprocess 解析
- CLIPPINGS_DIR 缺失路径加 JSON + 保留 sys.exit(1)"
```

（commit 前请示用户批准）

---

## Task 4: ima_obsidian_saver.py — main() subprocess 调 reclaim

**Files:**
- Modify: `ima_obsidian_saver.py` main()（line 1186 init_database 之后、line 1187 get_stats 之前）

**Interfaces:**
- Consumes: `reclaim_clippings.py` 的 `RECLAIM_RESULT:` JSON 行（Task 3）

- [ ] **Step 1: 在 main() 插入 subprocess 调用**

在 `init_database()`（line 1186）之后、`stats = get_stats(args.kb)`（line 1187）之前插入。文件顶部已有 `import subprocess`、`import sys`、`from pathlib import Path`。

```python
    # reclaim 兜底：subprocess 调 reclaim_clippings.py 认领滞留文件
    # （review v4 #1 方案 A：subprocess 避免 saver↔reclaim_clippings 循环引用）
    # review v4 #2 dry-run 门控
    import json as _json
    _reclaim_cmd = [sys.executable, str(Path(__file__).parent / "reclaim_clippings.py")]
    if not args.dry_run:
        _reclaim_cmd.append("--apply")
    try:
        _proc = subprocess.run(_reclaim_cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        _proc = None
        print("⚠️ reclaim 超时（120s），跳过本次认领")
    except OSError as _e:  # review plan #4：reclaim_clippings.py 缺失等
        _proc = None
        print("⚠️ reclaim 启动失败（{}），跳过本次认领".format(_e))
    if _proc:
        if _proc.stdout:
            print(_proc.stdout, end="")
        if _proc.stderr:
            print(_proc.stderr, end="", file=sys.stderr)
        # 解析 RECLAIM_RESULT JSON 行
        _reclaim_stats = {}
        for _line in (_proc.stdout or "").splitlines():
            if _line.startswith("RECLAIM_RESULT: "):
                try:
                    _reclaim_stats = _json.loads(_line[len("RECLAIM_RESULT: "):])
                except _json.JSONDecodeError:
                    pass
                break
        if _reclaim_stats.get("matched", 0) > 0:
            print("认领 {} 个滞留文件（移动 {}，标记 {}）".format(
                _reclaim_stats["matched"],
                _reclaim_stats.get("moved", 0),
                _reclaim_stats.get("marked", 0)))
        if _reclaim_stats.get("batch_corrupt_skipped"):
            print("跳过 {} 个批量错乱副本".format(_reclaim_stats["batch_corrupt_skipped"]))
        for _item in _reclaim_stats.get("rollback_failures") or []:
            print("⚠️ reclaim 回滚失败（文件位置不可知）：{}".format(_item))
        if _reclaim_stats.get("aborted"):
            print("⚠️ reclaim 中止：{}（剩余滞留下次再认领）".format(_reclaim_stats["aborted"]))
```

> ⚠️ **Python 3.9 兼容**：局部变量用 `_` 前缀避免污染；`.format()` 不用 f-string 嵌套。

- [ ] **Step 2: 集成验证（dry-run）**

Run: `cd /Users/berton/Github/Myself/Ima2Obsidian && python3 ima_obsidian_saver.py --dry-run --limit 1`

Expected：saver 启动时先跑 reclaim（输出 `Clippings 文件: N | DB 未保存文章: M` + `RECLAIM_RESULT:` 行），再打印数据库统计。

- [ ] **Step 3: Commit**

```bash
git add ima_obsidian_saver.py
git commit -m "feat(saver): subprocess reclaim on startup + JSON parse

main() init_database 后调 reclaim_clippings.py（subprocess 避免
循环引用），解析 RECLAIM_RESULT JSON 行打印摘要。dry-run 门控。"
```

（commit 前请示用户批准）

---

## Task 5: ima_incremental_update.py — ensure 调用（两个入口）

**Files:**
- Modify: `ima_incremental_update.py`（line 27-30 导入 + line 598 launch_obsidian + line 622 ensure_obsidian_ready）

**Interfaces:**
- Consumes: `ensure_appnap_disabled`（Task 1）

- [ ] **Step 1: 补具名导入（line 27-30）**

现有：
```python
from ima_common import (
    CUA_DRIVER, IMA_APP_NAME, run_cua, is_daemon_running,
    get_ima_main_window,
)
```
改为：
```python
from ima_common import (
    CUA_DRIVER, IMA_APP_NAME, run_cua, is_daemon_running,
    get_ima_main_window, ensure_appnap_disabled,
)
```

- [ ] **Step 2: launch_obsidian 加 ensure 调用（调用点 1，line 607-608）**

现有：
```python
def launch_obsidian(timeout: int = 30) -> bool:
    """..."""
    log("启动 Obsidian 应用...")
    subprocess.run(
        ["open", "-a", "Obsidian"],
        capture_output=True, timeout=10
    )
```
改为（启动前 ensure，新进程自动带 defaults）：
```python
def launch_obsidian(timeout: int = 30) -> bool:
    """..."""
    log("启动 Obsidian 应用...")
    ensure_appnap_disabled()  # 写入 defaults，紧接着 open 的新进程自动带标志
    subprocess.run(
        ["open", "-a", "Obsidian"],
        capture_output=True, timeout=10
    )
```

- [ ] **Step 3: ensure_obsidian_ready 加 ensure 调用（调用点 2，line 622-626）**

现有：
```python
def ensure_obsidian_ready() -> bool:
    """确保 Obsidian 已运行（未运行则自动启动），供保存器前置检查使用"""
    if is_obsidian_running():
        return True
    return launch_obsidian()
```
改为（review v4 #2：已运行时也要 ensure；review v5 #2：日志在函数内部）：
```python
def ensure_obsidian_ready() -> bool:
    """确保 Obsidian 已运行（未运行则自动启动），供保存器前置检查使用"""
    if is_obsidian_running():
        # review v4 #2：Obsidian 已运行也要确保 AppNap 禁用（否则首次部署
        # Obsidian 已在跑时 ensure 永不调用，8/2 故障复现）。
        # review v5 #2：日志由 ensure_appnap_disabled 内部统一打印
        ensure_appnap_disabled()
        return True
    return launch_obsidian()
```

- [ ] **Step 4: 验证语法 + 导入**

Run: `python3 -c "from ima_incremental_update import launch_obsidian, ensure_obsidian_ready; print('import ok')"`
Expected: `import ok`（无 NameError / SyntaxError）

- [ ] **Step 5: Commit**

```bash
git add ima_incremental_update.py
git commit -m "feat(incremental): call ensure_appnap_disabled at both Obsidian entry points

- launch_obsidian：启动前 ensure（新进程自动带 defaults）
- ensure_obsidian_ready：已运行时也 ensure（覆盖首次部署 Obsidian 在跑场景，
  review v4 #2）
- 补具名导入 ensure_appnap_disabled"
```

（commit 前请示用户批准）

---

## Task 6: 集成验证 + 8/2 清理

**Files:** 无代码改动（验证 + 数据清理）

- [ ] **Step 1: 删除 8/2 的 23 个垃圾副本**

用 md5 识别（review plan #5：比时间戳匹配稳健——8/2 的 23 文件 md5 相同
`6494d071a27035e8c94369a6134d735e`，是批量 flush 错乱的确凿特征）：

```bash
cd "$HOME/Obsidian Vault/Clippings"
TARGET_MD5="6494d071a27035e8c94369a6134d735e"
count=0
for f in *.md; do
  [ "$(md5 -q "$f")" = "$TARGET_MD5" ] && count=$((count+1))
done
echo "$count"
```
Expected: `23`

删除（请示用户批准后）：
```bash
TARGET_MD5="6494d071a27035e8c94369a6134d735e"
for f in *.md; do
  [ "$(md5 -q "$f")" = "$TARGET_MD5" ] && rm "$f"
done
```

- [ ] **Step 2: 手动重跑 saver 保存 8/2 的 23 篇**

```bash
cd /Users/berton/Github/Myself/Ima2Obsidian
python3 ima_obsidian_saver.py --des AI --kb AI --limit 12
python3 ima_obsidian_saver.py --des Invest --kb Invest --limit 5
python3 ima_obsidian_saver.py --des 皮皮鲁的知识库 --kb 皮皮鲁的知识库 --limit 6
```

Expected：每篇「✅ 完成」（NSAppSleepDisabled 已设，Obsidian 不冻结）。

> ⚠️ 跑前确认 Chrome 已开 + Web Clipper 扩展启用。如本次 saver 跑有失败，查
> `[诊断] activate Obsidian 后前台=` 是否出现（Task 5 改动后应无此诊断行——
> 方向 1 不再 per-article activate，靠系统级 NSAppSleepDisabled）。

- [ ] **Step 3: 跑全部单元测试（指定文件，review plan #1）**

Run: `cd /Users/berton/Github/Myself/Ima2Obsidian && python3 -m pytest tests/test_ensure_appnap.py tests/test_reclaim_dedup.py -v`
Expected: 9 passed（test_ensure_appnap 5 + test_reclaim_dedup 4）

> ⚠️ **不要跑 `pytest tests/`**——项目有 227 个现有测试，加新的共 232 个，会混入无关失败。

- [ ] **Step 4: launchd 实测（次日验证）**

等次日 11:50 launchd 自动触发（或手动 `launchctl start com.ima2obsidian.update`），
查 `incremental_update.log`：
- `launch_obsidian` 行后无"需重启"警告（defaults 已设）
- saver「✅ 完成」计数符合预期
- saver 开头有 reclaim 输出（`RECLAIM_RESULT:` 行）
- 次日 reclaim 认领数 = 0（无滞留 = 健康）

- [ ] **Step 5: 清理实测残留**

实测脚本和临时文件清理：
```bash
rm -f /tmp/obsidian_appnap_test.sh /tmp/obsidian_appnap_test.log
```

- [ ] **Step 6: 确认 tests/ 已在 Task 1/2 提交（review plan #2）**

Run: `git log --oneline -5`
Expected: 看到 Task 1/2 的 commit 已含 `tests/test_ensure_appnap.py` + `tests/test_reclaim_dedup.py`。无需额外提交（Task 6 无新代码）。

```bash
# 确认工作树干净（tests/ 无未提交改动）
git status --short
```
Expected: 无输出（工作树干净）

---

## Self-Review

### Spec 覆盖

| spec 要求 | 对应 Task |
|-----------|----------|
| §4.1 ensure_appnap_disabled() | Task 1 |
| §4.1 调用点 1（launch_obsidian） | Task 5 Step 2 |
| §4.1 调用点 2（ensure_obsidian_ready） | Task 5 Step 3 |
| §4.1 具名导入 | Task 5 Step 1 |
| §4.2 改动(a) md5 去重 | Task 2 + Task 3 Step 2-4 |
| §4.2 改动(b) glob→rglob | Task 3 Step 1 |
| §4.2 改动(c) JSON 输出 | Task 3 Step 5-6 |
| §4.2 改动(d) saver subprocess 调用 | Task 4 |
| §6.2 md5 去重验证 | Task 3 Step 7-8 |
| §7 8/2 垃圾副本清理 | Task 6 Step 1 |
| §7 重跑 saver | Task 6 Step 2 |

### Placeholder scan

无 TBD/TODO。所有代码步骤含完整代码。

### Type consistency

- `_compute_batch_corrupt_skipped(clip_files) -> set[Path]`：Task 2 定义，Task 3 调用，签名一致
- `ensure_appnap_disabled() -> bool`：Task 1 定义，Task 5 调用，签名一致
- `RECLAIM_RESULT:` JSON 字段：Task 3 输出，Task 4 解析，字段名一致（matched/moved/marked/no_match/no_folder/conflict/batch_corrupt_skipped/rollback_failures/aborted）

### 已知风险

- Task 3 Step 6 引用的 `matched`/`moved`/`marked`/`no_match`/`no_folder`/`conflict`/`rollback_failures` 是 reclaim_clippings.py main() 内现有变量——实施时确认它们的作用域覆盖 main() 末尾（它们在 line 108 定义，main() 末尾引用 OK）
- Task 6 Step 2 手动重跑 saver 可能因 Chrome 状态/验证页间歇失败——非本修复范围，失败的留给下次 launchd
