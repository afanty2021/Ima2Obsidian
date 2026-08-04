# saver 微信验证页检测 + 自动确认

## Context（为什么改）

2026-07-23 21:26 增量更新中，「皮皮鲁的知识库」11 篇文章保存 100% 失败（id 2914–2925）。systematic-debugging 排查确认：

**主根因（问题 B，11 篇）**：微信文章页对 saver 自动访问**间歇触发**「当前环境异常，要验证后才能正常访问」风控验证。saver `open_url` 打开 URL 后 `sleep 6s`，但页面停在验证页（saver 无点击「确认」逻辑），随后 `option+shift+o`(quick_clip) 在**验证页**触发 → 无文章内容 → 25s 轮询 Clippings 无落盘 →「未找到保存的文件」。风控按频次渐进触发，与「同次运行 AI/Invest/Andrew 库前 7 篇成功、皮皮鲁库后续全挂」时间线吻合。手动实测确认：手动点「确认」过验证后 quick_clip 正常落盘。

**次生根因（问题 A，1 篇 id=2913）**：验证页过渡态下，Web Clipper 把含 `\n` 的页面 title 当文件名 → 9 层畸形嵌套目录（内容完好），saver `find_and_rename` 用 `glob("*.md")` 非递归扫不到 → 漏认领。

**技术前提（已就绪）**：Chrome 的 AppleScript execute JavaScript 已于 2026-07-23 开启（`cua-driver call page enable_javascript_apple_events`，因 Chrome 150 菜单「视图→开发者→允许 Apple 事件中的 JavaScript」点不亮）。saver 现在可用 osascript execute JS 精确读网页/点按钮（Chrome 网页内容对 AX 树不暴露，必须走 execute JS）。

**目标**：saver 在 quick_clip 前检测验证页 + 自动点「确认」；修复后重跑 11 篇补存。

---

## 方案

### 改动 1：验证页检测 + 自动确认（核心）

**新增函数**（`ima_obsidian_saver.py`，照 `close_tab` L320-339 的 osascript 错误处理：`text=True` + `returncode` 检查 + `try/except TimeoutExpired`，仅 print 警告不 raise）：

```python
VERIFY_KEYWORDS = ("当前环境异常", "验证后才能正常访问", "环境异常", "完成验证")

def execute_chrome_js(js: str, browser_app="Google Chrome") -> Optional[str]:
    # osascript 字符串用双引号包裹 → JS 内部一律用单引号，避免引号冲突
    # osascript 字符串内禁止非 ASCII 注释（L304 NOTE）
    script = f'tell application "{browser_app}" to execute active tab of front window javascript "{js}"'
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        if r.returncode == 0: return r.stdout.strip()
        print(f"    ⚠️ execute_chrome_js 失败: {(r.stderr or r.stdout).strip()}")
    except (subprocess.TimeoutExpired, Exception) as e:
        print(f"    ⚠️ execute_chrome_js 异常: {e}")
    return None

def read_page_snapshot(browser_app="Google Chrome") -> Optional[dict]:
    # JS 用单引号；返回 JSON 字符串由 Python 解析
    js = "JSON.stringify({title:document.title,text:(document.body&&document.body.innerText||'').slice(0,800)})"
    raw = execute_chrome_js(js, browser_app)
    try: return json.loads(raw) if raw else None
    except json.JSONDecodeError: return None

def is_verify_page(snapshot: dict) -> bool:   # 纯函数，单测
    if not snapshot: return False
    text = (snapshot.get("text") or "") + (snapshot.get("title") or "")
    return any(k in text for k in VERIFY_KEYWORDS)

def click_confirm(browser_app="Google Chrome") -> bool:
    # 遍历可点击元素，文本含确认词则 click；返回是否点到
    js = ("var b=[...document.querySelectorAll(\"button,a,[role=button],input[type=button],input[type=submit]\")];"
          "var k=['确认','继续访问','继续','确定'];"
          "for(var e of b){var t=(e.textContent||e.value||'').trim();"
          "if(k.some(function(x){return t.indexOf(x)>=0})){e.click();return '1'}} '0'")
    return execute_chrome_js(js, browser_app) == "1"
```

> 实现注意：`execute_chrome_js` 的 JS 参数注入 osascript 双引号字符串——JS 内部必须用单引号、且不能含未转义双引号。`click_confirm` 的 selector 字符串用 `\"` 转义是 Python→osascript 层；落地时确认转义正确（可改用 `chr(34)` 或单引号 selector 规避）。

**集成进 `save_one_article`**（插入点：L577 `sleep(WAIT_PAGE_LOAD)` 之后、L580 `activate_browser` 之前）：

```python
# 验证页检测 + 自动确认（点确认后可能二次确认，最多 2 轮）
for attempt in range(2):
    snap = read_page_snapshot(browser_app)
    if not snap or not is_verify_page(snap):
        break  # 非验证页 → 继续原 quick_clip 流程
    print(f"    ⚠️ 检测到微信验证页，尝试自动确认（轮 {attempt+1}/2）")
    print(f"       [自取证] title={snap.get('title')!r} text={(snap.get('text') or '')[:120]!r}")
    if not click_confirm(browser_app):
        print("    ⚠️ 未找到确认按钮，放弃（保持未保存，下次重试）")
        break
    time.sleep(3.0)  # 等点确认后页面跳转
# 之后继续 activate_browser + trigger_quick_clip + 轮询落盘（L579+ 原逻辑不变）
```

**失败语义天然契合**：点不掉验证页 → quick_clip 仍在验证页失败 → `save_one_article` 返回 `False` → 调用方 L710-717 仅计数、不调 `mark_saved` → `obsidian_saved` 保持 0 → 下次 `get_unsaved_articles`（L196 `WHERE obsidian_saved=0`）自动重试。不丢数据、不改现有流程。

### 改动 2（可选，问题 A 防御性加固）

`find_and_rename_in_vault` L452 + `existing_files` 快照 L568 的 `glob("*.md")` 改为对 **CLIPPINGS_DIR 用 `rglob`**（VAULT_DIR 保持 glob，避免扫全 vault 慢）。两处须同步改，否则「新文件」判定错乱。

> 优先级低。建议先手动捞 2913 那篇畸形目录里完好的 .md（见验证步骤），代码加固随后。

---

## 单测（`tests/test_verify_page.py`）

照 `test_get_ima_main_window.py` 结构（pytest + `class Test*` + `with patch(...)`）：

- `test_is_verify_page_hit/miss`：含/不含 `VERIFY_KEYWORDS` 的 snapshot → True/False（纯函数，无需 mock）
- `test_read_page_snapshot_parse`：`patch("ima_obsidian_saver.execute_chrome_js", return_value='{"title":"x","text":"y"}')` → 断言解析 dict；返回 None/非法 JSON → None
- `test_click_confirm_finds_button`：patch `execute_chrome_js`，断言传入的 JS 含 selector 遍历 + 返回 `'1'`→True、`'0'`→False
- `test_save_one_article_passes_through_when_not_verify`：patch `read_page_snapshot` 返回非验证页 + mock `trigger_quick_clip`/`find_and_rename_in_vault`，断言未调 `click_confirm`

---

## 验证（端到端）

1. **单测**：`pytest tests/test_verify_page.py -v` 全过；且 `pytest tests/` 无回归
2. **重跑 11 篇**（当前验证页不触发，应全成功补存；若触发则新逻辑应对）：
   - 皮皮鲁库（10 篇）：`python3 ima_obsidian_saver.py --kb "皮皮鲁的知识库" --des "皮皮鲁的知识库" --limit 1000`
   - Andrew 库（3 篇含 2913）：`python3 ima_obsidian_saver.py --kb "Andrew" --des "Andrew" --limit 1000`
   - 运行时勿动键鼠
3. **DB 核对**：`SELECT obsidian_saved, COUNT(*) FROM articles WHERE id BETWEEN 2913 AND 2925 GROUP BY obsidian_saved` → 全为 1
4. **手动捞 2913 畸形目录**：把 `Clippings/朋友A君…/n/n…/…md` 的完好 .md 移到 `Andrew/260723 朋友A君….md`，删畸形空目录
5. **验证页实测**（若间歇复现）：观察日志「检测到微信验证页」+ 自动确认 + quick_clip 落盘成功；据 `[自取证]` 全文迭代 `VERIFY_KEYWORDS`/selector

---

## 不确定项

- **验证页「确认」按钮真实 selector 未知**（验证页间歇，无法当场取证 DOM）。`click_confirm` 多 selector 容错 + 自取证，首次触发据日志迭代。
- **验证页特征词**基于用户描述。自取证下次触发捕获真实全文再精化。
- **改动 2 rglob 范围**：仅 CLIPPINGS_DIR，避免扫全 vault；若实测匹配过多再收窄。
