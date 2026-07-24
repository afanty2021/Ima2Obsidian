#!/usr/bin/env python3
"""
IMA 微信文章 → Obsidian 自动保存器

从数据库读取已提取的文章 URL，在浏览器中打开，
通过 Obsidian Web Clipper 扩展保存到 Obsidian，
并自动重命名为 YYMMDD + title 格式。

工作流:
  1. 预提取文章发布日期（从微信页面 create_time 变量）
  2. 在浏览器中打开文章
  3. 触发 Web Clipper quick_clip 保存到 Obsidian
  4. 在 vault 中找到保存的文件，重命名为 YYMMDD title.md

前置条件:
  1. Chrome/Edge/Safari 已安装 Obsidian Web Clipper 扩展
  2. Obsidian 应用已运行并打开目标 Vault
  3. Web Clipper 已在扩展中连接到 Obsidian

使用:
  python3 ima_obsidian_saver.py                       # 保存所有
  python3 ima_obsidian_saver.py --limit 5             # 只处理前 5 篇
  python3 ima_obsidian_saver.py --dry-run             # 预览模式
  python3 ima_obsidian_saver.py --browser safari      # 使用 Safari
  python3 ima_obsidian_saver.py --mode clipper        # 弹窗模式
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Optional

import warnings

# 系统 Python 3.9 + LibreSSL 与 urllib3 v2 不兼容会触发 NotOpenSSLWarning，
# 污染 stderr 被 incremental_update 误冠 "错误:" 前缀。须在 import requests
# （触发 urllib3 首次导入并 warn）之前注册过滤。
# 注意：warnings.filterwarnings 的 message 是正则，用 re.match（行首锚定）匹配，
# 不是子串 search——故这里给的是告警文本的完整前缀。当前 urllib3 措辞命中、
# 全新子进程下有效（已实证 launchd 下被抑制）；若 urllib3 改写告警措辞需同步更新。
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import requests

from ima_common import DB_FILE, init_database, now_saved_at


# ==================== 配置 ====================

VAULT_DIR = Path("/Users/berton/Obsidian Vault")
CLIPPINGS_DIR = VAULT_DIR / "Clippings"

# 浏览器快捷键映射
BROWSERS = {
    "chrome": {"app": "Google Chrome", "shortcut_mods": ["option", "shift"]},
    "edge": {"app": "Microsoft Edge", "shortcut_mods": ["option", "shift"]},
    "safari": {"app": "Safari", "shortcut_mods": ["option", "shift"]},
}
DEFAULT_BROWSER = "chrome"

# Web Clipper quick_clip 快捷键
QUICK_CLIP_KEY = "o"
CLIPPER_KEY = "o"
CLIPPER_MODS = ["command", "shift"]

# 时间配置（秒）
WAIT_PAGE_LOAD = 6.0
WAIT_CLIP_SAVE = 4.0       # 触发 quick_clip 后起步等待（首次轮询前给 Clipper 一个窗口）
WAIT_CLIP_TOTAL = 25.0     # 轮询等待文件落盘的总预算（修夜间慢盘时序竞争；交互式秒回）
WAIT_CLIP_POLL = 1.5       # 落盘轮询间隔
WAIT_CLOSE_TAB = 1.0
WAIT_BETWEEN = 1.5

DEFAULT_LIMIT = 1300


# ==================== 日期提取 ====================

def extract_publish_date(url: str) -> str:
    """从微信文章页面提取发布日期，返回 YYMMDD 格式（带重试）"""
    import time

    # 指数退避重试：最多3次，超时依次为 15, 20, 25 秒
    for attempt in range(3):
        try:
            timeout = 15 + attempt * 5  # 15, 20, 25 秒
            headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
            resp = requests.get(url, headers=headers, timeout=timeout)
            html = resp.text

            # 方法1: create_time: JsDecode('YYYY-MM-DD HH:MM')
            m = re.search(r"create_time:\s*JsDecode\('(\d{4}-\d{2}-\d{2})", html)
            if m:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d")
                return dt.strftime("%y%m%d")

            # 方法2: ori_create_time / create_timestamp (Unix 时间戳)
            m = re.search(r"(?:ori_create_time|create_timestamp):\s*'(\d{10})'", html)
            if m:
                dt = datetime.fromtimestamp(int(m.group(1)))
                return dt.strftime("%y%m%d")

            # 方法3: var createTime = 'YYYY-MM-DD HH:MM'
            m = re.search(r"var\s+createTime\s*=\s*'(\d{4}-\d{2}-\d{2})", html)
            if m:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d")
                return dt.strftime("%y%m%d")

            # 方法4: publish_time (Unix 时间戳，在 URL 编码的 JSON 中)
            m = re.search(r"publish_time%22%3A(\d{10})", html)
            if m:
                dt = datetime.fromtimestamp(int(m.group(1)))
                return dt.strftime("%y%m%d")

            # 四种正则均未匹配（页面结构变更或非标准文章），不再重试，降级为当前日期
            print(f"    ⚠️  未匹配到发布日期正则（页面结构可能变更），将降级使用当前日期")
            break

        except requests.RequestException as e:
            print(f"    ⚠️  网络请求失败 (尝试 {attempt + 1}/3): {e}")
            if attempt < 2:  # 前两次失败时重试
                wait_time = 2 ** attempt  # 指数退避: 1, 2 秒
                print(f"    等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
            else:
                print(f"    ⚠️  网络重试耗尽，使用当前日期")
        except Exception as e:
            print(f"    ⚠️  日期提取失败: {e}")
            break

    # 降级: 使用当前日期
    return datetime.now().strftime("%y%m%d")


def extract_publish_date_js(browser_app: str = "Google Chrome") -> Optional[str]:
    """execute JS 读微信文章页 #publish_time 元素的发布日期（如 '2026年7月15日 09:56'）→ YYMMDD。

    requests 抓到的是微信精简页（无 create_time 字段，extract_publish_date 必失败）；
    浏览器渲染后 #publish_time 才有发布日期，故 open 文章后用本函数读（更可靠）。
    非日期文本/失败返回 None，让上游降级到 extract_publish_date 的值或 extract_date_from_content。
    """
    js = "(document.getElementById('publish_time')||{}).textContent"
    raw = execute_chrome_js(js, browser_app)
    if not raw:
        return None
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", raw)
    if not m:
        return None
    return f"{m.group(1)[2:]}{int(m.group(2)):02d}{int(m.group(3)):02d}"


def extract_date_from_content(text: str) -> Optional[str]:
    """从 Web Clipper 保存的文章正文提取发布日期（如 *2026年6月25日 10:00*）

    Returns:
        YYMMDD 字符串；正文无匹配模式时返回 None（不是空串）。

        契约要求：调用方把返回值喂给 SQL COALESCE(?, published_date) 时，
        必须传 None（而非 ""）才能让 COALESCE 跳过保留 DB 已有值——
        SQLite 中空串非 NULL，COALESCE('', 'fallback') 会选中空串覆盖 DB。
        本函数的历史 bug 是返回 "" 导致 reclaim 把 DB 已有真实日期清空。
    """
    m = re.search(r'\*(\d{4})年(\d{1,2})月(\d{1,2})日', text)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return dt.strftime("%y%m%d")
        except ValueError:
            pass
    return None


def sanitize_filename(title: str) -> str:
    """清理文件名中的非法字符，并按字节截断以遵守 macOS 255 字节限制"""
    title = title or ""  # None 安全：re.sub 对 None 抛 TypeError（调用方未必都守卫）
    # 移除或替换不适合文件名的字符
    cleaned = re.sub(r'[/\\:*?"<>|]', '-', title)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # macOS/APFS 文件名上限为 255 *字节*（非字符）。中文 UTF-8 占 3 字节/字，
    # 旧的字符截断 [:100] 对纯中文标题仍超限（100 中文字 ≈ 300 字节），导致
    # Web Clipper 落盘失败或被系统截断、重命名匹配不上 → 长标题文章变僵尸。
    # 文件名固定开销 "YYMMDD "(7) + ".md"(3) = 10 字节，留余量取 240 字节。
    MAX_BYTES = 240
    encoded = cleaned.encode('utf-8')
    if len(encoded) > MAX_BYTES:
        encoded = encoded[:MAX_BYTES]
        # 截断可能落在多字节字符中间，丢弃残缺尾部字节
        cleaned = encoded.decode('utf-8', errors='ignore')
    # 字节截断可能把末尾恰好落在空格上（上面的 strip 在截断之前执行），
    # 再 strip 一次避免 "260722 标题 .md"（.md 前尾随空格）引发 Finder 隐藏 /
    # Obsidian·iCloud 跨平台同步隐患。
    return cleaned.strip()


# ==================== 数据库 ====================

def get_unsaved_articles(limit: int, kb: str = None):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        c = conn.cursor()
        if kb:
            # 按知识库过滤，避免把其他 KB 的文章存进 --des 指定的文件夹
            c.execute("""
                SELECT id, url, title, knowledge_base
                FROM articles
                WHERE (obsidian_saved = 0 OR obsidian_saved IS NULL)
                  AND status = 'success'
                  AND url LIKE '%mp.weixin.qq.com%'
                  AND knowledge_base = ?
                ORDER BY id ASC
                LIMIT ?
            """, (kb, limit))
        else:
            c.execute("""
                SELECT id, url, title, knowledge_base
                FROM articles
                WHERE (obsidian_saved = 0 OR obsidian_saved IS NULL)
                  AND status = 'success'
                  AND url LIKE '%mp.weixin.qq.com%'
                ORDER BY id ASC
                LIMIT ?
            """, (limit,))
        rows = c.fetchall()
    return [{"id": r[0], "url": r[1], "title": r[2], "kb": r[3]} for r in rows]


def mark_saved(article_id: int, published_date: str = None):
    """
    标记文章为已保存到 Obsidian。

    published_date 采用 COALESCE 保护：
      - 调用方传入新日期 → 写入新值
      - 调用方未传（None）→ 保留 DB 中已有的值，避免重试场景误清空
      - DB 中也无值 → 保持 NULL（与 reclaim_clippings 的 UPDATE 口径一致）
    """
    with closing(sqlite3.connect(DB_FILE)) as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE articles SET obsidian_saved = 1, obsidian_saved_at = ?, "
            "published_date = COALESCE(?, published_date) WHERE id = ?",
            (now_saved_at(), published_date, article_id),
        )
        conn.commit()


def mark_deleted(article_id: int):
    """把文章标记为「已被发布者删除」：status 改为 'deleted'，永久跳出待保存队列。

    与 mark_saved 不同——删除是永久状态，不写 obsidian_saved（保持其 0/NULL 语义
    即「从未成功保存过」），仅改 status。所有待保存查询（get_unsaved_articles /
    get_stats / reclaim_clippings / incremental_update）都用 WHERE status='success'，
    故 status='deleted' 自动从这些查询消失，无需改任何 WHERE，也不会被下次运行反复打开。
    不计 failed_count，避免 0 落盘的删除页触发上游 launchd/incremental_update 告警。
    """
    with closing(sqlite3.connect(DB_FILE)) as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE articles SET status = 'deleted' WHERE id = ?",
            (article_id,),
        )
        conn.commit()


def get_stats(kb: str = None):
    """
    返回 {total, saved, unsaved, deleted}。

    unsaved 直接用与 get_unsaved_articles 完全相同的 WHERE 计算，
    避免 max(0, total-saved) 在 obsidian_saved 出现非 {0,1,NULL} 异常值时
    与实际可被处理的文章数分叉（导致 stats 显示有待保存但 main 取不到文章）。
    """
    with closing(sqlite3.connect(DB_FILE)) as conn:
        c = conn.cursor()
        where = "WHERE status = 'success' AND url LIKE '%mp.weixin.qq.com%'"
        params = []
        if kb:
            where += " AND knowledge_base = ?"
            params.append(kb)
        c.execute(f"SELECT COUNT(*) FROM articles {where}", params)
        total = c.fetchone()[0]
        # saved 与 total 同口径（都过滤 status+url，可选 kb）
        c.execute(f"SELECT COUNT(*) FROM articles {where} AND obsidian_saved = 1", params)
        saved = c.fetchone()[0]
        # unsaved 必须与 get_unsaved_articles 同口径：
        # (obsidian_saved = 0 OR obsidian_saved IS NULL)，避免异常值导致分叉
        c.execute(
            f"SELECT COUNT(*) FROM articles {where} AND (obsidian_saved = 0 OR obsidian_saved IS NULL)",
            params,
        )
        unsaved = c.fetchone()[0]
        # deleted：status='deleted'（已被发布者删除，永久跳过）。与 total 同 url/kb 口径，
        # 但 status 维度独立——不计入 total/unsaved，单独展示有多少文章被发布者删除。
        c.execute(
            "SELECT COUNT(*) FROM articles "
            "WHERE url LIKE '%mp.weixin.qq.com%' AND status = 'deleted'"
            + (" AND knowledge_base = ?" if kb else ""),
            params,
        )
        deleted = c.fetchone()[0]
    return {"total": total, "saved": saved, "unsaved": unsaved, "deleted": deleted}


# ==================== 浏览器自动化 ====================

def activate_browser(browser_app: str):
    subprocess.run(
        ["osascript", "-e", f'tell application "{browser_app}" to activate'],
        capture_output=True, timeout=5,
    )
    time.sleep(0.5)


def open_url(browser_app: str, url: str):
    subprocess.run(["open", "-a", browser_app, url], capture_output=True, timeout=10)


def send_keystroke(key: str, modifiers: list = None):
    if modifiers:
        parts = [f"{m} down" for m in modifiers]
        mod_str = " using {" + ", ".join(parts) + "}"
    else:
        mod_str = ""
    subprocess.run(
        ["osascript", "-e", f'tell application "System Events" to keystroke "{key}"{mod_str}'],
        capture_output=True, timeout=5,
    )


def close_tab(browser_app: str = None, retry_count: int = 0):
    """
    关闭浏览器标签页，优先使用后台方式

    Args:
        browser_app: 浏览器应用名称（如 "Chrome", "Safari"）
        retry_count: 重试次数（内部使用）
    """
    max_retries = 2

    if browser_app:
        # AppleScript 后台关闭，不激活 Chrome
        # NOTE: AppleScript 字符串中不能有非ASCII注释，会导致 osascript 语法错误
        script = f'''
tell application "{browser_app}"
    if (count of windows) > 0 then
        set w to window 1
        set tabCount to count of tabs of w
        if tabCount > 1 then
            close active tab of w
            return "closed"
        else
            return "single_tab"
        end if
    end if
    return "no_window"
end tell
'''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                stdout = result.stdout.strip().lower()
                if "closed" in stdout or "single_tab" in stdout:
                    if "single_tab" in stdout:
                        # 仅剩单个标签：Cmd+W 会关闭整个浏览器窗口（含用户其他标签），保留不动
                        print(f"    ℹ️  浏览器仅剩单标签，保留以避免关闭整个窗口")
                        return
                    print(f"    ✓ 标签页已关闭（AppleScript）")
                    return
            else:
                print(f"    ⚠️ AppleScript 关闭失败: {result.stderr or result.stdout}")
        except subprocess.TimeoutExpired:
            print(f"    ⚠️ AppleScript 执行超时")
        except Exception as e:
            print(f"    ⚠️ AppleScript 异常: {e}")

    # 降级方案：快捷键发到特定浏览器进程（而非全局）
    print(f"    → 尝试快捷键关闭...")
    try:
        if browser_app:
            r = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to tell process "{browser_app}" to keystroke "w" using command down'],
                capture_output=True, timeout=5
            )
            if r.returncode != 0:
                raise RuntimeError(f"osascript 退出码 {r.returncode}")
        else:
            send_keystroke("w", ["command"])
        print(f"    ✓ 标签页已关闭（快捷键）")
        time.sleep(0.5)
    except Exception as e:
        print(f"    ❌ 快捷键关闭失败: {e}")

        if retry_count < max_retries:
            print(f"    → 重试关闭 ({retry_count + 1}/{max_retries})...")
            time.sleep(1)
            close_tab(browser_app, retry_count + 1)
        else:
            print(f"    ⚠️ 警告：标签页可能未关闭，请手动检查 {browser_app or '浏览器'}")


def trigger_quick_clip(mods: list):
    send_keystroke(QUICK_CLIP_KEY, mods)


def trigger_clipper_and_save(mods: list):
    send_keystroke(CLIPPER_KEY, CLIPPER_MODS)
    time.sleep(2.0)
    send_keystroke("return", [])


# ==================== 微信验证页检测 ====================

# 微信「当前环境异常」风控验证页特征词。saver 自动访问会间歇触发该页，导致 quick_clip
# 打在验证页上无文章内容 → 0 落盘（见 Plans/snoopy-pondering-biscuit.md）。
# Chrome execute JS 已开启，故在 quick_clip 前用 JS 检测 + 自动点「确认」。
VERIFY_KEYWORDS = ("当前环境异常", "验证后才能正常访问", "环境异常", "完成验证")
# 「去验证」按钮(id=js_verify)在验证页渲染较慢，click_confirm 首次可能落空，故重试
VERIFY_CLICK_RETRIES = 4


def execute_chrome_js(js: str, browser_app: str = "Google Chrome") -> Optional[str]:
    """通过 AppleScript 在 Chrome 当前标签页执行 JS，返回求值结果字符串。

    照 close_tab 的错误处理：text=True + returncode 检查 + 超时/异常仅警告不 raise。
    引号约定：JS 被拼进 osascript 双引号字符串，JS 内部一律用单引号，不得含未转义
    双引号或反斜杠（否则 osascript 语法错）。osascript 字符串内亦不得有非 ASCII 注释。
    """
    script = f'tell application "{browser_app}" to execute active tab of front window javascript "{js}"'
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip()
        print(f"    ⚠️ execute_chrome_js 失败: {(r.stderr or r.stdout).strip()}")
    except subprocess.TimeoutExpired:
        print("    ⚠️ execute_chrome_js 超时")
    except Exception as e:
        print(f"    ⚠️ execute_chrome_js 异常: {e}")
    return None


def read_page_snapshot(browser_app: str = "Google Chrome") -> Optional[dict]:
    """读当前页 title + 正文前 800 字，供验证页检测与自取证。失败返回 None。"""
    js = "JSON.stringify({title:document.title,text:(document.body&&document.body.innerText||'').slice(0,800)})"
    raw = execute_chrome_js(js, browser_app)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def is_verify_page(snapshot: Optional[dict]) -> bool:
    """判断页面快照是否为微信风控验证页（纯函数）。

    验证页 text 可能没渲染（只剩 title='微信公众平台'），故 title 判定优先于关键词扫描。
    """
    if not snapshot:
        return False
    title = snapshot.get("title") or ""
    if title == "微信公众平台":  # 验证页 title（text 没渲染时的可靠标志）
        return True
    text = (snapshot.get("text") or "") + title
    return any(k in text for k in VERIFY_KEYWORDS)


# 微信「文章已被发布者删除」特征词。这类文章已不存在，永远无法保存——若保持未保存，
# 每次运行都会反复打开它（0 落盘 → failed_count++ → 触发上游告警）。检测到即 mark_deleted
# 把 status 改 'deleted'，自动从所有 status='success' 查询消失，永久跳过。
DELETED_KEYWORDS = ("该内容已被发布者删除", "此内容因违规已删除")


def is_deleted_page(snapshot: Optional[dict]) -> bool:
    """判断页面快照是否为「文章已被发布者删除」页（纯函数，与 is_verify_page 同构）。

    删除页是永久状态（不同于可恢复的验证页）：命中后短路返回，不触发 quick_clip。
    关键词与 VERIFY_KEYWORDS 互斥（删除页文本不含验证词），二者可先后检测互不误判。
    """
    if not snapshot:
        return False
    text = (snapshot.get("text") or "") + (snapshot.get("title") or "")
    return any(k in text for k in DELETED_KEYWORDS)


def click_confirm(browser_app: str = "Google Chrome") -> bool:
    """点掉验证页「去验证」按钮，返回是否点到。

    优先 getElementById('js_verify')——验证页「去验证」a 的稳定 id，不依赖 selector 时机
    （实测 selector 遍历在 saver 自动跑时偶发漏点）。js_verify 不在时退回 selector 文本匹配。
    execute_chrome_js 返回 '1' 表示点到。
    """
    js = ("var v=document.getElementById('js_verify');"
          "if(v){v.click();'1'}else{"
          "var b=[...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')];"
          "var k=['确认','继续访问','继续','确定','去验证'];"
          "for(var e of b){var t=(e.textContent||e.value||'').trim();"
          "if(k.some(function(x){return t.indexOf(x)>=0})){e.click();return '1'}}'0'}")
    return execute_chrome_js(js, browser_app) == "1"


def handle_verify_page(browser_app: str = "Google Chrome") -> bool:
    """检测并处理微信验证页。返回是否遇到过验证页（True=遇到过，False=非验证页）。

    在 quick_clip 前调用：非验证页直接放行；验证页则自动点「确认」，最多 2 轮（应对二次
    确认）。点不掉则放弃——quick_clip 会在验证页失败，save_one_article 返回 False，
    obsidian_saved 保持 0，下次 get_unsaved_articles 自动重试，不丢数据。
    每次命中打印 title+text 片段用于自取证（迭代 VERIFY_KEYWORDS / click_confirm）。
    """
    encountered = False
    for attempt in range(2):
        snap = read_page_snapshot(browser_app)
        if not snap or not is_verify_page(snap):
            return encountered  # 非验证页：首次则 False；点确认后离开则 True
        encountered = True
        print(f"    ⚠️ 检测到微信验证页，尝试自动确认（轮 {attempt + 1}/2）")
        print(f"       [自取证] title={snap.get('title')!r} text={(snap.get('text') or '')[:120]!r}")
        # 「去验证」a(id=js_verify)渲染慢，click_confirm 首次可能落空 → 重试等渲染
        clicked = False
        for _ in range(VERIFY_CLICK_RETRIES):
            if click_confirm(browser_app):
                clicked = True
                break
            time.sleep(1.0)
        if not clicked:
            print("    ⚠️ 未找到确认按钮，放弃（保持未保存，下次重试）")
            return True
        time.sleep(3.0)  # 等点确认后页面跳转到真文章
    return True


# ==================== Vault 文件重命名 ====================

def _non_conflicting_path(target: Path, source: Path) -> Path:
    """若 target 已存在且非 source 自身，追加 ' 2'/' 3' 序号后缀避免覆盖。

    Path.rename 在 POSIX 上原子覆盖目标；无守卫时两篇 sanitize 后同名的文章，
    第二篇会静默覆盖第一篇已落盘的 .md（永久丢数据）。此函数把目标改到不冲突路径，
    追加序号保留两文件。

    注意：与 reclaim_clippings 的冲突策略相反——reclaim 命中冲突直接跳过、把孤儿
    留在 Clippings；本函数追加序号保留两文件（saver 场景下两篇都是刚 clip 的有效内容，
    不能丢）。两者对同一 exists()+resolve() 条件采取不同动作，并非"对齐"。
    """
    if not target.exists() or target.resolve() == source.resolve():
        return target
    stem, suffix = target.stem, target.suffix
    n = 2
    while True:
        cand = target.with_name(f"{stem} {n}{suffix}")
        # macOS 文件名 255 字节上限：序号递增使名字变长，超限时 Path.exists() 会把
        # ENAMETOOLONG 静默当"可用"返回非法路径 → 随后 rename 崩。超限即按字节截断
        # stem 给 " N<suffix>" 留余量后重试（触发需数千同名冲突，属防御性兜底）。
        if len(cand.name.encode("utf-8")) > 255:
            stem = stem.encode("utf-8")[:240].decode("utf-8", errors="ignore")
            n = 2  # stem 缩短后从序号 2 重新找，避免 n 冻结导致的理论死循环（物理不可触发：
                   # 240B stem 需 ~10^12 同名冲突才会使后缀再超限，属防御性兜底，无单测覆盖）
            continue
        if not cand.exists() or cand.resolve() == source.resolve():
            return cand
        n += 1


# Web Clipper 落盘的干扰页内容特征。saver 在验证页/删除页上 quick_clip 会把干扰页存成 md
# （title=微信公众平台），find_and_rename 须排除这类文件，防止把干扰页当文章认领。
# 删除页命中后已短路不 clip，此处为防御性兜底（时序异常/短路未生效时仍能拦截）。
VERIFY_CLIPPING_MARKERS = ("环境异常", "完成验证", "去验证")
DELETED_CLIPPING_MARKERS = ("该内容已被发布者删除", "此内容因违规已删除")


def _is_verify_clipping(md_path: Path) -> bool:
    """检测 Web Clipper 落盘的 .md 是否为验证页/删除页等干扰内容（非文章）。"""
    try:
        txt = md_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    markers = VERIFY_CLIPPING_MARKERS + DELETED_CLIPPING_MARKERS
    return any(k in txt for k in markers) or '"微信公众平台"' in txt


def find_and_rename_in_vault(
    title: str,
    date_str: str,
    existing_files: set,
    search_dirs: list = None,
    target_folder: str = None,
):
    """
    在 Obsidian vault 中找到 Web Clipper 刚保存的文件，
    重命名为 YYMMDD title.md 格式，并移动到目标文件夹。

    existing_files: set of (Path, mtime) tuples captured before opening article
    target_folder: 目标文件夹名称（如 "AI"），如果为 None 则保持在原位置

    返回 (renamed: bool, actual_date_used: Optional[str])：
      - renamed=True 时 actual_date_used 是实际用于命名的日期
        （可能从文件内容 *YYYY年M月D日* 提取，覆盖了降级为"今天"的输入 date_str）
      - renamed=False 时 actual_date_used 为 None
    调用方（save_one_article → mark_saved）必须用此返回值把真实日期存进 DB published_date，
    避免 DB 存今天、文件名存真实日期的不一致。
    """
    if search_dirs is None:
        search_dirs = [CLIPPINGS_DIR, VAULT_DIR]

    target_name = f"{date_str} {sanitize_filename(title)}.md"

    # 确定目标路径
    if target_folder:
        # 创建目标文件夹路径
        folder_path = VAULT_DIR / target_folder
        folder_path.mkdir(parents=True, exist_ok=True)
        final_target_path = folder_path / target_name
    else:
        # 不移动，只重命名
        final_target_path = None

    # 一次性扫描所有目录的近 60s 内 .md 文件（避免第一步、第二步各 glob 整个 vault）
    now = time.time()
    existing_paths = {ef[0] for ef in existing_files}
    recent_files = []  # (path, mtime, is_new)
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        # CLIPPINGS_DIR 用 rglob：Web Clipper 偶发把含 \n 的 title 当文件名 → 畸形嵌套
        # 目录，glob("*.md") 只扫顶层会漏掉深处完好的 .md（见 id=2913）。
        # VAULT_DIR 保持 glob，避免递归扫全 vault 拖慢。
        matcher = search_dir.rglob if search_dir == CLIPPINGS_DIR else search_dir.glob
        for md_file in matcher("*.md"):
            try:
                mtime = os.path.getmtime(md_file)
            except OSError:
                continue
            if now - mtime > 60:
                continue
            if _is_verify_clipping(md_file):
                continue  # 验证页落盘，非文章，跳过认领（防错误数据）
            recent_files.append((md_file, mtime, md_file not in existing_paths))

    # 第一步：精确匹配 —— 标题与文件名互为子串（substring gate 已足够强，移除对中文无效的字符集启发式）
    candidates = [
        (f, m) for f, m, _ in recent_files
        if now - m <= 30 and (title in f.stem or f.stem in title)
        and len(f.stem) > 10 and len(title) > 10
    ]
    if candidates:
        candidates.sort(key=lambda x: x[1], reverse=True)
        md_file = candidates[0][0]
        stem = md_file.stem
        # 从文件内容提取真实发布日期（Web Clipper 保留 *YYYY年M月D日*），覆盖降级值
        try:
            file_date = extract_date_from_content(md_file.read_text(encoding="utf-8", errors="ignore"))
            if file_date and file_date != date_str:
                date_str = file_date
                target_name = f"{date_str} {sanitize_filename(title)}.md"
                if target_folder:
                    final_target_path = folder_path / target_name
        except OSError:
            pass
        if target_folder:
            if md_file != final_target_path:
                final_target_path = _non_conflicting_path(final_target_path, md_file)
                target_name = final_target_path.name
                md_file.rename(final_target_path)
                print(f"    移动: {stem[:40]}... → {target_folder}/{target_name[:50]}...")
        else:
            new_path = _non_conflicting_path(md_file.parent / target_name, md_file)
            if md_file != new_path:
                target_name = new_path.name
                md_file.rename(new_path)
                print(f"    重命名: {stem[:40]}... → {target_name[:50]}...")
        return True, date_str

    # 第二步：新文件兜底 —— 仅当恰好一个新文件时才认领；多个则歧义，不自动认领以免错配
    new_files = [(f, m) for f, m, is_new in recent_files if is_new]
    if new_files:
        if len(new_files) > 1:
            print(f"    ⚠️  发现 {len(new_files)} 个新文件，无法确定本文对应文件，跳过自动重命名")
            return False, None
        newest = new_files[0][0]
        # 从文件内容提取真实发布日期（Web Clipper 保留 *YYYY年M月D日*），覆盖降级值
        try:
            file_date = extract_date_from_content(newest.read_text(encoding="utf-8", errors="ignore"))
            if file_date and file_date != date_str:
                date_str = file_date
                target_name = f"{date_str} {sanitize_filename(title)}.md"
                if target_folder:
                    final_target_path = folder_path / target_name
        except OSError:
            pass
        if target_folder:
            if newest != final_target_path:
                final_target_path = _non_conflicting_path(final_target_path, newest)
                target_name = final_target_path.name
                newest.rename(final_target_path)
                print(f"    移动(新文件): {newest.stem[:40]}... → {target_folder}/{target_name[:50]}...")
        else:
            new_path = _non_conflicting_path(newest.parent / target_name, newest)
            if newest != new_path:
                target_name = new_path.name
                newest.rename(new_path)
                print(f"    重命名(新文件): {newest.stem[:40]}... → {target_name[:50]}...")
        return True, date_str

    return False, None


# ==================== 核心保存逻辑 ====================

def save_one_article(
    article: dict,
    browser_config: dict,
    mode: str = "quick",
    dry_run: bool = False,
    target_folder: str = None,
):
    """
    返回 (status: str, date_str: Optional[str])，status ∈ {'saved','failed','deleted'}。

    - 'saved'：文件已落盘并改名，date_str 为用于命名的 YYMMDD（调用方传给 mark_saved）
    - 'failed'：未落盘（验证页/未找到文件等可重试失败），date_str=None，下次自动重试
    - 'deleted'：文章已被发布者删除（永久不可恢复），date_str=None（调用方调 mark_deleted）
    调用方据 status 分流：saved→mark_saved，deleted→mark_deleted，failed→仅计数。
    """
    url = article["url"]
    title = article.get("title", "Unknown") or "Unknown"
    browser_app = browser_config["app"]
    shortcut_mods = browser_config["shortcut_mods"]

    if dry_run:
        date_str = extract_publish_date(url)
        folder_info = f" → {target_folder}/" if target_folder else ""
        new_name = f"{date_str} {sanitize_filename(title)}.md"
        print(f"  [DRY RUN] {title[:50]}...")
        print(f"    发布日期: {date_str}")
        print(f"    目标位置: {folder_info}{new_name[:60]}")
        return "saved", date_str

    # 1. 提取发布日期
    print(f"  提取日期...")
    date_str = extract_publish_date(url)
    print(f"    发布日期: {date_str}")

    # 记录 vault 中当前 .md 文件列表（用于后续检测新文件）
    existing_files = set()
    for d in [CLIPPINGS_DIR, VAULT_DIR]:
        if d.exists():
            # 与 find_and_rename 一致：CLIPPINGS_DIR 用 rglob，否则快照漏记深层文件，
            # 轮询时会把旧的畸形目录文件误判为"新文件"而错认领。
            matcher = d.rglob if d == CLIPPINGS_DIR else d.glob
            for f in matcher("*.md"):
                try:
                    existing_files.add((f, os.path.getmtime(f)))
                except OSError:
                    pass

    # 2. 打开文章
    print(f"  打开: {title[:50]}...")
    open_url(browser_app, url)
    time.sleep(WAIT_PAGE_LOAD)

    # 2.5 微信验证页检测 + 自动确认（风控验证页会让 quick_clip 打在空页上 → 0 落盘）
    handle_verify_page(browser_app)

    # 2.55 「文章已被发布者删除」检测：永久不可恢复，命中即短路返回，不触发 quick_clip
    #   （删除页 quick_clip 只会 0 落盘；且保持未保存会被每次运行反复打开 → failed_count 假告警）
    snap = read_page_snapshot(browser_app)
    if is_deleted_page(snap):
        print(f"    🗑️  文章已被发布者删除，标记 status='deleted' 永久跳过")
        print(f"       [自取证] title={(snap or {}).get('title')!r} "
              f"text={((snap or {}).get('text') or '')[:120]!r}")
        close_tab(browser_app)
        time.sleep(WAIT_CLOSE_TAB)
        return "deleted", None

    # 2.6 execute JS 读 #publish_time 覆盖日期（比 requests 预提取可靠；验证页/未加载则降级）
    js_date = extract_publish_date_js(browser_app)
    if js_date:
        date_str = js_date
        print(f"    发布日期(execute JS): {date_str}")

    # 3. 触发 Web Clipper
    activate_browser(browser_app)

    if mode == "quick":
        print(f"    触发 quick_clip ({'+'.join(shortcut_mods)}+{QUICK_CLIP_KEY})...")
        trigger_quick_clip(shortcut_mods)
    else:
        print(f"    触发 clipper (Cmd+Shift+{CLIPPER_KEY})...")
        trigger_clipper_and_save(shortcut_mods)

    # 4. 轮询查找新保存的文件（替代固定 sleep，修复夜间 Web Clipper 写盘慢的时序竞争）
    #    交互式通常 2-4s 落盘；launchd 夜间场景（屏幕休眠 / Chrome 后台）常 >6s，
    #    固定等待会误判"未找到"→ 文件稍后落盘滞留 Clippings。改为轮询：文件一到即认领。
    print(f"    查找并重命名（轮询等待落盘，最长 {WAIT_CLIP_TOTAL:g}s）...")
    time.sleep(WAIT_CLIP_SAVE)  # 起步窗口，再开始轮询
    renamed = False
    actual_date = None  # find_and_rename_in_vault 可能从内容提取到真实日期覆盖降级值
    deadline = time.time() + WAIT_CLIP_TOTAL
    while time.time() < deadline:
        renamed, actual_date = find_and_rename_in_vault(
            title, date_str, existing_files, target_folder=target_folder,
        )
        if renamed:
            break
        time.sleep(WAIT_CLIP_POLL)

    if not renamed:
        folder_info = f"{target_folder}/" if target_folder else ""
        print(f"    ⚠️  未找到保存的文件，可能需要手动移动到: {folder_info}{date_str} {sanitize_filename(title)}.md")

    # 5. 关闭标签页（尝试后台关闭，不激活浏览器）
    close_tab(browser_app)
    time.sleep(WAIT_CLOSE_TAB)

    # 返回 (status, date_str)：status ∈ {'saved','failed','deleted'}，二元组契约。
    # 仅当文件确实已保存并改名/移动时才算 'saved'，否则 'failed'（不 mark_saved 以便下次重试）。
    # actual_date 优先取 find_and_rename_in_vault 从文件内容提取的真实日期
    # （覆盖 extract_publish_date 降级为今天的值），让 DB published_date 与文件名一致
    if renamed:
        return "saved", (actual_date or date_str)
    return "failed", None


# ==================== 主函数 ====================

def main():
    parser = argparse.ArgumentParser(description="IMA 微信文章 → Obsidian 自动保存器")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="每次最多处理的文章数")
    parser.add_argument("--dry-run", action="store_true", help="预览模式")
    parser.add_argument(
        "--browser", choices=list(BROWSERS.keys()), default=DEFAULT_BROWSER,
        help=f"浏览器 (默认: {DEFAULT_BROWSER})",
    )
    parser.add_argument("--mode", choices=["quick", "clipper"], default="quick",
                        help="保存模式: quick=快速保存, clipper=弹窗确认")
    parser.add_argument("--des", default=None,
                        help="Obsidian 目标文件夹名称（如 AI），文件将保存到该文件夹")
    parser.add_argument("--kb", default=None,
                        help="只保存指定知识库的文章（避免不同 KB 混入同一文件夹）")
    args = parser.parse_args()

    browser_config = BROWSERS[args.browser]
    browser_app = browser_config["app"]

    print("\n" + "=" * 60)
    print("IMA 微信文章 → Obsidian 自动保存器")
    print("=" * 60)

    # fail-loud: 启动即校验 Vault 可读。glob() 遇 PermissionError 会静默返回空，
    # 无此校验时 ~/Documents 的 TCC 权限丢失会伪装成"每篇未找到文件"（曾静默故障一周）。
    try:
        next(VAULT_DIR.iterdir())
    except PermissionError:
        print(f"\n❌ 无权限读取 Obsidian Vault: {VAULT_DIR}", file=sys.stderr)
        print("   请在「系统设置 > 隐私与安全性 > 完全磁盘访问」中授权 /usr/bin/python3。", file=sys.stderr)
        print("   （~/Documents 受 TCC 保护；glob 静默吞权限错，致认领永远空、每篇误判未找到）", file=sys.stderr)
        sys.exit(1)
    except StopIteration:
        pass  # Vault 空但可读，放行

    init_database()
    stats = get_stats(args.kb)

    print(f"\n数据库统计:")
    print(f"  微信文章总数: {stats['total']}")
    print(f"  已保存到 Obsidian: {stats['saved']}")
    print(f"  待保存: {stats['unsaved']}")
    if stats.get("deleted"):
        print(f"  已删除(永久跳过): {stats['deleted']}")
    print(f"\nObsidian Vault: {VAULT_DIR}")
    if args.des:
        print(f"目标文件夹: {args.des}")
    print(f"浏览器: {browser_app}")

    mode_desc = "快速保存" if args.mode == "quick" else "弹窗确认"
    print(f"保存模式: {mode_desc}")
    if args.dry_run:
        print("运行模式: 预览 (DRY RUN)")

    if stats["unsaved"] == 0:
        print("\n✅ 没有待保存的文章")
        return

    articles = get_unsaved_articles(args.limit, args.kb)
    print(f"\n本次处理: {len(articles)} 篇\n")

    if not args.dry_run:
        print("请确保:")
        print(f"  1. {browser_app} 已安装 Obsidian Web Clipper 扩展")
        print("  2. Obsidian 应用已运行并打开了目标 Vault")
        print("  3. Web Clipper 已在扩展中连接到 Obsidian")
        print("  4. 保存期间不要操作键盘和鼠标")
        print()

        # 只在交互式终端（stdin 是 tty）时要求用户确认
        if sys.stdin.isatty():
            try:
                input("按 Enter 开始，Ctrl+C 取消...")
            except KeyboardInterrupt:
                print("\n已取消")
                return
        else:
            # 非交互模式（如从 subprocess 调用），自动继续执行
            print("⚠️  检测到非交互模式，自动开始执行...")
            print()

    saved_count = 0
    failed_count = 0
    deleted_count = 0

    for i, article in enumerate(articles, 1):
        print(f"\n[{i}/{len(articles)}]", end=" ")
        try:
            status, date_str = save_one_article(
                article, browser_config, mode=args.mode, dry_run=args.dry_run,
                target_folder=args.des
            )
            if status == "saved":
                if not args.dry_run:
                    mark_saved(article["id"], published_date=date_str)
                saved_count += 1
                print(f"    ✅ 完成")
            elif status == "deleted":
                # 文章已被发布者删除：永久跳过，不计 failed（避免触发上游告警）
                if not args.dry_run:
                    mark_deleted(article["id"])
                deleted_count += 1
                print(f"    🗑️  已删除（标记 status='deleted' 永久跳过）")
            else:  # failed
                failed_count += 1
                print(f"    ❌ 失败")
        except KeyboardInterrupt:
            print("\n\n⚠️  用户中断")
            break
        except Exception as e:
            failed_count += 1
            print(f"    ❌ 错误: {e}")
            try:
                close_tab(browser_app)
            except Exception:
                pass

        if i < len(articles):
            time.sleep(WAIT_BETWEEN)

    stats = get_stats(args.kb)
    print("\n" + "=" * 60)
    print("处理完成")
    print("=" * 60)
    print(f"  本次成功: {saved_count} 篇")
    print(f"  本次失败: {failed_count} 篇")
    print(f"  本次已删除: {deleted_count} 篇")
    print(f"  剩余待保存: {stats['unsaved']}")
    if stats.get("deleted"):
        print(f"  累计已删除(永久跳过): {stats['deleted']} 篇")

    # 退出码：让上游（incremental_update / launchd）能据失败数告警
    #   dry-run 不告警；全部失败 exit 1；部分失败 exit 2；否则 0
    #   deleted 不计入 failed（文章本身已不存在，非系统故障，不应告警）
    if not args.dry_run and failed_count > 0:
        sys.exit(1 if saved_count == 0 else 2)


if __name__ == "__main__":
    main()
