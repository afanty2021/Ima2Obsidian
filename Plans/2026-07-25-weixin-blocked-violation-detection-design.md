# 微信「账号屏蔽 / 违规不可查看」异常页检测设计

> 日期：2026-07-25
> 状态：设计稿，待用户 review → writing-plans
> 关联：[snoopy-pondering-biscuit.md](./snoopy-pondering-biscuit.md)（验证页检测起源）、MEMORY `ima-saver-weixin-verify-page`

## 1. 背景

`ima_obsidian_saver.py` 已对两类微信异常页做了拦截：

| 类别 | 语义 | 现有提示词 | 处理 |
|---|---|---|---|
| 可恢复（风控验证页） | 间歇触发，点「去验证」即可继续 | `当前环境异常` / `完成验证` 等 | `handle_verify_page` 自动点确认 |
| 永久不可恢复 | 文章已不存在，重试无意义 | `该内容已被发布者删除` / `此内容因违规已删除` | `is_deleted_page` → `mark_deleted` → `status='deleted'` 永久跳过 |

Chrome 中打开微信公众号文章时，除上述两种外，还会出现：

- **「此账号已被屏蔽，内容无法查看」** — 账号被微信平台屏蔽（永久）
- **「此内容因违规无法查看」** — 内容被平台下架（永久）

二者均为**永久不可恢复**状态，与现有 `DELETED_KEYWORDS` 同类。当前未拦截 → 每次运行都会反复打开这两类 URL → `quick_clip` 0 落盘 → `failed_count++` → 触发上游 `incremental_update` / `launchd` 假告警；同时 Web Clipper 可能把极简提示页落盘成 `.md`，需 `find_and_rename` 兜底排除。

## 2. 方案对比与决策

### 2.1 三种方案

| 方案 | 改动面 | 优点 | 缺点 |
|---|---|---|---|
| **A. 扩 `DELETED_KEYWORDS` 词表（推荐）** | 2 处常量 + 日志文案 | 最小改动；复用全部已加固的误判防护（<60 字阈值、落盘兜底） | `status='deleted'` 字面意义略宽于"发布者删除"（其实早含"违规已删除"） |
| B. 新增 `status='blocked'` | schema + 5+ 处 WHERE + stats + 文案 | 语义清晰 | 违反 YAGNI；行为与 deleted 完全相同，无实际收益 |
| C. 重命名 `status='unavailable'` + 迁移历史数据 | 数据迁移 + 全量改动 | 语义统一 | 改动面最大，无净收益 |

### 2.2 决策（用户已确认 2026-07-25）

- **方案 A**：扩 `DELETED_KEYWORDS` 与 `DELETED_CLIPPING_MARKERS`，复用现有 `is_deleted_page` 短文本阈值与 `mark_deleted` 路径。
- **日志细分原因、DB 统一**：日志区分「发布者删除 / 账号屏蔽 / 违规不可查看」三种 reason，便于从输出排查某篇为何没保存；DB `status` 仍统一 `'deleted'`，stats 文案不变。

## 3. 实施改动（精确到位置）

所有改动均在 `ima_obsidian_saver.py`。`ima_incremental_update.py` 经 subprocess 调 saver，无需改动。

### 3.1 扩词表常量

**`ima_obsidian_saver.py:482`** — `DELETED_KEYWORDS`

```python
# before
DELETED_KEYWORDS = ("该内容已被发布者删除", "此内容因违规已删除")

# after
# 三类永久不可恢复页（行为一致：mark_deleted 永久跳过，不计 failed）：
#   发布者删除 / 平台下架违规内容 / 账号被平台屏蔽
DELETED_KEYWORDS = (
    "该内容已被发布者删除",   # 发布者主动删除
    "此内容因违规已删除",     # 平台下架（旧文案，违规已删除）
    "此内容因违规无法查看",   # 平台下架（新文案，违规不可查看）
    "此账号已被屏蔽",         # 账号被平台屏蔽（"内容无法查看" 是后缀，前缀更稳定）
)
```

> 用「此账号已被屏蔽」前缀而非整句「此账号已被屏蔽，内容无法查看」，避免微信文案微调（逗号变体、后缀增删）导致漏判；前缀命中即足够。

**`ima_obsidian_saver.py:581`** — `DELETED_CLIPPING_MARKERS`（Web Clipper 落盘兜底）

```python
# before
DELETED_CLIPPING_MARKERS = ("该内容已被发布者删除", "此内容因违规已删除")

# after
DELETED_CLIPPING_MARKERS = DELETED_KEYWORDS  # 与正文检测同源，避免双源漂移
```

> `_is_verify_clipping` 已用 `len(txt) < 200` 短文本阈值防误判，对新增两类提示页（落盘后正文也极短）同样适用，无需调阈值。

### 3.2 `is_deleted_page` 不改逻辑

`is_deleted_page(snap)` 现有实现 `len(text) < 60 and any(k in text for k in DELETED_KEYWORDS)`（line 485-496）对新增两类依然有效：

- 「此账号已被屏蔽，内容无法查看」整页 innerText ≈ 15 字 ≪ 60
- 「此内容因违规无法查看」整页 innerText ≈ 11 字 ≪ 60
- 合法讨论审查的文章即便正文引用整句，前 800 字正文 ≫ 60，不误判

**仅更新 docstring**：把"文章已被发布者删除"扩为三类永久不可恢复页的统称。

### 3.3 日志细分原因

`save_one_article`（line 794-803）命中 `is_deleted_page` 后，目前统一打 `🗑️  文章已被发布者删除`。改为按命中关键词映射 reason：

```python
# before
if is_deleted_page(snap):
    print(f"    🗑️  文章已被发布者删除，标记 status='deleted' 永久跳过")
    print(f"       [自取证] title={(snap or {}).get('title')!r} "
          f"text={((snap or {}).get('text') or '')[:120]!r}")
    close_tab(browser_app)
    time.sleep(WAIT_CLOSE_TAB)
    return "deleted", None

# after
reason = _deleted_reason(snap)  # 新增辅助函数
if reason:
    print(f"    🗑️  {reason}，标记 status='deleted' 永久跳过")
    print(f"       [自取证] title={(snap or {}).get('title')!r} "
          f"text={((snap or {}).get('text') or '')[:120]!r}")
    close_tab(browser_app)
    time.sleep(WAIT_CLOSE_TAB)
    return "deleted", None
```

**新增辅助函数** `_deleted_reason(snapshot) -> Optional[str]`：返回人类可读 reason 字符串。

**契约（关键）**：`_deleted_reason(snap) is None` ⇔ `is_deleted_page(snap) is False`。实现上即把 `is_deleted_page` 的完整判定（`len(text) < 60` 阈值 + 关键词命中）内联进来——**不得**简化成纯关键词 `any(k in text for k in DELETED_KEYWORDS)`，否则合法讨论审查的文章（前 800 字正文 ≫ 60）会被误判 mark_deleted → 不可逆数据丢失。命中关键词后映射 reason：

| 命中关键词 | reason |
|---|---|
| `该内容已被发布者删除` | `发布者删除` |
| `此内容因违规已删除` / `此内容因违规无法查看` | `违规不可查看` |
| `此账号已被屏蔽` | `账号被屏蔽` |

**为什么改 `if is_deleted_page(snap)` 为 `if reason`**：避免对快照二次调用 `is_deleted_page` + `_deleted_reason`（同一份 text 跑两次正则匹配是浪费）；`_deleted_reason` 返回 None 即等价于"非删除页"，语义一致。

### 3.4 主循环日志不改

`main()` line 953-958 的 `elif status == "deleted":` 分支文案（`🗑️  已删除（标记 status='deleted' 永久跳过）`）保持不变——单篇 reason 已在 `save_one_article` 内打日志，主循环只计 `deleted_count` 汇总。

## 4. 测试策略

### 4.1 单元测试（新增，放 `tests/`）

| 测试 | 输入 | 期望 |
|---|---|---|
| `test_blocked_account_page` | `{title: "微信网页", text: "此账号已被屏蔽，内容无法查看"}` | `is_deleted_page → True`；`_deleted_reason → "账号被屏蔽"` |
| `test_violation_unavailable_page` | `{title: "微信网页", text: "此内容因违规无法查看"}` | `is_deleted_page → True`；`_deleted_reason → "违规不可查看"` |
| `test_publisher_deleted_page` | `{title: "微信网页", text: "该内容已被发布者删除"}` | `is_deleted_page → True`；`_deleted_reason → "发布者删除"`（回归保护） |
| `test_legit_article_discussing_censorship` | `{title: "谈审查", text: "此账号已被屏蔽...（+ 800 字正文）"}` | `is_deleted_page → False`（<60 字阈值防误判） |
| `test_verify_clipping_markers_blocked` | 内容含"此账号已被屏蔽" 且 <200 字 | `_is_verify_clipping → True`（落盘兜底） |
| `test_no_match_normal_page` | 普通文章快照 | `is_deleted_page → False`；`_deleted_reason → None` |

### 4.2 手工验证（用户实操，不写自动化）

`saver` 跑到一篇已知被屏蔽的 URL 时，观察日志输出 reason 是否正确分类、`status='deleted'` 是否落库、是否计入 `deleted_count` 而非 `failed_count`。

## 5. 风险与边界

| 风险 | 缓解 |
|---|---|
| 合法文章正文引用「此账号已被屏蔽」被误判 mark_deleted → 永久跳过 = 不可逆数据丢失 | `is_deleted_page` 的 `len(text) < 60` 阈值已防：合法文章前 800 字正文远超 60；`_is_verify_clipping` 的 `<200` 阈值同理 |
| 微信未来再改文案 | 已用前缀匹配（"此账号已被屏蔽"）而非整句；命中即打自取证日志（title+text 片段），迭代方便 |
| `status='deleted'` 字面义与新增"屏蔽"不完全契合 | 用户已确认接受；DB 不分状态，统一视为"永久不可恢复" |
| 与已有「此内容因违规已删除」语义重叠 | 不冲突：新旧违规文案都映射到同一 reason `违规不可查看`，统计无影响 |

## 6. 不做的事（YAGNI）

- ❌ 不新增 `status='blocked'` / `status='unavailable'`
- ❌ 不改 schema、WHERE 子句、stats 查询
- ❌ 不改 `main()` 主循环汇总文案
- ❌ 不写增量重扫脚本：已 `mark_deleted` 的历史数据无需重分类，本改动只影响**未来**命中的文章

## 7. 改动清单

- `ima_obsidian_saver.py:482` — 扩 `DELETED_KEYWORDS`（2 词 → 4 词）
- `ima_obsidian_saver.py:581` — `DELETED_CLIPPING_MARKERS` 引用 `DELETED_KEYWORDS`（同源）
- `ima_obsidian_saver.py:485` 区域 — 新增 `_deleted_reason(snap)` 辅助函数
- `ima_obsidian_saver.py:485` 区域 — 更新 `is_deleted_page` docstring（不改逻辑）
- `ima_obsidian_saver.py:794-803` — `save_one_article` 用 `_deleted_reason` 替代 `is_deleted_page` 调用 + 细分日志
- `tests/` — 新增本设计 4.1 节 6 个单测
