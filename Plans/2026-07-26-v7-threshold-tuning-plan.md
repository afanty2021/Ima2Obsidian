# v7 阈值调整 + 漏检自取证 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 把 `_deleted_reason` 阈值 60→100(覆盖实测 65 字违规页) + 加漏检自取证机制(调用点 `_log_possible_miss` + `_POSSIBLE_MISS_SEEN` 节流)。

**Architecture:** `_deleted_reason` 保持纯函数(无副作用);漏检自证移到 `save_one_article` 调用点(避免 `is_verify_page` 双调用时重复打印);节流按 body hash(单次 run 内去重,跨 run 重复对运维有利)。详见 [v7 spec](./2026-07-26-weixin-blocked-violation-detection-design-v7.md)。

**Tech Stack:** Python 3 / pytest

## Global Constraints

- **测试命令**:`python3 -m pytest tests/<file>.py -v`
- **commit message**:简体中文;**禁止 push**
- **代码注释**:简体中文
- **TDD**:先写失败测试 → 跑确认失败 → 实现 → 跑确认通过
- 工作目录:`/Users/berton/Github/Myself/Ima2Obsidian`
- **基线**:`main` 分支(v5 已合并),`_deleted_reason` 当前阈值 60,无 `_log_possible_miss`

---

## Task 1: 阈值 60→100 + docstring/注释同步 + 边界测试

**Files:**
- Modify: `ima_obsidian_saver.py:495-517`(`_deleted_reason` 阈值 + docstring)
- Modify: `ima_obsidian_saver.py:523` 附近(`is_verify_page` docstring)
- Modify: `ima_obsidian_saver.py:854-857`(调用点 `[debug]` 注释 + `# TODO` 锚点)
- Test: `tests/test_deleted_page.py`(加边界测试 + 更新注释)

- [ ] **Step 1: 写失败测试 — 阈值 100 边界**

在 `tests/test_deleted_page.py` 的 `TestDeletedReason` 类末尾追加:

```python
    def test_threshold_99_hits(self):
        """len(body)=99 含关键词 → 命中（v7 阈值 100 边界）"""
        body = "此账号已被屏蔽" + "x" * (99 - len("此账号已被屏蔽"))
        assert len(body) == 99
        assert saver._deleted_reason({"text": body}) == "账号被屏蔽"

    def test_threshold_100_returns_none(self):
        """len(body)=100 含关键词 → 返回 None（v7 阈值 100 边界；capsys 无日志——纯函数）"""
        body = "此账号已被屏蔽" + "x" * (100 - len("此账号已被屏蔽"))
        assert len(body) == 100
        assert saver._deleted_reason({"text": body}) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_deleted_page.py::TestDeletedReason::test_threshold_99_hits tests/test_deleted_page.py::TestDeletedReason::test_threshold_100_returns_none -v`
Expected: `test_threshold_99_hits` FAIL(99 >= 60 → 返回 None,断言 `"账号被屏蔽"` 失败);`test_threshold_100_returns_none` PASS(100 >= 60 → 返回 None,巧合通过)

- [ ] **Step 3: 改 `_deleted_reason` 阈值 60→100**

`ima_obsidian_saver.py:512`:
```python
# before
    if len(body) >= 60:
        return None

# after
    if len(body) >= 100:
        return None
```

docstring(line 501、506):
```python
# before
    判定：len(body) < 60 阈值 + _DELETED_REASON_MAP 关键词子串匹配（k in body，非正则）。
    ...
    阈值是防误判的关键——合法讨论审查的文章前 800 字正文 ≫ 60，靠阈值防 mark_deleted

# after
    判定：len(body) < 100 阈值 + _DELETED_REASON_MAP 关键词子串匹配（k in body，非正则）。
    ...
    阈值是防误判的关键——合法讨论审查的文章前 800 字正文 ≫ 100，靠阈值防 mark_deleted
```

- [ ] **Step 4: 改 `is_verify_page` docstring(line 523 附近)**

```python
# before
    前置 _deleted_reason 排除——在 _deleted_reason 判定范围内（body <60 字且含
    _DELETED_REASON_MAP 关键词）的永久不可恢复页不是验证页...

# after
    前置 _deleted_reason 排除——在 _deleted_reason 判定范围内（body <100 字且含
    _DELETED_REASON_MAP 关键词）的永久不可恢复页不是验证页...
```

- [ ] **Step 5: 改调用点 `[debug]` 注释(line 854-857)**

```python
# before
    # 渐进验证：<60 字阈值对真实屏蔽/违规页是否有效（spec §5 风险缓解措施）
    # 默认开启；运维嫌吵可设 IMA_DEBUG_BODY_LEN=0/false/no/off 关闭
    # TODO(渐进验证)：首篇屏蔽/违规 URL 命中后，根据日志确认 len(body) 真实长度；
    #   若 ≥60 字阈值过紧需调整 _deleted_reason；若确认阈值有效，移除此 print 与门控

# after
    # 渐进验证：<100 字阈值对真实屏蔽/违规页是否有效（spec §5 风险缓解措施 + v7 §3.1）
    # 默认开启；运维嫌吵可设 IMA_DEBUG_BODY_LEN=0/false/no/off 关闭
    # TODO(渐进验证)：首篇屏蔽/违规 URL 命中后，根据日志确认 len(body) 真实长度；
    #   若 ≥100 字阈值过紧需调整 _deleted_reason；若确认阈值有效，移除此 print 与门控
```

- [ ] **Step 6: 更新现有测试注释(line 54、58、66)**

`tests/test_deleted_page.py`:
- line 54:`合法文章 body 远超 60 字` → `合法文章 body 远超 100 字`
- line 58:`assert len(long_body) > 60` → `assert len(long_body) > 100`(确认 long_body 仍 >100;若不够加 `"详细情况分析"` 倍数)
- line 66:`body='' 时 title+body <60` → `body='' 时 body <100`

- [ ] **Step 7: 跑全测试确认通过**

Run: `python3 -m pytest tests/ -v`
Expected: PASS(现有测试 + 新 2 个边界测试)

- [ ] **Step 8: commit**

```bash
git add ima_obsidian_saver.py tests/test_deleted_page.py
git commit -m "feat(saver): _deleted_reason 阈值 60→100 + docstring/注释同步

实测违规页最大 65 字（PR #5 首日数据），阈值 60 漏检；100 留 +35 余量。
is_verify_page docstring + 调用点 # TODO 锚点同步阈值。

spec v7 §3.1/§3.4/§3.5"
```

---

## Task 2: 漏检自取证(_log_possible_miss + 调用点 + 节流)

**Files:**
- Modify: `ima_obsidian_saver.py:474` 之后(新增 `_POSSIBLE_MISS_SEEN` + `_log_possible_miss`)
- Modify: `ima_obsidian_saver.py:853-870`(调用点加漏检分支)
- Test: `tests/test_deleted_page.py`(新增 `_log_possible_miss` 单元测试 + 集成测试)

- [ ] **Step 1: 写失败测试 — `_log_possible_miss` 单元测试**

在 `tests/test_deleted_page.py` 末尾追加新测试类:

```python
class TestLogPossibleMiss:
    """_log_possible_miss: 漏检自取证（v7 §3.2）"""

    def setup_method(self):
        """每个测试前清空节流集合（防 module-level 状态串扰，v7 review #5）"""
        saver._POSSIBLE_MISS_SEEN.clear()

    def test_no_keyword_quiet(self, capsys):
        """body 不含 DELETED 关键词 → capsys 无输出"""
        saver._log_possible_miss("x" * 200)
        assert capsys.readouterr().out == ""

    def test_with_keyword_format(self, capsys):
        """含关键词 → 输出含 len/hits/body[:200]"""
        body = "此账号已被屏蔽" + "x" * 200
        saver._log_possible_miss(body)
        out = capsys.readouterr().out
        assert "[疑似漏检自取证]" in out
        assert "len(body)=" in out
        assert "此账号已被屏蔽" in out  # hits 含关键词

    def test_caps_body_at_200(self, capsys):
        """body 300 字 → 输出只含前 200 字（v7 review #5 修复）"""
        body = "此账号已被屏蔽" + "y" * 300
        saver._log_possible_miss(body)
        out = capsys.readouterr().out
        assert "yyy" not in out  # 第 201+ 字的 y 不在输出里
        assert out.count("y") <= 200

    def test_throttle_same_body(self, capsys):
        """同一 body 调 2 次 → 只打 1 次（v7 #6 节流）"""
        body = "此账号已被屏蔽" + "z" * 200
        saver._log_possible_miss(body)
        saver._log_possible_miss(body)
        out = capsys.readouterr().out
        assert out.count("[疑似漏检自取证]") == 1

    def test_different_body_both_print(self, capsys):
        """不同 body → 各打 1 次"""
        saver._log_possible_miss("此账号已被屏蔽" + "a" * 200)
        saver._log_possible_miss("该内容已被发布者删除" + "b" * 200)
        out = capsys.readouterr().out
        assert out.count("[疑似漏检自取证]") == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_deleted_page.py::TestLogPossibleMiss -v`
Expected: FAIL(`AttributeError: module 'ima_obsidian_saver' has no attribute '_POSSIBLE_MISS_SEEN'`)

- [ ] **Step 3: 新增 `_POSSIBLE_MISS_SEEN` + `_log_possible_miss`**

在 `ima_obsidian_saver.py` 找到 `_DEBUG_BODY_LEN_SEEN` 定义(line 474 附近),在其后追加:

```python
# v7 #6：漏检自证节流集合，同一 body 内容只打一次（与 _DEBUG_BODY_LEN_SEEN 设计相仿）。
# 注意：module-level set 仅存活于单个 Python 进程，跨 launchd run 会重置——
# 跨 run 重复反而对运维有利（不会漏报漏检 URL）。
_POSSIBLE_MISS_SEEN: Set[int] = set()


def _log_possible_miss(body: str) -> None:
    """body >= 阈值但含 DELETED 关键词时打自取证诊断（v7 §3.2）。

    不承诺「精准触发排除合法文章」——子串匹配无法区分合法引用整句 vs 真实漏检。
    讨论审查的媒体文章会触发噪声，已知 tradeoff（靠节流 + 接受）。
    body[:200] 截断（覆盖文末 chrome 行，为 v8 收集证据）。
    _POSSIBLE_MISS_SEEN 节流：同一 body 只打一次（单次 run 内去重）。
    """
    body_hash = hash(body)
    if body_hash in _POSSIBLE_MISS_SEEN:
        return
    _POSSIBLE_MISS_SEEN.add(body_hash)  # 移到扫描前（无关键词也记录，避免重扫）
    hits = [k for k, _ in _DELETED_REASON_MAP if k in body]
    if not hits:
        return
    print(f"    [疑似漏检自取证] len(body)={len(body)} 命中关键词={hits} body[:200]={body[:200]!r}")
```

- [ ] **Step 4: 跑单元测试确认通过**

Run: `python3 -m pytest tests/test_deleted_page.py::TestLogPossibleMiss -v`
Expected: PASS(5 个测试)

- [ ] **Step 5: 写失败测试 — 调用点集成测试**

在 `tests/test_deleted_page.py::TestSaveOneArticleDeletedPath` 类末尾追加:

```python
    def test_save_one_article_long_body_with_keyword_logs_miss(self, isolated_vault, capsys):
        """长 body(>=100) + 含 DELETED 关键词（不含 VERIFY）→ 漏检自证打 1 次（v7 §4.3）

        v7 review #2 修复：单次调用内只打 1 次（_deleted_reason 纯函数，自证在调用点）。
        """
        saver._POSSIBLE_MISS_SEEN.clear()  # 防串扰
        vault, clip_dir = isolated_vault
        # body 含 DELETED 关键词但不含 VERIFY_KEYWORDS（避免 is_verify_page 走 True 分支）
        body = "此账号已被屏蔽" + "填充文本。" * 30  # len >= 100
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "T", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=False), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "某文章", "text": body}), \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault", return_value=(False, None)), \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        # 长违规页走 quick_clip 0 落盘 → ("failed", None)
        assert result == ("failed", None)
        captured = capsys.readouterr().out
        # 漏检自证只打 1 次（v7 review #2：不是 v6 的 2 次）
        assert captured.count("[疑似漏检自取证]") == 1
```

- [ ] **Step 6: 跑集成测试确认失败**

Run: `python3 -m pytest tests/test_deleted_page.py::TestSaveOneArticleDeletedPath::test_save_one_article_long_body_with_keyword_logs_miss -v`
Expected: FAIL(当前调用点没有 `_log_possible_miss` 调用,count == 0 != 1)

- [ ] **Step 7: 调用点加漏检分支**

`ima_obsidian_saver.py` 找到 `reason = _deleted_reason(snap)` + `if reason is not None: ... return "deleted", None` 段。在 `return "deleted", None` 之后(即 `if reason is not None` 块之后)追加:

```python
    # v7：漏检自取证——_deleted_reason 返回 None 但 body>=100，调 _log_possible_miss
    #     （调用点显式判；不在 _deleted_reason 内部——避免 is_verify_page 双调用时重复打印）
    body = (snap or {}).get("text") or ""
    if len(body) >= 100:
        _log_possible_miss(body)
```

- [ ] **Step 8: 跑全测试确认通过**

Run: `python3 -m pytest tests/ -v`
Expected: PASS(现有测试 + Task 1 边界测试 + Task 2 单元/集成测试)

- [ ] **Step 9: commit**

```bash
git add ima_obsidian_saver.py tests/test_deleted_page.py
git commit -m "feat(saver): _log_possible_miss 漏检自取证 + 调用点 + _POSSIBLE_MISS_SEEN 节流

_deleted_reason 保持纯函数（无副作用）；自证在 save_one_article 调用点显式判
（避免 is_verify_page 双调用时打 2 次）。节流按 body hash（单次 run 内去重，
跨 run 重复对运维有利——不漏报）。body[:200] 覆盖文末 chrome 行。

spec v7 §3.2/§3.3"
```

---

## Self-Review

### Spec coverage

| v7 spec 章节 | 覆盖 task |
|---|---|
| §3.1 `_deleted_reason` 阈值 + 纯函数 | Task 1(阈值)+ 确认纯函数(基线已纯) |
| §3.2 `_log_possible_miss` + `_POSSIBLE_MISS_SEEN` | Task 2 Step 3 |
| §3.3 调用点漏检分支 | Task 2 Step 7 |
| §3.4 `is_verify_page` docstring | Task 1 Step 4 |
| §3.5 调用点注释 + # TODO | Task 1 Step 5 |
| §4.1 边界测试 | Task 1 Step 1 |
| §4.2 `_log_possible_miss` 单元测试 | Task 2 Step 1 |
| §4.3 集成测试 | Task 2 Step 5 |

无遗漏。

### Placeholder scan

无 TBD/TODO(plan 步骤里)。filler 用「填充文本。」字面值。所有代码块完整。

### Type consistency

- `_POSSIBLE_MISS_SEEN: Set[int]` — Task 2 Step 3 定义,测试 Step 1 消费 ✓
- `_log_possible_miss(body: str) -> None` — Task 2 Step 3 定义,调用点 Step 7 消费 ✓
- `hash(body)` 返回 int,与 `Set[int]` 一致 ✓
