# 微信「账号屏蔽 / 违规不可查看」异常页检测设计

> 日期：2026-07-26（最终版，经 5 轮 review 迭代定稿）
> 状态：定稿，待 writing-plans
> 关联：[snoopy-pondering-biscuit.md](./snoopy-pondering-biscuit.md)（验证页检测起源）、MEMORY `ima-saver-weixin-verify-page`

## 1. 背景

`ima_obsidian_saver.py` 已对两类微信异常页做了拦截：

| 类别 | 语义 | 提示词 | 处理 |
|---|---|---|---|
| 可恢复（风控验证页） | 间歇触发，点「去验证」即可继续 | `当前环境异常` / `完成验证` 等 | `handle_verify_page` 自动点确认 |
| 永久不可恢复 | 文章已不存在 | `该内容已被发布者删除` / `此内容因违规已删除` | `mark_deleted` → `status='deleted'` 永久跳过 |

Chrome 中打开微信公众号文章时，除上述两种外，还会出现：

- **「此账号已被屏蔽，内容无法查看」** — 账号被微信平台屏蔽（永久）
- **「此内容因违规无法查看」** — 内容被平台下架（永久）

二者均为**永久不可恢复**状态，与现有删除页同类。当前未拦截 → 反复打开 → `quick_clip` 0 落盘 → `failed_count++` → 触发上游 `incremental_update` / `launchd` 假告警。

## 2. 核心设计决策

1. **方案 A**：扩删除页关键词集，复用现有 `mark_deleted` 路径。DB `status` 仍统一 `'deleted'`，stats 文案不变。日志细分三类 reason（发布者删除 / 违规不可查看 / 账号被屏蔽）。
2. **`_deleted_reason` 单源判定**：判定逻辑（阈值 + 关键词 + reason 映射）集中到 `_deleted_reason`，删除原 `is_deleted_page` 和 `DELETED_KEYWORDS`（避免双源漂移 / 死代码）。所有判定走 `_deleted_reason`。
3. **keyword 只查 body，不查 title**：`_deleted_reason` 只扫 `snapshot['text']`（body innerText），不并 title。原 2 条关键词是整句 unlikely 作标题，并 title 无害；新增的「此账号已被屏蔽」是名词性短语，合法文章标题可能含此短语（如「评此账号已被屏蔽现象」），并 title 会在慢加载（body=''）时误杀合法文章 → mark_deleted 永久跳过 = 不可逆数据丢失。
4. **`is_verify_page` 前置排除**：`is_verify_page` 首行加 `if _deleted_reason(snapshot) is not None: return False`。屏蔽/违规/删除页（body 含 DELETED 关键词）直接返回 False → `handle_verify_page` 第一次循环退出 → 消除 verify 重试浪费（最坏 ~12-14s/URL）。验证页（body 不含 DELETED 关键词）→ `_deleted_reason` 返回 None → 原逻辑不变 → **「验证后转删除」链路保留**。
5. **维持 v1 调用顺序**：`handle_verify_page`（在前）→ `read_page_snapshot` → `_deleted_reason`（在后）。承载「验证后转删除」时序。不交换顺序——曾有 review 建议交换以省 verify 浪费，但破坏链路；#4 的前置排除已消除浪费，无需交换。

## 3. 实施

所有改动在 `ima_obsidian_saver.py`。`ima_incremental_update.py` 经 subprocess 调 saver，接口未变，不动。

### 3.1 词表常量

**删除 `DELETED_KEYWORDS`（line 479-482），改为 `_DELETED_REASON_MAP`（唯一源）**。放在 `is_verify_page`（line 464）**之前**——`is_verify_page` 和 `_deleted_reason` 都引用它，前向定义避免阅读时跳转：

```python
# 三类永久不可恢复页（行为一致：mark_deleted 永久跳过，不计 failed）：
#   发布者删除 / 平台下架违规内容 / 账号被平台屏蔽
# 顺序敏感：首条命中决定 reason（近义关键词放一起，如两条违规文案映射同一 reason）。
# 修改本表会影响：_deleted_reason（判定源）、is_verify_page（前置排除）、
#   is_deleted_page（已删，但若有未来 wrapper 同理）——改词表时同步审视这些调用方。
# 匹配语义：全部子串匹配（k in text，非正则）；每条标注 prefix/sentence 见行末注释。
_DELETED_REASON_MAP = (
    # 前 3 条 sentence（整句本身就是强信号，极不可能出现在合法短文本；前缀化收益小）
    ("该内容已被发布者删除",   "发布者删除"),        # sentence
    ("此内容因违规已删除",     "违规不可查看"),      # sentence（旧文案）
    ("此内容因违规无法查看",   "违规不可查看"),      # sentence（新文案）
    # 第 4 条 prefix（「内容无法查看」是通用后缀，前缀对文案微调鲁棒）
    ("此账号已被屏蔽",         "账号被屏蔽"),        # prefix
)
```

**`DELETED_CLIPPING_MARKERS`（line 581）全用整句**，与 `_DELETED_REASON_MAP` 分离（两场景权衡不同：CLIPPING 误判代价 = 永久跳过合法文章 > 漏判代价，故更保守用整句）：

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

### 3.2 `_deleted_reason`（单源判定，只查 body）

放在 `_DELETED_REASON_MAP` 之后、`is_verify_page` 之前：

```python
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

### 3.3 `is_verify_page` 前置排除（line 464-476）

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

### 3.4 `save_one_article` 调用点（line 791-803，维持 v1 顺序）

**注释更新为三类统称**；调用点改用 `_deleted_reason`；日志含 reason：

```python
# 2.5 微信验证页检测 + 自动确认（风控验证页会让 quick_clip 打在空页上 → 0 落盘）
#   is_verify_page 前置 _deleted_reason 排除，屏蔽/违规页不会被误判为验证页 → 不浪费重试
handle_verify_page(browser_app)

# 2.55 永久不可恢复页检测（发布者删除 / 违规下架 / 账号屏蔽）：命中即短路返回，不触发 quick_clip
#   （此类页 quick_clip 只会 0 落盘；保持未保存会被每次运行反复打开 → failed_count 假告警）
snap = read_page_snapshot(browser_app)
print(f"    [debug] len(body)={len((snap or {}).get('text') or '')}")  # 自取证：监控真实 innerText 长度
reason = _deleted_reason(snap)
if reason is not None:
    print(f"    🗑️  {reason}，标记 status='deleted' 永久跳过")
    print(f"       [自取证] title={(snap or {}).get('title')!r} "
          f"text={((snap or {}).get('text') or '')[:120]!r}")
    close_tab(browser_app)
    time.sleep(WAIT_CLOSE_TAB)
    return "deleted", None
```

> `len(body)` debug 日志：用户基于经验判定 <60 字阈值有效，但未对新增两类页实证。此日志作为渐进验证——首篇屏蔽/违规 URL 命中时，从日志确认真实 innerText 长度。若 ≥60 字 → `_deleted_reason` 漏检 → 据此迭代阈值或策略。

### 3.5 `mark_deleted` docstring + 相关文案更新（line 254-262 / 954 / 751 / 299-300）

**`mark_deleted` docstring（line 254-262）**——扩三类统称 + 修正函数引用（`incremental_update` 是脚本非函数；`reclaim_clippings` 是 `find_and_rename_in_vault` 旧称）：

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

**同步更新 3 处文案**（维持语义一致，避免未来维护者误以为 `status='deleted'` 仅指发布者删除）：

- **line 954** 行内注释：`# 文章已被发布者删除` → `# 永久不可恢复页（发布者删除/违规/屏蔽）`
- **line 751** `save_one_article` docstring：`'deleted'：文章已被发布者删除（永久不可恢复）` → `'deleted'：永久不可恢复页（发布者删除/违规下架/账号屏蔽）`
- **line 299-300** `get_stats` 注释：`# deleted：status='deleted'（已被发布者删除，永久跳过）...展示有多少文章被发布者删除` → `# deleted：status='deleted'（永久不可恢复，含发布者删除/违规/屏蔽）...展示有多少文章永久不可恢复`

## 4. 测试策略

### 4.1 `_deleted_reason` 单元测试（`tests/test_deleted_page.py`）

删除原 `TestIsDeletedPage` 类（`is_deleted_page` 已删），改为测 `_deleted_reason`：

| 测试 | 输入 | 期望 |
|---|---|---|
| `test_blocked_account_page` | `{text: "此账号已被屏蔽，内容无法查看"}` | `_deleted_reason → "账号被屏蔽"` |
| `test_violation_unavailable_page` | `{text: "此内容因违规无法查看"}` | `_deleted_reason → "违规不可查看"` |
| `test_publisher_deleted_page` | `{text: "该内容已被发布者删除"}` | `_deleted_reason → "发布者删除"` |
| `test_legit_long_article_not_deleted` | `{text: "此账号已被屏蔽...（+ 800 字正文）"}` | `_deleted_reason → None`（阈值防误判） |
| `test_legit_title_with_keyword_body_empty_not_deleted` | `{title: "评此账号已被屏蔽现象", text: ""}` | `_deleted_reason → None`（**只查 body，title 不扫**——防慢加载误杀） |
| `test_no_match_normal_page` | 普通文章快照 | `_deleted_reason → None` |

### 4.2 `is_verify_page` 前置排除测试（`tests/test_verify_page.py` 追加）

现有 7 个 `is_verify_page` 测试仍通过（snapshot 不含 `_DELETED_REASON_MAP` 关键词 → `_deleted_reason` None → 原逻辑）。新增：

| 测试 | 输入 | 期望 |
|---|---|---|
| `test_verify_page_excludes_blocked_page` | `{title: "微信公众平台", text: "此账号已被屏蔽，内容无法查看"}` | `False` |
| `test_verify_page_excludes_violation_page` | `{title: "微信公众平台", text: "此内容因违规无法查看"}` | `False` |
| `test_verify_page_excludes_publisher_deleted` | `{title: "微信", text: "该内容已被发布者删除"}` | `False` |
| `test_verify_page_keeps_real_verify_page` | `{title: "微信公众平台", text: "当前环境异常，完成验证"}` | `True`（链路保留） |

### 4.3 `_is_verify_clipping` 路径测试（`tests/test_malformed_dir.py` 追加）

三检路径：① `title: 微信公众平台` → ② `<200字 & DELETED_CLIPPING_MARKERS` → ③ `VERIFY_CLIPPING_MARKERS ≥2`。测试须明确走哪条路径 + 正文排除 VERIFY 标记 + 测试 ③ 含 DELETED 整句（阈值护栏）+ 完整 frontmatter 结构。**filler 文本必须指定**（避免实现者用含 VERIFY 标记的填充）：

| 测试 | 输入 | 期望 | 路径 |
|---|---|---|---|
| `test_clipping_title_weixin_pub_platform` | `---\ntitle: "微信公众平台"\n---\n正文。` | `True` | ① |
| `test_clipping_blocked_marker_no_special_title` | `---\ntitle: "此账号已被屏蔽"\n---\n此账号已被屏蔽，内容无法查看` | `True` | ②（title 非微信平台才走到；生产是概率性兜底） |
| `test_clipping_long_article_with_marker_not_skipped` | `---\ntitle: "谈审查"\n---\n此账号已被屏蔽，内容无法查看。填充文本。填充文本。...（>200 字，filler 用「填充文本。」* N，禁含 VERIFY 标记）` | `False` | 阈值防误判（含 DELETED 整句——阈值被改时测试失败） |

### 4.4 集成测试（`tests/test_deleted_page.py::TestSaveOneArticleDeletedPath` 追加）

复用现有 mock 模式。**关键：补一个不 mock `handle_verify_page` 的端到端测试**——mock `read_page_snapshot` 返回屏蔽页 + mock `click_confirm`，断言 `click_confirm.call_count == 0`（验证 #4 前置排除在集成层生效，未来删前置排除会回归）。

| 测试 | mock 配置 | 断言 |
|---|---|---|
| `test_save_blocked_page_short_circuits` | mock `read_page_snapshot` → 屏蔽页；mock `handle_verify_page` → `return_value=False` | `result == ("deleted", None)`；`trigger_quick_clip.assert_not_called()`；`handle_verify_page.assert_called_once()`（锁死顺序）；**capsys stdout 含 `🗑️  账号被屏蔽，标记`（全句匹配，非子串——防自取证日志误中）** |
| `test_save_violation_page_short_circuits` | 同上，违规页 | stdout 含 `🗑️  违规不可查看，标记` |
| `test_save_deleted_page_reason_in_stdout` | 同上，发布者删除页 | stdout 含 `🗑️  发布者删除，标记` |
| `test_verify_precise_exclusion_e2e`（**不 mock verify**） | mock `read_page_snapshot` → 屏蔽页；**不 mock `handle_verify_page`**；mock `click_confirm` | `click_confirm.call_count == 0`（屏蔽页不触发 verify 重试——#4 前置排除端到端验证） |

### 4.5 手工验证

`saver` 跑到已知屏蔽/违规 URL 时观察：reason 正确分类、不再出现 `⚠️ 检测到微信验证页` 误导日志、`status='deleted'` 落库、计入 `deleted_count` 而非 `failed_count`、`[debug] len(body)=N` 日志确认真实 innerText 长度。

## 5. 风险与边界

| 风险 | 缓解 |
|---|---|
| 合法文章引用「此账号已被屏蔽」被误判 mark_deleted → 不可逆数据丢失 | `len(body) < 60` 阈值（合法文章 ≫60）；**只查 body 不查 title**（防慢加载 title 误杀） |
| 真实屏蔽/违规页 body innerText ≥60 字（含 UI chrome）→ `_deleted_reason` 漏检 → bug 静默存留 | `[debug] len(body)=N` 自取证日志（渐进验证）；首篇命中时确认长度，据此迭代 |
| 微信改文案 | 第 4 条用前缀匹配；命中即打自取证日志，迭代方便 |
| 「验证后转删除」时序依赖 3 次快照（verify attempt1 → verify attempt2 → 调用点） | 见 §3.4：删除页是终态不重定向，attempt2 与调用点 snap3 之间页面稳定；测试 §4.4 锁死顺序 |
| `is_verify_page` 前置排除改变现有行为 | §4.2 现有 7 测试全通过 + 新增 4 测试覆盖前置排除 |
| 两表策略不一致（MAP 第 4 条 prefix vs CLIPPING 第 4 条 sentence） | 有意权衡：CLIPPING 误判代价更高（永久跳过合法文章）故更保守；微信改文案时 CLIPPING 兜底可能失配，但被 path ① 抢先（CLIPPING 兜底本是概率性） |
| `status='deleted'` 字面义与「屏蔽」不完全契合 | 用户已确认接受；DB 统一视为「永久不可恢复」 |

## 6. 历史教训（5 轮 review 迭代关键点）

记录关键决策路径，避免实施时重蹈覆辙：

1. **顺序交换破坏「验证后转删除」链路**（v2→v3 撤回）：`handle_verify_page`（点确认→页面跳转）必须在 `_deleted_reason` 之前，否则验证页保护的删除页无法被捕获。
2. **委托模式必须清理原常量**（v3→v4）：`is_deleted_page` 委托 `_deleted_reason` 后，原 `DELETED_KEYWORDS` 沦为死代码，必须删除。
3. **verify 精准化是正交方案**（v4 #6）：`is_verify_page` 前置 `_deleted_reason` 排除，与顺序交换无关——不要混为一谈。
4. **新增关键词时重新审视现有逻辑前提**（v4→v5）：原 2 条整句 unlikely 作标题，并 title 无害；新增名词性短语「此账号已被屏蔽」打破这前提 → 标题误杀 → 改为只查 body。
5. **docstring 引用的函数/常量必须实际存在**（v5）：`incremental_update` 是脚本非函数；`reclaim_clippings` 是旧称；`DELETED_KEYWORDS` 已删不能引用。

## 7. 改动清单

**`ima_obsidian_saver.py`**：
- line 254-262 — `mark_deleted` docstring 更新三类统称 + 修正函数引用
- line 299-300 — `get_stats` 注释更新三类统称
- line 464 之前 — 新增 `_DELETED_REASON_MAP` + `_deleted_reason`（提到 `is_verify_page` 之前）
- line 464-476 — `is_verify_page` 首行加前置排除；docstring 更新
- line 479-482 — 删除 `DELETED_KEYWORDS`
- line 485-496 — 删除 `is_deleted_page`（已委托，无生产引用）
- line 581 — `DELETED_CLIPPING_MARKERS` 全用整句 + 注释
- line 751 — `save_one_article` docstring `'deleted'` 语义更新
- line 791-803 — `save_one_article` 调用点：维持 v1 顺序；注释三类统称；加 `[debug] len(body)` 日志；改用 `_deleted_reason`；reason 细分日志
- line 954 — 主循环行内注释更新

**`tests/test_verify_page.py`**：追加 4 个前置排除测试（§4.2）

**`tests/test_deleted_page.py`**：
- 删除 `TestIsDeletedPage` 类，改为 `TestDeletedReason` 测 `_deleted_reason`（§4.1）
- 追加 4 个集成测试（§4.4，含 1 个不 mock verify 的端到端测试）

**`tests/test_malformed_dir.py`**：追加 3 个 `_is_verify_clipping` 路径测试（§4.3）

**`ima_incremental_update.py`**：不动
