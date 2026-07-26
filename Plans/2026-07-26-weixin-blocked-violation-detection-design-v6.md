# v6 Design Spec: 微信违规页长度阈值调整 + 漏检自取证

> **日期**：2026-07-26  
> **基线**：v5 spec（commit `c179387`，PR #5 已合并到 main）  
> **范围**：A 应急止血（B/C 进入 v7）  
> **状态**：设计中

---

## 1. 背景

PR #5（commit `c179387`）2026-07-26 11:50 首次生产运行，`[debug] len(body)=N` 日志给出真实违规页长度分布：**30 / 49 / 55 / 59 / 65**（5 种）。其中：

- 30/49/55/59 字违规页 `< 60` 阈值 → 命中 `_deleted_reason` → mark_deleted 永久跳过（共 4 次成功）
- **65 字违规页 `>= 60` 阈值** → 返回 None → 走 quick_clip 0 落盘 → 同一 URL 反复打开 3 次（12:06:51 / 12:07:30 / 12:08:10，每次 ~40s 间隔）

这是 v5 spec §5 风险表「真实屏蔽/违规页 body innerText ≥60 字 → 漏检」的**实测确证**。证据存档于 [[ima-saver-deleted-reason-len-threshold-realdata]] memory。

同时发现**诊断盲区**：`_deleted_reason` 命中时打 `[自取证] title=... text=...`，未命中时只打 `[debug] len(body)=N`（仅长度无内容）——65 字违规页的真实 body 内容至今未知，无法判断是「提示语+UI chrome」还是「长版本提示语」。

**v6 应急止血目标**：
1. 调阈值 `60 → 100`，覆盖实测 65 字 + 留余量
2. 补精准未命中自取证，让未来漏检可定位

**v6 范围外（进入 v7 spec）**：
- B 系统修复：剥 UI chrome 行后算长度（需多个漏检样本判断 chrome 模式）
- C 完整治理：「去验证」按钮未识别（独立问题）+ v5 P0 #3 title 扫描回归评估

---

## 2. 核心设计决策

1. **阈值 `60 → 100`**
   - 实测违规页最大 65 字，100 留 +35 余量（容忍"投诉理由"等 chrome 长度变化）
   - 实测合法文章最小 496 字，100 留 ~5 倍安全边际
   - 与 CLIPPING 路径的 `< 200` 阈值拉开距离（两阈值撞车会让维护者困惑）

2. **未命中精准自取证**
   - 触发条件：`len(body) >= 100 且 body 含 _DELETED_REASON_MAP 关键词`
   - 合法文章不含关键词 → 不打（无污染）
   - 命中页由 reason 分支处理 → 不重复打
   - 只有"含关键词但超阈值"的页打——**正是漏检诊断场景**
   - 输出格式：`[疑似漏检自取证] len(body)=N 命中关键词=[...] body[:100]='...'`

3. **范围严格限定**
   - 不动 v5 的词表（`_DELETED_REASON_MAP` / `DELETED_CLIPPING_MARKERS`）
   - 不动 `is_verify_page` 前置排除逻辑
   - 不动调用点顺序（v5 §3.4 维持 v1 顺序）
   - 不动 title 扫描行为（v5 §3.2 只查 body 不查 title）
   - **v6 只改 `_deleted_reason` 一个函数 + 新增 `_log_possible_miss` 辅助函数**

---

## 3. 实施

### 3.1 `_deleted_reason` 改动（`ima_obsidian_saver.py` line 495-512）

```python
def _deleted_reason(snapshot: Optional[dict]) -> Optional[str]:
    """永久不可恢复页判定 + reason 映射（单源实现）。

    返回 None ⇔ 非删除页（含普通文章、验证页、空快照）；返回 reason 字符串 ⇔ 是
    永久不可恢复页（发布者删除 / 违规下架 / 账号屏蔽）。

    判定：len(body) < 100 阈值 + _DELETED_REASON_MAP 关键词子串匹配（k in body，非正则）。
    只查 body（snapshot['text']），不并 title——删除页 title 恒为「微信公众平台」不含
    关键词，并 title 无益；而合法文章 title 可能含「此账号已被屏蔽」等名词性短语
    （如「评此账号已被屏蔽现象」），并 title 会在慢加载 body='' 时误杀合法文章。

    阈值是防误判的关键——合法讨论审查的文章前 800 字正文 ≫ 100，靠阈值防 mark_deleted
    永久跳过导致不可逆数据丢失。不得简化成纯关键词匹配（丢阈值 = 误杀合法文章）。

    body >= 100 时调 _log_possible_miss——若含 DELETED 关键词则打自取证诊断
    （为 v7 chrome 剥离设计收集证据）。
    """
    if not snapshot:
        return None
    body = snapshot.get("text") or ""    # 只查 body，不并 title（防标题误杀）
    if len(body) < 100:
        for keyword, reason in _DELETED_REASON_MAP:    # 顺序敏感：首条命中决定 reason
            if keyword in body:                         # 子串匹配（非正则）
                return reason
        return None
    # body >= 100：超阈值，调 _log_possible_miss 诊断（含关键词才打日志）
    _log_possible_miss(body)
    return None
```

**关键变更点**（相对 v5 line 495-512）：
- docstring 中 `< 60` → `< 100`
- 阈值检 `if len(body) >= 60: return None` 改为 `if len(body) < 100: ... else: _log_possible_miss(body); return None`
- 末尾增加 `_log_possible_miss(body)` 调用

### 3.2 新增 `_log_possible_miss` 辅助函数（紧邻 `_deleted_reason` 之后）

```python
def _log_possible_miss(body: str) -> None:
    """body >= 阈值但含 DELETED 关键词时打自取证诊断（v6 §3.2）。

    精准触发：合法文章不含 DELETED 关键词 → hits 为空 → 立即返回不打；命中页由
    reason 分支处理不重复打；只有"含关键词但超阈值"的页打——正是漏检诊断场景。
    为下一轮 v7 chrome 剥离设计收集证据。
    """
    hits = [k for k, _ in _DELETED_REASON_MAP if k in body]
    if not hits:
        return
    print(f"    [疑似漏检自取证] len(body)={len(body)} 命中关键词={hits} body[:100]={body[:100]!r}")
```

**设计要点**：
- 辅助函数内部再判 `if not hits: return`——无关键词时立即返回不打
- 避免合法文章的双关键词扫描代价：只有 `body >= 100` 才进入辅助函数，辅助函数内部二次扫关键词
- body 截断到前 100 字（`body[:100]`）防日志爆炸；同时 `repr()` 转义换行符便于日志解析

### 3.3 调用点注释同步（line 855 附近）

```python
    # 渐进验证：<100 字阈值对真实屏蔽/违规页是否有效（spec §5 风险缓解措施 + v6 §3.1）
    # 默认开启；运维嫌吵可设 IMA_DEBUG_BODY_LEN=0/false/no/off 关闭
    # TODO(渐进验证)：首篇屏蔽/违规 URL 命中后，根据日志确认 len(body) 真实长度；
    #   若 ≥100 字阈值过紧需调整 _deleted_reason；若确认阈值有效，移除此 print 与门控
```

变更：
- `<60 字` → `<100 字`
- `≥60 字` → `≥100 字`
- `（spec §5 风险缓解措施）` → `（spec §5 风险缓解措施 + v6 §3.1）`

**`# TODO(渐进验证)` 锚点保留**——v6 调阈值不消除 TODO，等 v7 chrome 剥离后才考虑移除。

---

## 4. 测试策略

### 4.1 `_deleted_reason` 单元测试（`tests/test_deleted_page.py` 追加）

**更新现有阈值边界用例**（v5 是 60，v6 是 100）：
- `test_threshold_99_hits`：len=99 含关键词 → 命中（边界）
- `test_threshold_100_miss`：len=100 含关键词 → 返回 None（边界）

**新增用例**：
- `test_long_body_no_keyword_no_log`：len=200 不含关键词 → 返回 None + **不打自取证**（验证合法文章无噪声）
- `test_long_body_with_keyword_logs`：len=200 含关键词 → 返回 None + 打 `[疑似漏检自取证]`
- `test_long_body_multiple_keywords`：len=200 含多个关键词 → 自取证列出所有命中

### 4.2 `_log_possible_miss` 单元测试（同文件）

- `test_log_possible_miss_no_keyword_quiet`：body 不含关键词 → capsys 无输出
- `test_log_possible_miss_with_keyword_format`：含关键词 → 输出含 `len=`/`hits=`/`body[:100]=`
- `test_log_possible_miss_caps_body_at_100`：body 200 字 → 输出只含前 100 字

### 4.3 集成测试（`tests/test_deleted_page.py::TestSaveOneArticleDeletedPath` 追加）

- 现有命中集成测试（55 字违规页）应仍通过（55 < 100，无变化）
- 新增：`test_save_one_article_long_body_with_keyword_logs_miss` —— len=200 含关键词的 save_one_article，断言 stdout 含 `[疑似漏检自取证]` 且 `mark_deleted` 未被调用

### 4.4 现有测试影响盘点

- v5 的 194 个测试中，凡涉及阈值边界的（如旧 `test_threshold_60_*`）需更新为 100
- 命中测试（len=30/49/55/59 实测值，都 < 100）不受影响
- 节流测试（`test_debug_body_len_throttle`）不受影响（v6 不动节流机制）

---

## 5. 风险与边界

| 风险 | 缓解 |
|---|---|
| 阈值 100 仍漏检更长违规页（如未来 120 字） | `[疑似漏检自取证]` 会捕获并打日志，下次迭代可见，为 v7 提供证据 |
| 阈值 100 误判短合法文章（100-200 字评论/摘要） | 实测合法文章最小 496 字，100 留 ~5 倍边际；且必须同时含 DELETED 关键词才 mark_deleted（关键词是强信号） |
| `_log_possible_miss` 双关键词扫描代价 | 仅对 `body >= 100` 触发，合法文章（800 字）多扫一次子串是 ms 级，可忽略 |
| 自取证日志 launchd 长期跑污染 | 精准触发排除合法文章；同一漏检页反复打开会反复打，但 saver 会因 0 落盘告警（退出码 1），运维会处理 |
| v5 的 `[debug] len(body)=N` 日志冗余 | v6 **不删除**——仍是 len 分布采集源；与 `[疑似漏检自取证]` 互补（前者所有页都打长度，后者仅疑似漏检打内容） |
| `_log_possible_miss` 与命中自取证日志风格不一致 | 命中用 `🗑️ {reason} ...` + `[自取证] title=... text=...`；疑似漏检用 `[疑似漏检自取证]`——前缀区分，运维 grep 易分辨 |

---

## 6. 改动清单

| 文件 | 改动 | 行数 |
|---|---|---|
| `ima_obsidian_saver.py` | `_deleted_reason` 阈值 60→100 + 末尾调 `_log_possible_miss`；新增 `_log_possible_miss` 辅助函数；line 855 注释 `<60` → `<100` | ~15 行代码 |
| `tests/test_deleted_page.py` | 更新阈值边界用例（60→100）；新增 `_log_possible_miss` 单元测试（3 个）；新增长 body 集成测试（1 个） | ~50 行测试 |

**总改动**：~15 行代码 + ~50 行测试，单文件生产改动 + 单文件测试改动。

---

## 7. 验证步骤

1. **单元测试**：`pytest tests/test_deleted_page.py -v` —— 新增/更新用例全通过
2. **全测试**：`pytest tests/` —— 194+ 测试全通过（不得新增 failed）
3. **等下次 11:50 launchd 跑**，预期：
   - 65 字违规页**会被命中**（65 < 100），日志显示 `🗑️ 违规不可查看`（不再漏检）
   - 若有更长违规页（>100 字），`[疑似漏检自取证]` 会暴露其 body 内容，为 v7 chrome 剥离设计提供证据

---

## 附录 A：v5 基线引用

- **基线 commit**：`c179387`（PR #5 rebase merge 到 main）
- **v5 spec 文件**：`Plans/2026-07-25-weixin-blocked-violation-detection-design.md`
- **v5 关键章节**：
  - §3.1 词表常量（v6 不动）
  - §3.2 `_deleted_reason`（v6 修改阈值）
  - §3.3 `is_verify_page` 前置排除（v6 不动）
  - §3.4 调用点顺序（v6 不动）
  - §5 风险表（v6 修复第 2 行「真实屏蔽/违规页 body ≥60 字漏检」）

## 附录 B：v6 不解决的问题（进入 v7）

- **B 系统修复**：剥 UI chrome 行后算长度（如过滤 ≤4 字短行"投诉"/"返回首页"），让阈值对 chrome 变化完全鲁棒。需要先采集多个不同长度的漏检样本（通过 v6 自取证日志）判断 chrome 模式。
- **C 完整治理**：
  - 「去验证」按钮未识别（2026-07-26 实测 12:00:55 / 12:01:39，`click_confirm` 含「去验证」关键词但仍报"未找到确认按钮"——元素定位问题）
  - v5 P0 #3 title 扫描回归（实测未触发，但理论上慢加载 + title 含关键词的合法文章会误杀）
