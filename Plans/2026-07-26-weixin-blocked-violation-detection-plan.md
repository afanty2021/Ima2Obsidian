# 微信屏蔽/违规异常页检测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 saver 正确识别并永久跳过微信「此账号已被屏蔽」「此内容因违规无法查看」两类永久不可恢复页,消除反复打开导致的 0 落盘假告警;顺带通过 `is_verify_page` 前置排除消除屏蔽页被误判为验证页时的 ~12-14s 重试浪费。

**Architecture:** 方案 A——扩删除页关键词集(以 `_DELETED_REASON_MAP` 为唯一源),复用现有 `mark_deleted` 路径(DB `status='deleted'`)。判定逻辑下沉到 `_deleted_reason` 单源函数(只查 body 不查 title,防标题误杀)。`is_verify_page` 前置 `_deleted_reason` 排除,消除屏蔽页 verify 浪费。维持 v1 调用顺序,保留「验证后转删除」链路。详见 [spec](./2026-07-25-weixin-blocked-violation-detection-design.md)。

**Tech Stack:** Python 3 / SQLite3 / AppleScript subprocess / pytest

## Global Constraints

- **提交规则**:每个 task 结尾 commit;commit message 用简体中文;**禁止 `git push`**(用户全局规则)
- **测试命令**:`python3 -m pytest tests/<file>.py -v`(项目用 python3,非 python)
- **代码注释**:简体中文
- **YAGNI**:不做的事见 spec §6(不新增 status / 不改 schema / 不交换顺序 / 不抽取公共子集 / 不做 innerText 实证——靠 debug 日志渐进验证)

---

## File Structure

| 文件 | 责任 | 改动类型 |
|---|---|---|
| `ima_obsidian_saver.py` | 主逻辑(常量 + `_deleted_reason` + `is_verify_page` + `save_one_article` + docstrings) | Modify |
| `tests/test_deleted_page.py` | `_deleted_reason` 单元测试 + `save_one_article` 集成测试 | Modify(删 `TestIsDeletedPage`,加 `TestDeletedReason` + 集成测试) |
| `tests/test_verify_page.py` | `is_verify_page` 前置排除测试 | Modify(追加) |
| `tests/test_malformed_dir.py` | `_is_verify_clipping` 路径测试 | Modify(追加) |
| `ima_incremental_update.py` | 不动 | — |

---

## Task 1: `_DELETED_REASON_MAP` + `_deleted_reason` 单源判定(只查 body)

**Files:**
- Modify: `ima_obsidian_saver.py:464` 之前(新增 `_DELETED_REASON_MAP` + `_deleted_reason`)
- Modify: `ima_obsidian_saver.py:479-482`(删除 `DELETED_KEYWORDS`)
- Modify: `ima_obsidian_saver.py:485-496`(删除 `is_deleted_page`)
- Test: `tests/test_deleted_page.py`(删 `TestIsDeletedPage`,加 `TestDeletedReason`)

**Interfaces:**
- Produces: `_DELETED_REASON_MAP: Tuple[Tuple[str,str],...]`、`_deleted_reason(snapshot: Optional[dict]) -> Optional[str]`
- 后续 task 消费:Task 2 `is_verify_page` 前置排除调 `_deleted_reason`;Task 4 `save_one_article` 调用点调 `_deleted_reason`

- [ ] **Step 1: 写失败测试 — `TestDeletedReason` 类(替换 `TestIsDeletedPage`)**

打开 `tests/test_deleted_page.py`,把现有的 `class TestIsDeletedPage:` 整个类(line 18-54)替换为下面的 `TestDeletedReason`。注意新增 `test_legit_title_with_keyword_body_empty_not_deleted`(只查 body 的标题误杀防护,spec §2 决策 3):

```python
class TestDeletedReason:
    """_deleted_reason: 永久不可恢复页判定 + reason 映射（单源，只查 body）。"""

    def test_hit_publisher_deleted(self):
        """body 含「该内容已被发布者删除」→ 命中 reason='发布者删除'"""
        assert saver._deleted_reason({"text": "该内容已被发布者删除"}) == "发布者删除"

    def test_hit_violation_deleted_old(self):
        """body 含「此内容因违规已删除」（旧文案）→ '违规不可查看'"""
        assert saver._deleted_reason({"text": "此内容因违规已删除"}) == "违规不可查看"

    def test_hit_violation_unavailable_new(self):
        """body 含「此内容因违规无法查看」（新文案）→ '违规不可查看'"""
        assert saver._deleted_reason({"text": "此内容因违规无法查看"}) == "违规不可查看"

    def test_hit_blocked_account(self):
        """body 含「此账号已被屏蔽」（前缀匹配）→ '账号被屏蔽'"""
        assert saver._deleted_reason({"text": "此账号已被屏蔽，内容无法查看"}) == "账号被屏蔽"

    def test_miss_normal_article(self):
        """正常文章 body 不命中"""
        assert saver._deleted_reason({"title": "别只循环听英文歌", "text": "正文内容"}) is None

    def test_miss_verify_page(self):
        """微信验证页不应被判为删除页（验证页可恢复、删除页永久，处理路径不同）"""
        assert saver._deleted_reason({"title": "验证", "text": "当前环境异常，完成验证"}) is None

    def test_none_snapshot(self):
        assert saver._deleted_reason(None) is None

    def test_empty_snapshot(self):
        assert saver._deleted_reason({}) is None

    def test_long_article_with_phrase_not_deleted(self):
        """合法长文章 body 引用删除整句（讨论审查/媒体类）→ 不误判

        阈值是防误判的关键——合法文章 body 远超 60 字。
        """
        long_body = ("近日有读者发现某公众号文章打开后提示该内容已被发布者删除，"
                     "据悉该文章此前因违规被投诉。" + "详细情况分析" * 20)
        assert len(long_body) > 60  # 前置：确实是长 body
        assert saver._deleted_reason({"title": "媒体报道", "text": long_body}) is None

    def test_legit_title_with_keyword_body_empty_not_deleted(self):
        """合法文章 title 含关键词短语但 body 为空（慢加载）→ 不命中（只查 body，防标题误杀）

        新增第 4 关键词「此账号已被屏蔽」是名词性短语，合法文章 title 可能含此短语
        （如「评此账号已被屏蔽现象」）。若 _deleted_reason 并 title 扫描，慢加载
        body='' 时 title+body <60 + 子串命中 → mark_deleted 永久跳过合法文章。
        只查 body 防此误杀。
        """
        snap = {"title": "评此账号已被屏蔽现象", "text": ""}
        assert saver._deleted_reason(snap) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_deleted_page.py::TestDeletedReason -v`
Expected: FAIL(`AttributeError: module 'ima_obsidian_saver' has no attribute '_deleted_reason'`)

- [ ] **Step 3: 实现 `_DELETED_REASON_MAP` + `_deleted_reason`**

打开 `ima_obsidian_saver.py`。找到 line 464 的 `def is_verify_page`。在它**之前**(line 462-463 的空行处)插入 `_DELETED_REASON_MAP` + `_deleted_reason`。**放在 `is_verify_page` 之前**——后续 Task 2 会让 `is_verify_page` 调用 `_deleted_reason`,前向定义避免阅读时跳转:

```python
# ==================== 永久不可恢复页判定（单源） ====================

# 三类永久不可恢复页（行为一致：mark_deleted 永久跳过，不计 failed）：
#   发布者删除 / 平台下架违规内容 / 账号被平台屏蔽
# 顺序敏感：首条命中决定 reason（近义关键词放一起，如两条违规文案映射同一 reason）。
# 修改本表会影响：_deleted_reason（判定源）、is_verify_page（前置排除）——改词表时
# 同步审视这些调用方。
# 匹配语义：全部子串匹配（k in text，非正则）；每条标注 prefix/sentence 见行末注释。
_DELETED_REASON_MAP = (
    # 前 3 条 sentence（整句本身就是强信号，极不可能出现在合法短文本；前缀化收益小）
    ("该内容已被发布者删除",   "发布者删除"),        # sentence
    ("此内容因违规已删除",     "违规不可查看"),      # sentence（旧文案）
    ("此内容因违规无法查看",   "违规不可查看"),      # sentence（新文案）
    # 第 4 条 prefix（「内容无法查看」是通用后缀，前缀对文案微调鲁棒）
    ("此账号已被屏蔽",         "账号被屏蔽"),        # prefix
)


def _deleted_reason(snapshot: Optional[dict]) -> Optional[str]:
    """永久不可恢复页判定 + reason 映射（单源实现）。

    返回 None ⇔ 非删除页（含普通文章、验证页、空快照）；返回 reason 字符串 ⇔ 是
    永久不可恢复页（发布者删除 / 违规下架 / 账号屏蔽）。

    判定：len(body) < 60 阈值 + _DELETED_REASON_MAP 关键词子串匹配（k in body，非正则）。
    只查 body（snapshot['text']），不并 title——删除页 title 恒为「微信公众平台」不含
    关键词，并 title 无益；而合法文章 title 可能含「此账号已被屏蔽」等名词性短语
    （如「评此账号已被屏蔽现象」），并 title 会在慢加载 body='' 时误杀合法文章。

    阈值是防误判的关键——合法讨论审查的文章前 800 字正文 ≫ 60，靠阈值防 mark_deleted
    永久跳过导致不可逆数据丢失。不得简化成纯关键词匹配（丢阈值 = 误杀合法文章）。
    """
    if not snapshot:
        return None
    body = snapshot.get("text") or ""    # 只查 body，不并 title（防标题误杀）
    if len(body) >= 60:
        return None
    for keyword, reason in _DELETED_REASON_MAP:    # 顺序敏感：首条命中决定 reason
        if keyword in body:                         # 子串匹配（非正则）
            return reason
    return None

```

- [ ] **Step 4: 跑新测试确认通过**

Run: `python3 -m pytest tests/test_deleted_page.py::TestDeletedReason -v`
Expected: PASS(10 个测试全过)

- [ ] **Step 5: 删除 `DELETED_KEYWORDS` + `is_deleted_page`**

打开 `ima_obsidian_saver.py`。找到 line 479-482 的 `DELETED_KEYWORDS` 定义(含其上 3 行注释,line 479-482)。**整段删除**(含注释):

```python
# 微信「文章已被发布者删除」特征词。这类文章已不存在，永远无法保存——若保持未保存，
# 每次运行都会反复打开它（0 落盘 → failed_count++ → 触发上游告警）。检测到即 mark_deleted
# 把 status 改 'deleted'，自动从所有 status='success' 查询消失，永久跳过。
DELETED_KEYWORDS = ("该内容已被发布者删除", "此内容因违规已删除")
```

再找到 line 485-496 的 `is_deleted_page` 函数(委托改造前的原函数)。**整段删除**:

```python
def is_deleted_page(snapshot: Optional[dict]) -> bool:
    """判断页面快照是否为「文章已被发布者删除」页（纯函数，与 is_verify_page 同构）。
    ...
    """
    if not snapshot:
        return False
    text = (snapshot.get("text") or "") + (snapshot.get("title") or "")
    return len(text) < 60 and any(k in text for k in DELETED_KEYWORDS)
```

- [ ] **Step 6: 跑全测试确认无残留引用**

Run: `python3 -m pytest tests/test_deleted_page.py tests/test_verify_page.py -v`
Expected: PASS。**若 `TestSaveOneArticleDeletedPath`(line 57-132)报错引用 `is_deleted_page`**,改为 `_deleted_reason(snap) is not None`(这些测试在 Task 4 会重写,这里临时修引用错误即可)。

具体:`test_deleted_page_short_circuits` 等用例若 patch 了 `is_deleted_page` 或调用了它,改为 patch/调 `_deleted_reason`。

- [ ] **Step 7: commit**

```bash
git add ima_obsidian_saver.py tests/test_deleted_page.py
git commit -m "refactor(saver): _deleted_reason 单源判定（只查 body）+ 删 DELETED_KEYWORDS/is_deleted_page

- 新增 _DELETED_REASON_MAP（唯一源）+ _deleted_reason（单源判定，只查 body 防标题误杀）
- 删除 DELETED_KEYWORDS（沦为死代码）和 is_deleted_page（无生产引用）
- TestIsDeletedPage → TestDeletedReason（含标题误杀防护测试）

spec §3.1-§3.2,§6 教训 #2/#4"
```

---

## Task 2: `is_verify_page` 前置 `_deleted_reason` 排除

**Files:**
- Modify: `ima_obsidian_saver.py:464-476`(`is_verify_page` 首行加前置排除 + docstring)
- Test: `tests/test_verify_page.py`(追加 4 个测试)

**Interfaces:**
- Consumes: `_deleted_reason(snapshot)` from Task 1
- Produces: `is_verify_page` 对删除页返回 False(不再误判为验证页)

- [ ] **Step 1: 写失败测试 — 4 个前置排除测试**

打开 `tests/test_verify_page.py`。在 `class TestIsVerifyPage:` 类内末尾(`test_verify_title_with_long_text_not_misjudged` 方法之后)追加:

```python
    def test_verify_page_excludes_blocked_account_page(self):
        """屏蔽页（body 含「此账号已被屏蔽」）→ 不是验证页（前置 _deleted_reason 排除）

        防止 handle_verify_page 对屏蔽页浪费 ~12-14s 重试（click_confirm 误点通用按钮
        + 两轮 attempt sleep）。spec §3.3 决策 4。
        """
        snap = {"title": "微信公众平台", "text": "此账号已被屏蔽，内容无法查看"}
        assert saver.is_verify_page(snap) is False

    def test_verify_page_excludes_violation_unavailable_page(self):
        """违规页（新文案）→ 不是验证页"""
        snap = {"title": "微信公众平台", "text": "此内容因违规无法查看"}
        assert saver.is_verify_page(snap) is False

    def test_verify_page_excludes_publisher_deleted_page(self):
        """发布者删除页 → 不是验证页（回归保护）"""
        snap = {"title": "微信", "text": "该内容已被发布者删除"}
        assert saver.is_verify_page(snap) is False

    def test_verify_page_keeps_real_verify_page(self):
        """真验证页（body 不含 DELETED 关键词）→ 仍命中（验证后转删除链路保留）"""
        snap = {"title": "微信公众平台", "text": "当前环境异常，完成验证"}
        assert saver.is_verify_page(snap) is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_verify_page.py::TestIsVerifyPage::test_verify_page_excludes_blocked_account_page tests/test_verify_page.py::TestIsVerifyPage::test_verify_page_excludes_violation_unavailable_page tests/test_verify_page.py::TestIsVerifyPage::test_verify_page_excludes_publisher_deleted_page -v`
Expected: 3 个 FAIL(屏蔽/违规/删除页当前被 `title=='微信公众平台' and len(text)<50` 误判为 True)

- [ ] **Step 3: 实现 `is_verify_page` 前置排除**

打开 `ima_obsidian_saver.py`。找到 `def is_verify_page`(Task 1 后,位置约 line 510-522,因前面插入了 `_DELETED_REASON_MAP` + `_deleted_reason`)。在 `if not snapshot: return False` 之后插入前置排除,并更新 docstring:

```python
def is_verify_page(snapshot: Optional[dict]) -> bool:
    """判断页面快照是否为微信风控验证页。

    前置 _deleted_reason 排除——在 _deleted_reason 判定范围内（body <60 字且含
    _DELETED_REASON_MAP 关键词）的永久不可恢复页不是验证页，避免 handle_verify_page
    对屏蔽/违规页浪费 ~12-14s 重试（click_confirm 误点通用按钮 + 两轮 attempt sleep）。
    验证页 body 不含 _DELETED_REASON_MAP 关键词 → _deleted_reason 返回 None → 原逻辑不变。
    """
    if not snapshot:
        return False
    if _deleted_reason(snapshot) is not None:       # 前置排除：删除页不是验证页
        return False
    title = snapshot.get("title") or ""
    text = snapshot.get("text") or ""
    if any(k in text + title for k in VERIFY_KEYWORDS):
        return True
    return title == "微信公众平台" and len(text) < 50
```

- [ ] **Step 4: 跑测试确认通过(新测试 + 现有 7 个回归)**

Run: `python3 -m pytest tests/test_verify_page.py -v`
Expected: PASS(11 个测试:原 7 个 + 新 4 个)

- [ ] **Step 5: commit**

```bash
git add ima_obsidian_saver.py tests/test_verify_page.py
git commit -m "feat(saver): is_verify_page 前置 _deleted_reason 排除

屏蔽/违规/删除页不再被误判为验证页 → 消除 handle_verify_page ~12-14s 重试浪费。
验证页 body 不含 DELETED 关键词 → 原逻辑不变 → 验证后转删除链路保留。

spec §3.3 决策 4"
```

---

## Task 3: `DELETED_CLIPPING_MARKERS` 全用整句 + `_is_verify_clipping` 路径测试

**Files:**
- Modify: `ima_obsidian_saver.py:581`(`DELETED_CLIPPING_MARKERS` 4 条整句 + 注释)
- Test: `tests/test_malformed_dir.py`(追加 3 个路径测试)

**Interfaces:**
- Consumes: 无新接口
- Produces: `DELETED_CLIPPING_MARKERS` 扩展为 4 条整句

- [ ] **Step 1: 写失败测试 — 3 个路径测试**

打开 `tests/test_malformed_dir.py`。找到 `test_long_article_with_deleted_phrase_not_skipped` 方法(line 99-109)之后追加。注意:**filler 必须用「填充文本。」字面值**(spec §4.3,禁含 VERIFY_CLIPPING_MARKERS):

```python
    def test_clipping_title_weixin_pub_platform_blocked(self):
        """屏蔽页落盘（frontmatter title=微信公众平台）→ path ① 强信号命中 → 不认领

        生产真实路径：微信系统提示页落盘后 frontmatter title 恒为「微信公众平台」，
        _is_verify_clipping 第一检 title==微信公众平台 直接命中。
        """
        md = tmp_path / "blocked.md"
        md.write_text('---\ntitle: "微信公众平台"\n---\n此账号已被屏蔽，内容无法查看。',
                      encoding="utf-8")
        assert saver._is_verify_clipping(md) is True

    def test_clipping_blocked_marker_no_special_title(self):
        """title 非「微信公众平台」+ DELETED_CLIPPING_MARKERS 命中 + <200 字 → path ② 命中

        生产上 path ① 抢先（title 恒为微信平台），path ② 是概率性兜底（应对 title 变种）。
        本测试故意用别的 title 才能走到 path ②，验证 DELETED_CLIPPING_MARKERS 兜底生效。
        """
        md = tmp_path / "v.md"
        md.write_text('---\ntitle: "此账号已被屏蔽"\n---\n此账号已被屏蔽，内容无法查看',
                      encoding="utf-8")
        assert len(md.read_text(encoding="utf-8")) < 200  # 前置：短文件
        assert saver._is_verify_clipping(md) is True

    def test_clipping_long_article_with_blocked_marker_not_skipped(self):
        """合法长文章含屏蔽整句 → 不误判（阈值护栏）

        body 含 DELETED_CLIPPING_MARKERS 整句（非前缀）——若阈值被改成 2，path ② 命中
        → 测试失败 → 暴露阈值回归。filler 必须用「填充文本。」字面值，禁含 VERIFY 标记
        （「环境异常」/「完成验证」/「去验证」），否则 path ③ 抢先命中与预期矛盾。
        """
        md = tmp_path / "media.md"
        body = "此账号已被屏蔽，内容无法查看。" + "填充文本。" * 30
        md.write_text(f'---\ntitle: "谈审查"\n---\n{body}', encoding="utf-8")
        assert len(md.read_text(encoding="utf-8")) > 200  # 前置：长文件
        assert saver._is_verify_clipping(md) is False
```

> **注意**:`test_clipping_title_weixin_pub_platform_blocked` 和 `test_clipping_blocked_marker_no_special_title` 用到 `tmp_path` fixture。检查 `tests/test_malformed_dir.py` 现有测试是否已用 `tmp_path`(若已用则直接复用;若用 `monkeypatch` 需调整 fixture 签名)。

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_malformed_dir.py::TestMalformedDir::test_clipping_blocked_marker_no_special_title tests/test_malformed_dir.py::TestMalformedDir::test_clipping_long_article_with_blocked_marker_not_skipped -v`
Expected: `test_clipping_blocked_marker_no_special_title` FAIL(当前 `DELETED_CLIPPING_MARKERS` 只 2 条,不含屏蔽整句);`test_clipping_long_article_with_blocked_marker_not_skipped` PASS(当前长文章不命中,巧合通过)

- [ ] **Step 3: 扩展 `DELETED_CLIPPING_MARKERS`**

打开 `ima_obsidian_saver.py`。找到 `DELETED_CLIPPING_MARKERS` 定义(Task 1 后约 line 605-610)。替换为 4 条整句 + 注释:

```python
# 与 _DELETED_REASON_MAP 分离：CLIPPING 场景 .md 文件检测，全用整句更保守。
# 前 3 条与 _DELETED_REASON_MAP 有意保持一致（微信改文案时维护者须同步两处）；
# 不抽取公共子集——抽象成本 > 低频同步成本（YAGNI）。
# 注意：生产 CLIPPING 路径上，_is_verify_clipping ① title=='微信公众平台' 第一检抢先
# 命中（微信系统提示页落盘 title 恒为此），② 对屏蔽/违规页是概率性兜底（应对 title 变种）。
DELETED_CLIPPING_MARKERS = (
    "该内容已被发布者删除",
    "此内容因违规已删除",
    "此内容因违规无法查看",
    "此账号已被屏蔽，内容无法查看",  # sentence（_DELETED_REASON_MAP 第 4 条用 prefix；策略不同）
)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_malformed_dir.py -v`
Expected: PASS(现有 5 个 + 新 3 个 = 8 个测试)

- [ ] **Step 5: commit**

```bash
git add ima_obsidian_saver.py tests/test_malformed_dir.py
git commit -m "feat(saver): DELETED_CLIPPING_MARKERS 扩 4 条整句 + 路径测试

新增「此内容因违规无法查看」「此账号已被屏蔽，内容无法查看」两条整句。
test_clipping_* 三测显式区分 _is_verify_clipping 三检路径（①title/②MARKERS/③阈值）。

spec §3.1,§4.3"
```

---

## Task 4: `save_one_article` 调用点 + 集成测试

**Files:**
- Modify: `ima_obsidian_saver.py:791-803`(调用点:维持 v1 顺序 + 注释三类统称 + `[debug] len(body)` 日志 + 改用 `_deleted_reason` + reason 细分日志)
- Test: `tests/test_deleted_page.py::TestSaveOneArticleDeletedPath`(追加 4 个集成测试,含 1 个不 mock verify 的端到端测试)

**Interfaces:**
- Consumes: `_deleted_reason` from Task 1
- Produces: `save_one_article` 对屏蔽/违规/删除页短路返回 `("deleted", None)` + reason 日志

- [ ] **Step 1: 写失败测试 — 4 个集成测试**

打开 `tests/test_deleted_page.py`。找到 `TestSaveOneArticleDeletedPath` 类(Task 1 后,该类的 `test_deleted_page_short_circuits` 等用例可能已临时改过)。在类内末尾追加 4 个测试。注意 **`test_verify_precise_exclusion_e2e` 不 mock `handle_verify_page`**,验证 #6 前置排除端到端生效:

```python
    def test_save_blocked_page_short_circuits_with_reason(self, isolated_vault, capsys):
        """屏蔽页 → ('deleted', None) + 日志含「账号被屏蔽」+ verify 被调用一次（顺序锁）"""
        vault, clip_dir = isolated_vault
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "已屏蔽", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=False) as mock_verify, \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "微信公众平台", "text": "此账号已被屏蔽，内容无法查看"}), \
             patch("ima_obsidian_saver.activate_browser") as mock_activate, \
             patch("ima_obsidian_saver.trigger_quick_clip") as mock_clip, \
             patch("ima_obsidian_saver.find_and_rename_in_vault") as mock_rename, \
             patch("ima_obsidian_saver.close_tab") as mock_close, \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        assert result == ("deleted", None)
        mock_clip.assert_not_called()
        mock_rename.assert_not_called()
        mock_activate.assert_not_called()
        mock_close.assert_called_once()
        mock_verify.assert_called_once()  # 锁死「verify 必先调用」顺序不变量
        captured = capsys.readouterr()
        assert "🗑️  账号被屏蔽，标记" in captured.out  # 全句匹配，防自取证日志误中

    def test_save_violation_page_short_circuits_with_reason(self, isolated_vault, capsys):
        """违规页（新文案）→ 日志含「违规不可查看」"""
        vault, clip_dir = isolated_vault
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "违规", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=False), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "微信公众平台", "text": "此内容因违规无法查看"}), \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault"), \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        assert result == ("deleted", None)
        captured = capsys.readouterr()
        assert "🗑️  违规不可查看，标记" in captured.out

    def test_save_publisher_deleted_reason_in_stdout(self, isolated_vault, capsys):
        """发布者删除页 → 日志含「发布者删除」（回归保护 reason 文案）"""
        vault, clip_dir = isolated_vault
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "已删", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=False), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "微信", "text": "该内容已被发布者删除"}), \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault"), \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        assert result == ("deleted", None)
        captured = capsys.readouterr()
        assert "🗑️  发布者删除，标记" in captured.out

    def test_verify_precise_exclusion_e2e(self, isolated_vault):
        """端到端：不 mock handle_verify_page，验证 is_verify_page 前置排除真生效

        mock read_page_snapshot 返回屏蔽页 + mock click_confirm。
        若 is_verify_page 前置排除被破坏（删除 _deleted_reason 调用），屏蔽页会被
        误判为验证页 → handle_verify_page 调 click_confirm → call_count > 0 → 测试失败。
        mock verify 的集成测试（上面三个）无法发现此回归，故需此端到端用例。
        """
        vault, clip_dir = isolated_vault
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "屏蔽", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "微信公众平台", "text": "此账号已被屏蔽，内容无法查看"}), \
             patch("ima_obsidian_saver.click_confirm") as mock_click, \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault"), \
             patch("ima_obsidian_saver.time.sleep"):
            # 不 mock handle_verify_page —— 让它真跑，验证前置排除让 click_confirm 不被调
            result = saver.save_one_article(article, browser_config)

        assert result == ("deleted", None)
        assert mock_click.call_count == 0  # 屏蔽页不应触发 verify 重试
```

> **注意 fixture**:`isolated_vault` 已在 `TestSaveOneArticleDeletedPath` 类定义(line 60-68)。新增用例加 `capsys` 参数(pytest 内置 fixture,无需定义)。`test_verify_precise_exclusion_e2e` 不需 `capsys`。

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_deleted_page.py::TestSaveOneArticleDeletedPath::test_save_blocked_page_short_circuits_with_reason -v`
Expected: FAIL(当前调用点仍用旧文案"文章已被发布者删除",capsys 断言"账号被屏蔽"不命中)

- [ ] **Step 3: 改 `save_one_article` 调用点**

打开 `ima_obsidian_saver.py`。找到 line 791-803 的 `# 2.5` 到 `return "deleted", None` 段。**整段替换**(维持 v1 顺序 `handle_verify_page` 在前;注释更新三类统称;加 `[debug] len(body)` 日志;改用 `_deleted_reason`;reason 细分日志):

```python
    # 2.5 微信验证页检测 + 自动确认（风控验证页会让 quick_clip 打在空页上 → 0 落盘）
    #   is_verify_page 前置 _deleted_reason 排除，屏蔽/违规页不会被误判为验证页 → 不浪费重试
    handle_verify_page(browser_app)

    # 2.55 永久不可恢复页检测（发布者删除 / 违规下架 / 账号屏蔽）：命中即短路返回，不触发 quick_clip
    #   （此类页 quick_clip 只会 0 落盘；保持未保存会被每次运行反复打开 → failed_count 假告警）
    snap = read_page_snapshot(browser_app)
    print(f"    [debug] len(body)={len((snap or {}).get('text') or '')}")  # 自取证：监控真实 innerText 长度（渐进验证 <60 字阈值）
    reason = _deleted_reason(snap)
    if reason is not None:
        print(f"    🗑️  {reason}，标记 status='deleted' 永久跳过")
        print(f"       [自取证] title={(snap or {}).get('title')!r} "
              f"text={((snap or {}).get('text') or '')[:120]!r}")
        close_tab(browser_app)
        time.sleep(WAIT_CLOSE_TAB)
        return "deleted", None
```

- [ ] **Step 4: 跑测试确认通过(4 个新测试 + 现有回归)**

Run: `python3 -m pytest tests/test_deleted_page.py -v`
Expected: PASS(`TestDeletedReason` 10 个 + `TestSaveOneArticleDeletedPath` 全部含新 4 个)

- [ ] **Step 5: commit**

```bash
git add ima_obsidian_saver.py tests/test_deleted_page.py
git commit -m "feat(saver): save_one_article 调用点改用 _deleted_reason + reason 细分日志

- 维持 v1 顺序（handle_verify_page 在前，承载验证后转删除链路）
- 注释更新三类统称（发布者删除/违规下架/账号屏蔽）
- 加 [debug] len(body) 自取证日志（渐进验证 <60 字阈值）
- 改用 _deleted_reason + reason 细分日志
- 新增 4 个集成测试（含 1 个不 mock verify 的端到端测试锁前置排除）

spec §3.4,§4.4"
```

---

## Task 5: docstring + 文案更新

**Files:**
- Modify: `ima_obsidian_saver.py:254-262`(`mark_deleted` docstring)
- Modify: `ima_obsidian_saver.py:299-300`(`get_stats` 注释)
- Modify: `ima_obsidian_saver.py:751`(`save_one_article` docstring `'deleted'` 行)
- Modify: `ima_obsidian_saver.py:954`(主循环行内注释)

**Interfaces:** 无(纯文档,不改逻辑)

- [ ] **Step 1: 更新 `mark_deleted` docstring(line 254-262)**

```python
def mark_deleted(article_id: int):
    """把文章标记为「永久不可恢复」：status 改为 'deleted'，永久跳出待保存队列。

    涵盖三类页面（行为一致，DB 不区分）：
      - 发布者删除（该内容已被发布者删除）
      - 违规下架（此内容因违规已删除 / 此内容因违规无法查看）
      - 账号屏蔽（此账号已被屏蔽，内容无法查看）

    与 mark_saved 不同——永久不可恢复是终态，不写 obsidian_saved（保持其 0/NULL 语义
    即「从未成功保存过」），仅改 status。所有待保存查询（get_unsaved_articles /
    get_stats / find_and_rename_in_vault / ima_incremental_update.py）都用
    WHERE status='success'，故 status='deleted' 自动从这些查询消失，无需改任何 WHERE。
    不计 failed_count，避免 0 落盘的删除页触发上游告警。
    """
```

> **关键修正**:原 docstring 引用 `reclaim_clippings / incremental_update`——前者是 `find_and_rename_in_vault` 旧称,后者是独立脚本非函数。已修正。

- [ ] **Step 2: 更新 `get_stats` 注释(line 299-300)**

```python
        # deleted：status='deleted'（永久不可恢复，含发布者删除/违规/屏蔽）。与 total 同
        # url/kb 口径，但 status 维度独立——不计入 total/unsaved，单独展示有多少文章永久不可恢复。
```

- [ ] **Step 3: 更新 `save_one_article` docstring(line 751)**

```python
    - 'deleted'：永久不可恢复页（发布者删除/违规下架/账号屏蔽），date_str=None（调用方调 mark_deleted）
```

- [ ] **Step 4: 更新主循环行内注释(line 954)**

```python
                # 永久不可恢复页（发布者删除/违规/屏蔽）：永久跳过，不计 failed（避免触发上游告警）
```

- [ ] **Step 5: 跑全测试确认无回归**

Run: `python3 -m pytest tests/ -v`
Expected: PASS(全测试通过;docstring 改动不影响逻辑)

- [ ] **Step 6: commit**

```bash
git add ima_obsidian_saver.py
git commit -m "docs(saver): mark_deleted/get_stats/save_one_article/主循环 文案统一三类统称

- mark_deleted docstring 扩三类 + 修正函数引用（reclaim_clippings→find_and_rename_in_vault，
  incremental_update 标注为脚本）
- get_stats/save_one_article docstring/主循环注释统一「永久不可恢复」语义

spec §3.5"
```

---

## Self-Review

### Spec coverage

| spec 章节 | 覆盖 task |
|---|---|
| §3.1 词表常量(`_DELETED_REASON_MAP` + `DELETED_CLIPPING_MARKERS`) | Task 1(MAP)+ Task 3(CLIPPING) |
| §3.2 `_deleted_reason`(单源,只查 body) | Task 1 |
| §3.3 `is_verify_page` 前置排除 | Task 2 |
| §3.4 `save_one_article` 调用点 | Task 4 |
| §3.5 `mark_deleted` docstring + 文案更新 | Task 5 |
| §4.1 `_deleted_reason` 单元测试 | Task 1 Step 1 |
| §4.2 `is_verify_page` 前置排除测试 | Task 2 Step 1 |
| §4.3 `_is_verify_clipping` 路径测试 | Task 3 Step 1 |
| §4.4 集成测试(含端到端) | Task 4 Step 1 |

无遗漏。

### Placeholder scan

无 TBD/TODO。所有代码块完整。filler 文本已指定「填充文本。」。

### Type consistency

- `_deleted_reason(snapshot: Optional[dict]) -> Optional[str]` — Task 1 定义,Task 2/4 消费 ✓
- `_DELETED_REASON_MAP: Tuple[Tuple[str, str], ...]` — Task 1 定义,Task 2 间接消费(via `_deleted_reason`) ✓
- `is_verify_page(snapshot: Optional[dict]) -> bool` — Task 2 改,Task 4 端到端测试间接调 ✓
- `DELETED_CLIPPING_MARKERS: Tuple[str, ...]` — Task 3 改 ✓

类型一致。
