# v7 Design Spec: 微信违规页长度阈值调整 + 漏检自取证（v6 修订）

> **日期**：2026-07-26（v7，基于 v6 code-review 11 条修订）
> **基线**：v6 spec（`Plans/2026-07-26-weixin-blocked-violation-detection-design-v6.md`）
> **范围**：A 应急止血（B/C 进入 v8）
> **状态**：设计中，待 review

## 0. v7 修订说明（基于 v6 code-review 11 条）

v6 code-review 11 条全部接受。核心变更：

- **#2/#8 设计变更**：`_log_possible_miss` 从 `_deleted_reason` **内部**移到 `save_one_article` **调用点**。`_deleted_reason` 恢复纯函数（无 print 副作用）；单次调用内只打 1 次（解决 v6 单次 2× 重复）。
- **#1/#3 承认噪声**：v6 §2 决策 2「合法文章不含关键词→不打」是事实性错误。子串匹配无法区分「合法引用整句」vs「真实漏检」。讨论审查的媒体文章（如现有测试 `test_long_article_with_phrase_not_deleted`，body=162 字含「该内容已被发布者删除」）会触发噪声——已知 tradeoff，靠节流（#6）+ 接受。
- **#5 截断调整**：`body[:100]` → `body[:200]`（chrome 行通常在文末，截 100 会丢证据）。
- **#6 节流机制**：加 `_POSSIBLE_MISS_SEEN` 集合（同一 body 内容只打一次），与 `[debug] len(body)` 的 `_DEBUG_BODY_LEN_SEEN` 相仿。
- **#4/#11 文档同步**：`is_verify_page` docstring `<60`→`<100`；§2 加决策「提阈改变 is_verify_page 行为范围（60-99 区间）」。
- **#7/#9/#10 测试明确化**：集成测试明确 body 内容 + 打印次数（1 次）；边界测试明确日志预期；测试名修正。

### v7 review 8 条修订（v7.1）

v7 spec 经 10 角度 review 发现 8 条问题，全部接受：

- **P0 #1**：§3.3 snippet 简化了现有 `[debug]` 代码（丢了默认 `"1"` 参数 + falsy 判定 + `_DEBUG_BODY_LEN_SEEN` 节流）→ 完整呈现现有代码，只追加漏检分支
- **P0 #2**：节流「避免跨 launchd run 重复打印」是虚假承诺（module-level set 跨进程重置）→ 改声明为「单次 run 内节流；跨 run 重复反而对运维有利（不漏报）」
- **P1 #3**：`Set[str]` 类型注解错误（`hash()` 返回 int）→ `Set[int]`
- **P1 #4**：§3.3 文字「elif」与 snippet「if」不一致 + `# TODO` 锚点遗漏 → 文字改 if；snippet 保留锚点
- **P2 #5**：§4 测试策略漏 `_POSSIBLE_MISS_SEEN.clear()` fixture（module-level 状态串扰）→ 加 fixture 要求
- **P2 #6**：节流顺序——无关键词 body 不记录 → 每次重扫 → body_hash 记录移到关键词扫描之前
- **P3 #7**：「设计对称」措辞不准（key 不同：len vs hash）→ 「设计相仿」
- **P3 #8**：「v6 污染测试」表述夸大（测试不断言 capsys，不会失败）→ 「v6 pytest stdout 冒出噪声行（CI 检查 stdout 会受影响）」

### PR #6 review v3 修订（v7.2，commit `cf3a544` 之后）

PR #6 在合并前 review v3 发现 5 条问题（合并 P0 #1 + P1 #2 + P2 #3 + P3 #4 #5），全部接受。**核心洞察**：两类日志用途相反——`[debug] len(body)=N` 高频低值（应门控），`[疑似漏检自取证]` 低频高值（不应门控）。

- **P0 #1 + P1 #2**：`_log_possible_miss` 调用被绑到 `IMA_DEBUG_BODY_LEN` 门控内 → 运维设 `IMA_DEBUG_BODY_LEN=0` 降噪时漏检诊断证据源全失。修复：调用移出门控，始终执行；env 只门控 `[debug]` 块。**测试 `test_save_one_article_long_body_with_keyword_logs_miss` 在 `IMA_DEBUG_BODY_LEN=0` 下必须通过**（reviewer 实测复现 bug）。
- **P2 #3**：v7 spec §3.2 原写「`_POSSIBLE_MISS_SEEN.add(body_hash)` 移到关键词扫描**前**」（v7 review #6 决策）与代码 add 顺序矛盾（实际是扫描**后**）。本 spec §3.2 已回写：add 在扫描后，仅含关键词才记录，集合语义清晰。
- **P3 #4**：`_log_possible_miss` 日志只含 `len/hits/body[:200]`，缺 url/title → 运维无法定位漏检文章。修复：函数签名加 `url`、`title` 参数（默认 None 兼容旧调用），日志含 `url=... title=...`。
- **P3 #5**：`_POSSIBLE_MISS_SEEN: Set[int]` 用 `hash(body)` 做 key 有理论碰撞风险。修复：类型改 `Set[str]`，直接存 body 全文。内存代价可控（只有含关键词的 body 才记录，实测每 run < 10 条）。**v7 review #3「`Set[str]` 是类型错误」结论被推翻**——PR #6 review v3 改回 `Set[str]` 是有意为之，不再用 `hash()`。

## 1. 背景（沿用 v6）

PR #5（v5 实施）2026-07-26 11:50 首次生产运行，`[debug] len(body)=N` 日志给出真实违规页长度分布：**30 / 49 / 55 / 59 / 65**。65 字违规页超 v5 阈值 60 → 漏检 → 反复打开 3 次。证据存档于 MEMORY `ima-saver-deleted-reason-len-threshold-realdata`。

v7 应急止血：阈值 `60 → 100` + 漏检自取证（移到调用点 + 节流）。

## 2. 核心设计决策

1. **阈值 `60 → 100`**
   - 实测违规页最大 65 字，100 留 +35 余量
   - 实测合法文章最小 496 字，100 留 ~5 倍安全边际
   - **提隐含行为变更（#11）**：v5 时 body=80+关键词 → `_deleted_reason` 返回 None → `is_verify_page` 继续 check VERIFY_KEYWORDS；v7 后同样输入 → `_deleted_reason` 返回 reason → `is_verify_page` 前置排除 return False。此变化有利（60-99 区间的删除页不再进 verify 浪费重试），但 `is_verify_page` 的前置排除范围从「body<60」扩到「body<100」。

2. **未命中自取证（v7 修正：移到调用点 + 承认噪声）**
   - 触发位置：`save_one_article` 调用点（**不在 `_deleted_reason` 内部**——v6 的设计破坏纯度 + 单次调用打 2 次）
   - 触发条件：`_deleted_reason` 返回 None **且** `len(body) >= 100` **且** body 含 `_DELETED_REASON_MAP` 关键词
   - **不承诺「精准触发排除合法文章」**（v6 错误承诺）：子串匹配无法区分合法引用。讨论审查的媒体文章会触发噪声——已知 tradeoff，靠节流（决策 4）+ 接受
   - 输出格式：`[疑似漏检自取证] len(body)=N 命中关键词=[...] body[:200]='...'`

3. **`_deleted_reason` 恢复纯函数（v7 关键修复）**
   - v6 在 `_deleted_reason` 内部调 `_log_possible_miss` → 破坏纯度（#8）+ 单次调用内打 2 次（#2，因 `is_verify_page` 和 `save_one_article` 都调 `_deleted_reason`）
   - v7：`_deleted_reason` 只做判定（阈值 + 关键词 → reason/None），无 print。自取证移到调用点（决策 2）

4. **节流机制（#6）**
   - `_POSSIBLE_MISS_SEEN: Set[str]` 模块级集合，key=body 全文（PR #6 review v3 #5：从 `Set[int]`/`hash(body)` 改为 `Set[str]`/body 全文，消除理论碰撞；推翻原 v7 review #3「`Set[str]` 是类型错误」结论）
   - **单次 run 内节流**：同一 body 内容只打一次（如同一 URL 重试时去重）
   - **跨 run 重复**（v7 review #2）：module-level set 仅存活于单个 Python 进程，launchd 每次跑都启动新进程 → 跨 run 会重复打印。**这反而对运维有利**（不会漏报漏检 URL；若跨 run 也节流，漏检 URL 第一次报警后就被静默，运维可能错过）
   - 与 `[debug] len(body)=N` 的 `_DEBUG_BODY_LEN_SEEN` 设计**相仿**（v7 review #7：都是 module-level set 节流，但 key 不同——前者按 len，后者按 body 全文）

5. **范围严格限定（沿用 v6）**
   - 不动词表（`_DELETED_REASON_MAP` / `DELETED_CLIPPING_MARKERS`）
   - 不动 `is_verify_page` 前置排除逻辑（但 docstring 阈值同步 #4）
   - 不动调用点顺序、title 扫描行为
   - v7 只改 `_deleted_reason`（恢复纯函数）+ 新增 `_log_possible_miss`（body[:200]）+ 调用点加漏检分支 + `_POSSIBLE_MISS_SEEN` 节流

## 3. 实施

### 3.1 `_deleted_reason` 恢复纯函数（`ima_obsidian_saver.py` line 495-512）

```python
def _deleted_reason(snapshot: Optional[dict]) -> Optional[str]:
    """永久不可恢复页判定 + reason 映射（单源实现，纯函数）。

    返回 None ⇔ 非删除页（含普通文章、验证页、空快照）；返回 reason 字符串 ⇔ 是
    永久不可恢复页（发布者删除 / 违规不可查看 / 账号屏蔽）。

    判定：len(body) < 100 阈值 + _DELETED_REASON_MAP 关键词子串匹配（k in body，非正则）。
    只查 body（snapshot['text']），不并 title——删除页 title 恒为「微信公众平台」不含
    关键词，并 title 无益；而合法文章 title 可能含「此账号已被屏蔽」等名词性短语
    （如「评此账号已被屏蔽现象」），并 title 会在慢加载 body='' 时误杀合法文章。

    阈值是防误判的关键——合法讨论审查的文章前 800 字正文 ≫ 100，靠阈值防 mark_deleted
    永久跳过导致不可逆数据丢失。不得简化成纯关键词匹配（丢阈值 = 误杀合法文章）。

    v7：本函数是纯函数（无副作用）。漏检自取证由调用点（save_one_article）负责，
    避免 is_verify_page + save_one_article 双调用时重复打印（v6 #2/#8 修复）。
    """
    if not snapshot:
        return None
    body = snapshot.get("text") or ""    # 只查 body，不并 title（防标题误杀）
    if len(body) >= 100:
        return None
    for keyword, reason in _DELETED_REASON_MAP:    # 顺序敏感：首条命中决定 reason
        if keyword in body:                         # 子串匹配（非正则）
            return reason
    return None
```

**关键变更（相对 v6）**：
- 删除 v6 末尾的 `_log_possible_miss(body)` 调用——恢复纯函数
- docstring 加「v7：本函数是纯函数（无副作用）」说明

### 3.2 新增 `_log_possible_miss` 辅助函数（紧邻 `_deleted_reason` 之后）

```python
# 已打印过疑似漏检自取证的 body 集合：相同 body 只打印一次（单次 run 内去重；
# 跨 launchd run 因进程重启不持久化——这是设计选择：跨 run 重复报警反而对运维友好，
# 不漏报漏检 URL）。
# key 用 body 全文（Set[str]）而非 hash(body)（Set[int]）——消除理论碰撞风险
# （PR #6 review v3 #5）。内存代价可控：只有"含 DELETED 关键词的 body"才被记录
# （合法文章不计），实测每 run < 10 条。
_POSSIBLE_MISS_SEEN: Set[str] = set()


def _log_possible_miss(body: str, url: Optional[str] = None, title: Optional[str] = None) -> None:
    """body >= 阈值但含 DELETED 关键词时打自取证诊断（v7 §3.2）。

    v7 修正（v6 #1/#3/#5/#6）：
    - 不承诺「精准触发排除合法文章」——子串匹配无法区分合法引用整句 vs 真实漏检。
      讨论审查的媒体文章会触发噪声，已知 tradeoff（靠节流 + 接受）。
    - body[:200] 截断（v6 的 [:100] 会丢文末 chrome 行，与「为 v8 收集 chrome 证据」矛盾）。
    - _POSSIBLE_MISS_SEEN 节流：同一 body 内容只打一次（避免单次 run 内重复打印）。

    PR #6 review v3 回写：
    - 节流 key 用 body 全文（Set[str]），不再用 hash(body)（Set[int]）——消除理论碰撞
      （#5）。`_POSSIBLE_MISS_SEEN.add(body)` 在关键词扫描**后**（仅含关键词才记录）——
      集合语义清晰（只含可疑漏检 body），CPU 影响可忽略；本决策保留，spec 与代码对齐（#3）。
    - 签名加 url/title 参数（默认 None 兼容旧调用），日志含定位信息（#4）。
    - 调用点不受 IMA_DEBUG_BODY_LEN 门控——见 §3.3（#1 #2）。
    """
    hits = [k for k, _ in _DELETED_REASON_MAP if k in body]
    if not hits:
        return
    is_new = body not in _POSSIBLE_MISS_SEEN
    _POSSIBLE_MISS_SEEN.add(body)  # 扫描后 add（仅含关键词才记录）
    if not is_new:
        return
    url_repr = repr(url) if url else "None"
    title_repr = repr(title)[:80] if title else "None"
    print(f"    [疑似漏检自取证] url={url_repr} title={title_repr} "
          f"len(body)={len(body)} 命中关键词={hits} body[:200]={body[:200]!r}")
```

### 3.3 `save_one_article` 调用点加漏检分支（line 853-870 附近）

**关键（v7 review #1）**：现有 `[debug] len(body)` 有完整的环境变量判定（默认 `"1"` 开启 + falsy 列表关闭）+ `_DEBUG_BODY_LEN_SEEN` 节流。v7 **不简化**这部分，只改阈值注释 + 追加漏检分支：

```python
    snap = read_page_snapshot(browser_app)
    # 渐进验证：<100 字阈值对真实屏蔽/违规页是否有效（spec §5 风险缓解措施 + v7 §3.1）
    # 默认开启；运维嫌吵可设 IMA_DEBUG_BODY_LEN=0/false/no/off 关闭
    # TODO(渐进验证)：首篇屏蔽/违规 URL 命中后，根据日志确认 len(body) 真实长度；
    #   若 ≥100 字阈值过紧需调整 _deleted_reason；若确认阈值有效，移除此 print 与门控
    if os.environ.get("IMA_DEBUG_BODY_LEN", "1").lower() not in ("0", "false", "no", "off", ""):
        body_len = len((snap or {}).get('text') or '')
        if body_len not in _DEBUG_BODY_LEN_SEEN:
            _DEBUG_BODY_LEN_SEEN.add(body_len)
            print(f"    [debug] len(body)={body_len}")
    reason = _deleted_reason(snap)
    if reason is not None:
        print(f"    🗑️  {reason}，标记 status='deleted' 永久跳过")
        print(f"       [自取证] title={(snap or {}).get('title')!r} "
              f"text={((snap or {}).get('text') or '')[:120]!r}")
        close_tab(browser_app)
        time.sleep(WAIT_CLOSE_TAB)
        return "deleted", None
    # v7：漏检自取证——_deleted_reason 返回 None 但 body>=100，调 _log_possible_miss
    #     （调用点显式 if 分支；不在 _deleted_reason 内部——避免 is_verify_page 双调用时
    #      重复打印，v6 #2/#8 修复）
    # PR #6 review v3 #1 #2：_log_possible_miss 调用移出 IMA_DEBUG_BODY_LEN 门控——
    #     两类日志用途相反（[debug] 高频低值 vs [疑似漏检] 低频高值），不应共享开关。
    body = (snap or {}).get("text") or ""
    if len(body) >= 100:
        _log_possible_miss(body, url=url, title=title)
```

**关键变更（相对 v6）**：
- `[debug]` 块**完全保留**现有实现（只改注释 `<60`→`<100` + `# TODO` 锚点阈值同步），不简化——v7 review #1 修复
- 漏检自取证从 `_deleted_reason` 内部移到调用点（显式 `if len(body) >= 100` 分支，不是 elif——前面 `reason is not None` 已 return）
- 单次 `save_one_article` 内只打 1 次（`_deleted_reason` 被 `is_verify_page` 调用时不打——因 `_deleted_reason` 已无 print 副作用）
- **PR #6 review v3 #1 #2**：`_log_possible_miss` 调用移出 `IMA_DEBUG_BODY_LEN` 门控。两类日志用途相反——`[debug] len(body)=N` 高频低值（运维嫌吵会用 env 关），`[疑似漏检自取证]` 低频高值（运维依赖此证据定位漏检），共享开关会让运维在降噪时丢失漏检诊断。修复后 `_log_possible_miss` 始终调用，env 只门控 `[debug]` 块。

### 3.4 `is_verify_page` docstring 同步（line 523 附近，#4）

```python
def is_verify_page(snapshot: Optional[dict]) -> bool:
    """判断页面快照是否为微信风控验证页。

    前置 _deleted_reason 排除——在 _deleted_reason 判定范围内（body <100 字且含
    _DELETED_REASON_MAP 关键词）的永久不可恢复页不是验证页...（v7：阈值 60→100）
    """
```

仅改 docstring 的 `<60` → `<100`，**不改逻辑**（`is_verify_page` 本身不直接写阈值，它调 `_deleted_reason`，阈值由 `_deleted_reason` 控制）。

### 3.5 调用点注释同步（line 855 附近，沿用 v6）

`<60 字` → `<100 字`；`（spec §5 风险缓解措施）` → `（spec §5 风险缓解措施 + v7 §3.1）`。`# TODO(渐进验证)` 锚点保留。

## 4. 测试策略

### 4.1 `_deleted_reason` 单元测试（`tests/test_deleted_page.py`）

**v7 关键：`_deleted_reason` 恢复纯函数——所有测试 capsys 应无 `[疑似漏检自取证]` 输出**（自证移到调用点）。

更新阈值边界用例（v5 是 60，v7 是 100）：
- `test_threshold_99_hits`：len=99 含关键词 → 命中 reason + capsys 无漏检日志
- `test_threshold_100_returns_none`：len=100 含关键词 → 返回 None + **capsys 无漏检日志**（关键：`_deleted_reason` 是纯函数，不自取证；边界日志预期明确，v6 #9 修复）

新增用例：
- `test_long_body_no_keyword_returns_none`：len=200 不含关键词 → 返回 None + capsys 无任何日志（纯函数）
- `test_long_body_with_keyword_returns_none_no_log`：len=200 含关键词 → 返回 None + **capsys 无漏检日志**（关键：`_deleted_reason` 不打日志，自证在调用点；v6 #1 修复——现有 `test_long_article_with_phrase_not_deleted` 不再被噪声污染）

### 4.2 `_log_possible_miss` 单元测试（同文件）

- `test_log_possible_miss_no_keyword_quiet`：body 不含关键词 → capsys 无输出
- `test_log_possible_miss_with_keyword_format`：含关键词 → 输出含 `len=`/`hits=`/`body[:200]=`
- `test_log_possible_miss_caps_body_at_200`：body 300 字 → 输出只含前 200 字（v6 #5 修复）
- `test_log_possible_miss_throttle_same_body`：同一 body 调 2 次 → 只打 1 次（v7 #6 节流）
- `test_log_possible_miss_different_body_both_print`：不同 body → 各打 1 次

> **fixture 要求（v7 review #5）**：`_POSSIBLE_MISS_SEEN` 是 module-level 可变状态，测试间会串扰（先前测试往 set 加了 hash，后续节流测试的第一次调用被静默跳过 → 断言看到 0 输出）。每个 `_log_possible_miss` 测试前须 `saver._POSSIBLE_MISS_SEEN.clear()`（仿 `tests/test_debug_body_len.py:32` 为 `_DEBUG_BODY_LEN_SEEN` 加的 clear fixture）。

### 4.3 集成测试（`tests/test_deleted_page.py::TestSaveOneArticleDeletedPath`，v6 #7 明确化）

`test_save_one_article_long_body_with_keyword_logs_miss`：
- **body 内容**：len=200，含「该内容已被发布者删除」DELETED 关键词，**不含** VERIFY_KEYWORDS（「当前环境异常」/「完成验证」/「去验证」——避免 is_verify_page 走 True 分支触发 click_confirm）
- **打印次数断言**：`stdout.count("[疑似漏检自取证]") == 1`（v7 只在调用点打 1 次，不是 v6 的 2 次——v6 #2 修复）
- `mark_deleted` 未被调用（返回 `("failed", None)` 因 quick_clip 在长违规页上 0 落盘）

> **注意**：真实长违规页（body>=100）走 quick_clip 会 0 落盘 → `find_and_rename_in_vault` 找不到文件 → 返回 `("failed", None)`。集成测试 mock `find_and_rename_in_vault` 返回 `(False, None)` 模拟此场景。

### 4.4 现有测试影响盘点（v6 #1/#10 修正）

- `test_long_article_with_phrase_not_deleted`（body=162 含「该内容已被发布者删除」）：**v6 会在 pytest stdout 冒出 `[疑似漏检自取证]` 噪声行**（v7 review #8：测试本身只断言 `_deleted_reason(...) is None` 不读 capsys，不会失败；但 stdout 被污染，CI 若检查 stdout 会受影响）。**v7 不再污染**——`_deleted_reason` 恢复纯函数，单元测试不调调用点，不触发 `_log_possible_miss`
- 凡涉及阈值边界的旧用例（`test_threshold_60_*`）需更新为 100
- 命中测试（len=30/49/55/59 实测值，都 <100）不受影响
- 节流测试：`TestDebugBodyLenThrottle` 类（不是 `test_debug_body_len_throttle` 函数，v6 #10 修正）不受影响

## 5. 风险与边界

| 风险 | 缓解 |
|---|---|
| 阈值 100 仍漏检更长违规页（如 120 字） | `[疑似漏检自取证]` 捕获并打日志（节流后只打 1 次），为 v8 提供证据 |
| 阈值 100 误判短合法文章（100-200 字评论） | 实测合法文章最小 496 字，100 留 ~5 倍边际；且必须同时含 DELETED 关键词才 mark_deleted |
| **噪声污染合法讨论审查文章**（v6 #1/#3，v7 承认） | 子串匹配无法区分合法引用 vs 真实漏检。靠 `_POSSIBLE_MISS_SEEN` 节流（同一 body 只打 1 次）+ 接受。现有 `test_long_article_with_phrase_not_deleted` 不再被污染（`_deleted_reason` 纯函数） |
| `_log_possible_miss` 单次调用内打 2 次（v6 #2） | **v7 修复**：自证移到调用点，`_deleted_reason` 纯函数。单次 `save_one_article` 只打 1 次 |
| `_deleted_reason` 加 print 破坏纯度（v6 #8） | **v7 修复**：`_deleted_reason` 恢复纯函数，自证在调用点 |
| `body[:100]` 丢文末 chrome 证据（v6 #5） | **v7 修复**：`body[:200]`，覆盖 chrome 行 |
| 自取证日志 launchd 长期跑累积噪声（v6 #6） | **v7 修复**：`_POSSIBLE_MISS_SEEN` 节流——**单次 run 内**同一 body 只打 1 次；**跨 run 重复**（module-level set 跨进程重置，v7 review #2），反而对运维有利（不漏报漏检 URL） |
| `is_verify_page` 前置排除范围扩大（60→100，v6 #11） | 有利变化（60-99 区间删除页不再进 verify 浪费）；docstring 同步（v6 #4） |
| `[debug] len(body)=N` 与 `[疑似漏检自取证]` 冗余 | 互补：前者所有页打长度（节流按长度），后者仅疑似漏检打内容（节流按 body 哈希） |

## 6. 改动清单

| 文件 | 改动 | 行数 |
|---|---|---|
| `ima_obsidian_saver.py` | `_deleted_reason` 恢复纯函数（删 v6 末尾 `_log_possible_miss` 调用）；新增 `_POSSIBLE_MISS_SEEN` + `_log_possible_miss`（body[:200] + 节流）；`save_one_article` 调用点加漏检分支；`is_verify_page` docstring `<60`→`<100`；line 855 注释 `<60`→`<100` | ~25 行代码 |
| `tests/test_deleted_page.py` | 更新阈值边界用例（60→100）；新增 `_log_possible_miss` 单元测试（5 个）；新增长 body 集成测试（1 个，明确打印次数=1） | ~60 行测试 |

## 7. 验证步骤

1. **单元测试**：`python3 -m pytest tests/test_deleted_page.py -v`
2. **全测试**：`python3 -m pytest tests/` —— 不得新增 failed
3. **等下次 launchd 跑**，预期：
   - 65 字违规页命中（65 < 100）→ `🗑️ 违规不可查看`
   - 若有更长违规页（>100 字）→ `[疑似漏检自取证]` 暴露 body[:200]，为 v8 chrome 剥离提供证据
   - 同一漏检 URL 反复打开只打 1 次（节流）

## 附录：v7 不解决的问题（进入 v8）

- **B 系统修复**：剥 UI chrome 行后算长度（需多个漏检样本判断 chrome 模式，靠 v7 自取证日志采集）
- **C 完整治理**：「去验证」按钮未识别 + v5 P0 #3 title 扫描回归评估
