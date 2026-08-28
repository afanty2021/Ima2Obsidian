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
from typing import Optional, Set

import warnings

# 系统 Python 3.9 + LibreSSL 与 urllib3 v2 不兼容会触发 NotOpenSSLWarning，
# 污染 stderr 被 incremental_update 误冠 "错误:" 前缀。须在 import requests
# （触发 urllib3 首次导入并 warn）之前注册过滤。
# 注意：warnings.filterwarnings 的 message 是正则，用 re.match（行首锚定）匹配，
# 不是子串 search——故这里给的是告警文本的完整前缀。当前 urllib3 措辞命中、
# 全新子进程下有效（已实证 launchd 下被抑制）；若 urllib3 改写告警措辞需同步更新。
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import requests

from ima_common import (
    DB_FILE, init_database, now_saved_at, ensure_appnap_disabled, find_cliclick,
    run_cua, get_ime_source, ime_blocks_option_shortcuts,
    read_chrome_profile_info, read_web_clipper_status,
    CUA_DRIVER, is_daemon_running,
)


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
WAIT_PAGE_LOAD_MAX = 6.0    # 页面加载自适应等待上限：readyState 轮询超时仍未 complete 则睡满兜底（原固定值 6s 每篇白等一半）
WAIT_PAGE_POLL = 0.3        # readyState 轮询间隔
WAIT_PAGE_SETTLE = 0.5      # complete 后微等：innerText/#publish_time 完整渲染补齐（微信服务端直出通常已就绪）
MAX_PAGE_POLLS = 60         # readyState 轮询硬上限：垃圾返回等任何形态都不会无限打 osascript
WAIT_PUBLISH_TIME_RETRY = 2.0  # publish_time 首读为空（冷启动渲染慢）时短等后经 JS 兜底重读
WAIT_CLIP_SAVE = 1.0        # 首轮落盘轮询前的起步间隔；半成品文件由 _file_write_settled 双采样防护
                            # （原固定 4s 起步窗已被稳定性检查替代——实测成功案例首轮即命中）
WAIT_CLIP_TOTAL = 25.0     # 轮询等待文件落盘的总预算（修夜间慢盘时序竞争；交互式秒回）
WAIT_CLIP_POLL = 1.5       # 落盘轮询间隔
WAIT_CLOSE_TAB = 1.0

# ---- clipper 模式弹窗回执 + 同签名熔断（CUA observe→act→verify 理念落地）----
# 10s 而非 6s：Chrome 刚被 ensure_chrome_js_enabled 重启/冷启动时，扩展弹窗
# 首次弹出偏慢（service worker 冷唤醒），过紧会在批前几篇误触发熔断
WAIT_POPUP_APPEAR = 10.0    # 触发 ⇧⌘O 后等待剪藏器弹窗「窗口」出现的超时（秒）
WAIT_POPUP_POLL = 0.5       # 弹窗窗口轮询间隔
WAIT_AX_BUTTONS = 12.0      # 弹窗 AX 按钮就绪等待：Chromium 每轮首个弹窗需数秒开启（实测 ~6s），
                            # 一次探测即放弃会让"每轮第 1 篇"必然走回车且落盘失败（8/27-8/28 复现）；
                            # 12s 与 IMA 侧 wait_for_ax_ready 预算对齐，给冷缓存/繁忙机器留余量
WAIT_AX_BUTTONS_POLL = 0.5  # AX 按钮轮询间隔
CONSECUTIVE_FAIL_ABORT = 3  # 同签名连续失败熔断阈值（跳过剩余批次，避免整批空转）

# 弹窗确认按钮的匹配标签（小写子串匹配）。扩展弹窗 UI 目前为英文；若未来本地化，
# 在此追加对应语言标签即可（匹配不到时已有回车键兜底，不会中断流程）
ADD_BUTTON_LABELS = ("add to obsidian",)
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

    happy-path 不再单独调用（publish_time 已并入 read_page_snapshot 同一往返）；本函数
    保留为快照落空的冷启动兜底——save_one_article 短等 WAIT_PUBLISH_TIME_RETRY 后经
    此重读一次。requests 抓到的是微信精简页（无 create_time 字段，extract_publish_date
    必失败）；浏览器渲染后 #publish_time 才有发布日期。非日期文本/失败返回 None。
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
    """把文章标记为「永久不可恢复」：status 改为 'deleted'，永久跳出待保存队列。

    涵盖三类页面（行为一致，DB 不区分）：
      - 发布者删除（该内容已被发布者删除）
      - 违规不可查看（此内容因违规已删除 / 此内容因违规无法查看）
      - 账号屏蔽（此账号已被屏蔽，内容无法查看）

    与 mark_saved 不同——永久不可恢复是终态，不写 obsidian_saved（保持其 0/NULL 语义
    即「从未成功保存过」），仅改 status。所有待保存查询（get_unsaved_articles /
    get_stats / reclaim_clippings.py / ima_incremental_update.py）都用
    WHERE status='success'，故 status='deleted' 自动从这些查询消失，无需改任何 WHERE。
    不计 failed_count，避免 0 落盘的删除页触发上游告警。
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
        # deleted：status='deleted'（永久不可恢复，含发布者删除/违规/屏蔽）。与 total 同
        # url/kb 口径，但 status 维度独立——不计入 total/unsaved，单独展示有多少文章永久不可恢复。
        c.execute(
            "SELECT COUNT(*) FROM articles "
            "WHERE url LIKE '%mp.weixin.qq.com%' AND status = 'deleted'"
            + (" AND knowledge_base = ?" if kb else ""),
            params,
        )
        deleted = c.fetchone()[0]
    return {"total": total, "saved": saved, "unsaved": unsaved, "deleted": deleted}


# ==================== 浏览器自动化 ====================

def get_frontmost_app() -> str:
    """返回当前前台应用名（GUI session 诊断用）。

    launchd 后台进程可能不在用户 Aqua session 中——osascript keystroke 即使 rc=0
    也可能发到空上下文，Chrome 收不到（systematic-debugging 假设 A5）。本函数
    在 quick_clip 触发时打印前台应用，对比 launchd vs 交互式差异。

    失败返回占位字符串（不抛异常，避免污染主流程）。
    """
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of '
             'first application process whose frontmost is true'],
            capture_output=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace").strip() or "<empty>"
        err = result.stderr.decode("utf-8", errors="replace").strip()
        return f"<rc={result.returncode}: {err[:100]}>"
    except subprocess.TimeoutExpired:
        return "<timeout>"
    except Exception as e:
        return f"<exception: {type(e).__name__}: {e}>"


def activate_browser(browser_app: str):
    """激活浏览器到前台。osascript 失败时打印诊断（与 send_keystroke 一致）。

    诊断目的（systematic-debugging Phase 4）：launchd 启动的 Python 子进程可能
    被 macOS TCC 拒绝 System Events 控制，原 capture_output=True 静默吞 stderr，
    saver 误以为激活成功继续后续步骤。
    """
    result = subprocess.run(
        ["osascript", "-e", f'tell application "{browser_app}" to activate'],
        capture_output=True, timeout=5,
    )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        print(f"    ⚠️ activate_browser({browser_app!r}) 失败 "
              f"rc={result.returncode}: {err[:200]}", flush=True)
    time.sleep(0.5)


def open_url(browser_app: str, url: str):
    subprocess.run(["open", "-a", browser_app, url], capture_output=True, timeout=10)


# cliclick modifier 名称映射（saver 内部用 option/cmd/shift 等 osascript 风格，
# cliclick 用 alt/cmd/shift —— CoreGraphics CGEventPost 命名约定）
_CLICLICK_MOD_MAP = {
    "option": "alt", "alt": "alt",
    "cmd": "cmd", "command": "cmd",
    "shift": "shift",
    "ctrl": "ctrl", "control": "ctrl",
    "fn": "fn",
}


# 模块级缓存：import 时检测一次，避免每次 send_keystroke 重复 stat。
# launchd 下若未找到，send_keystroke 走提前 return 降级路径。
_CLICLICK_PATH: Optional[str] = find_cliclick()


def send_keystroke(key: str, modifiers: list = None):
    """模拟键盘事件触发 Web Clipper 等扩展快捷键。

    用 cliclick（CGEventPost，CoreGraphics）替代 osascript（Apple Event）——绕过
    macOS TCC AppleEvents 限制（osascript 报错 1002「"osascript"不允许发送按键」，
    因 osascript 完全不在 TCC 库；python3 有 Accessibility 但无 AppleEvents，
    授权不通用）。python3 已有 Accessibility TCC 授权，CGEventPost 通过
    CoreGraphics 路由，不走 Apple Event，绕过限制（systematic-debugging Phase 4）。

    ⚠️ 双层 TCC 要求（PR #7 + follow-up）：
    1. cliclick 走 CGEventPost 不走 Apple Event → 绕过 AppleEvents TCC（PR #7 修复）。
    2. **但 CGEventPost 仍需 Accessibility 授权**——launchd 启动的 cliclick 不继承
       用户 GUI session 的 Accessibility（与 iTerm 启动不同），事件会被 TCC 默默 drop
       （rc=0 但事件无效，Web Clipper 不响应）。**必须在系统设置 → 隐私与安全性 →
       辅助功能里手动添加 `/opt/homebrew/bin/cliclick`**（或 cliclick 实际安装路径，
       见 `ima_common.find_cliclick` 候选列表）。详见 SAVER.md「Web Clipper 自动化依赖」章节。

    已实证（2026-07-28 launchctl start 跑）：
    - 修复前：0 成功 / 18 失败（cliclick 未在辅助功能里）
    - 用户手动添加 cliclick 到辅助功能后：38+ 成功 / 2 失败（独立原因）

    cliclick 语法：`cliclick kd:<mods> t:<key> ku:<mods>`
    - kd: 修饰键 key down（alt/cmd/ctrl/shift/fn）
    - t: 主键（任意可打印字符，触发按键事件）
    - ku: 修饰键 key up

    已实测（systematic-debugging Phase 3）：cliclick kd:alt,shift t:o ku:alt,shift
    能触发 Web Clipper，文件自动落盘 Clippings（无需用户确认对话框）。

    路径解析（launchd 兼容）：cliclick 二进制路径在 import 时由 ima_common.find_cliclick()
    检测并缓存到 _CLICLICK_PATH。launchd 启动的进程不继承用户 shell 的 PATH
    （默认仅 /usr/bin:/bin:/usr/sbin:/sbin），故不能依赖 PATH 查找——必须用绝对
    路径调用。未找到时打印诊断并提前 return（不抛异常）。

    诊断保留：cliclick 失败时打印 returncode + stderr（与原 osascript 诊断一致，
    capture_output=True 保留以避免污染主流程 stdout）。
    """
    if not _CLICLICK_PATH:
        print(f"    ⚠️ send_keystroke 失败：cliclick 未安装（brew install cliclick）",
              flush=True)
        return

    modifiers = modifiers or []
    cliclick_mods = ",".join(_CLICLICK_MOD_MAP.get(m, m) for m in modifiers)

    cmd = [_CLICLICK_PATH]
    if cliclick_mods:
        cmd.append(f"kd:{cliclick_mods}")
    cmd.append(f"t:{key}")
    if cliclick_mods:
        cmd.append(f"ku:{cliclick_mods}")

    result = subprocess.run(cmd, capture_output=True, timeout=5)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        print(f"    ⚠️ send_keystroke(key={key!r}, mods={modifiers}) 失败 "
              f"rc={result.returncode}: {err[:200]}", flush=True)


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
                capture_output=True, text=True, timeout=10
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

    # 降级方案：快捷键关闭
    # 优先用 cliclick（CGEventPost，不走 Apple Event）——osascript keystroke 在
    # launchd 后台被 TCC 拦截（osascript 不在 TCC 库，报 errAEEventNotPermitted /
    # 退出码 1），cliclick 有 Accessibility 授权可绕过（与 send_keystroke 同理）。
    print(f"    → 尝试快捷键关闭...")
    closed = False
    if _CLICLICK_PATH:
        try:
            result = subprocess.run(
                [_CLICLICK_PATH, "kd:cmd", "t:w", "ku:cmd"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                print(f"    ✓ 标签页已关闭（cliclick）")
                closed = True
                time.sleep(0.5)
            else:
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(f"    ⚠️ cliclick 关闭失败 rc={result.returncode}: {err[:200]}")
        except subprocess.TimeoutExpired:
            print(f"    ⚠️ cliclick 执行超时")
        except Exception as e:
            print(f"    ⚠️ cliclick 异常: {e}")
    else:
        print(f"    ⚠️ cliclick 未安装（brew install cliclick），跳过")

    # 最后降级：osascript keystroke（仅交互式终端可用，launchd 下 TCC 拦截）
    if not closed and browser_app:
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to tell process "{browser_app}" to keystroke "w" using command down'],
                capture_output=True, timeout=5
            )
            if r.returncode == 0:
                print(f"    ✓ 标签页已关闭（osascript 快捷键）")
                closed = True
                time.sleep(0.5)
            else:
                err = r.stderr.decode("utf-8", errors="replace").strip()
                print(f"    ⚠️ osascript 快捷键失败 rc={r.returncode}: {err[:200]}")
        except subprocess.TimeoutExpired:
            print(f"    ⚠️ osascript 快捷键超时")
        except Exception as e:
            print(f"    ⚠️ osascript 快捷键异常: {e}")

    if not closed:
        if retry_count < max_retries:
            print(f"    → 重试关闭 ({retry_count + 1}/{max_retries})...")
            time.sleep(1)
            close_tab(browser_app, retry_count + 1)
        else:
            print(f"    ⚠️ 警告：标签页可能未关闭，请手动检查 {browser_app or '浏览器'}")


def trigger_quick_clip(mods: list):
    send_keystroke(QUICK_CLIP_KEY, mods)


def trigger_clipper_and_save(mods: list):
    """非 Chrome 浏览器的 clipper 降级路径：热键 + 固定等待 + 回车（无回执）。"""
    send_keystroke(CLIPPER_KEY, CLIPPER_MODS)
    time.sleep(2.0)
    send_keystroke("return", [])


# 最近一次失败签名（save_one_article 各失败路径写入，主循环熔断读取）：
#   popup_missing  = 剪藏器弹窗未出现（快捷键/扩展/击键送达问题）
#   file_not_found = 触发成功但 Vault 未落盘（Obsidian/Clipper 连接问题）
#   exception      = 未预期异常（main 的 except 分支写入）
_LAST_FAILURE_SIGNATURE = "unknown"


def _cua_call(tool, params, timeout=15):
    """cua-driver 调用 → dict | None。

    空 stdout 且 exit 0 视为成功（{}）；超时/非零退出/坏 JSON 返回 None，
    不抛异常（沿用提取器 run_cua_call 的容错口径，单次失败只降级不中断）。
    """
    try:
        out = run_cua(["call", tool, json.dumps(params)], timeout=timeout)
    except Exception as e:
        print(f"    ⚠️ cua-driver {tool} 失败: {e}", flush=True)
        return None
    out = (out or "").strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _chrome_windows():
    """list_windows → Chrome 窗口列表（跳过缺 window_id 的异常条目）。

    list_windows 走窗口层（CGWindow）不遍历 AX 树，不受 Chromium 渲染器
    AX「按需开启、超时关闭」的影响（get_window_state 常只回菜单栏元素），
    所以弹窗回执以「窗口集合的增删」判定，稳定可靠。
    """
    try:
        data = json.loads(run_cua(["list_windows"], timeout=10))
    except Exception:
        return []
    out = []
    for w in data.get("windows", []):
        if "chrome" not in str(w.get("app_name", "")).lower():
            continue
        if w.get("window_id") is None or w.get("pid") is None:
            continue  # 缺关键字段的脏条目，跳过避免下标崩溃
        out.append(w)
    return out


def _find_clipper_popup(baseline_ids):
    """在 Chrome 窗口中找剪藏器弹窗：基线之外新出现的窄窗口（实测约 364×554）。"""
    for w in _chrome_windows():
        if w["window_id"] in baseline_ids:
            continue
        b = w.get("bounds") or {}
        if 150 <= b.get("width", 0) <= 700:
            return w
    return None


def _ax_press_add_button(popup_win):
    """AX 路径点击弹窗里的确认按钮（语义动作优先于回车键）。

    标签按 ADD_BUTTON_LABELS 小写子串匹配（扩展 UI 本地化时追加常量即可）。
    Chromium 渲染器 AX 按需开启：每轮批处理的**第 1 个弹窗**树为空、需数秒才
    就绪（实测 ~6s）——此前"一次探测→立即回车"让每轮首篇必然落盘失败
    （8/27-8/28 三天复现，与文章内容无关、纯位置问题）。现改为在
    WAIT_AX_BUTTONS 预算内轮询；后续弹窗进程级 AX 已开启，首探即中零开销。
    超时仍未出现按钮才交回车兜底。

    返回 True=已点击；False=预算内未见按钮（或 pid/window_id 缺失）。
    """
    pid, window_id = popup_win.get("pid"), popup_win.get("window_id")
    if pid is None or window_id is None:
        return False
    deadline = time.time() + WAIT_AX_BUTTONS
    started = time.time()
    while True:
        st = _cua_call("get_window_state",
                       {"pid": pid, "window_id": window_id,
                        "include_screenshot": False})
        for el in (st or {}).get("elements", []):
            label = str(el.get("label", "")).lower()
            if (any(t in label for t in ADD_BUTTON_LABELS)
                    and "Button" in str(el.get("role", ""))):
                warmup = time.time() - started
                if warmup > 1.5:  # 首个弹窗预热：记录真实分布，供调 WAIT_AX_BUTTONS 参考
                    print(f"    ℹ️ AX 按钮等待 {warmup:.1f}s 就绪（首个弹窗预热）")
                clicked = _cua_call("click", {
                    "pid": pid, "window_id": window_id,
                    "element_index": el["element_index"],
                })
                return clicked is not None
        if time.time() >= deadline:
            return False
        time.sleep(WAIT_AX_BUTTONS_POLL)


def trigger_clipper_with_receipt():
    """clipper 模式触发（⇧⌘O），带命令层回执（CUA observe→act→verify）：

    1. 触发前记 Chrome 窗口基线，触发后轮询「新弹窗窗口」出现 → 命令已送达扩展；
    2. 弹窗出现后优先 AX 点击 'Add to Obsidian'（语义动作）——按钮搜索在
       WAIT_AX_BUTTONS 预算内轮询：Chromium 每轮**首个弹窗**的渲染器 AX 需数秒
       开启（实测 ~6s），后续弹窗进程级已开启、首探即中；
    3. 预算内未见按钮才回退回车键（最后手段）。

    Returns:
      True  = 保存动作已触发，调用方继续轮询落盘；
      False = 弹窗未出现（快捷键未注册/扩展未响应/击键被 TCC 或输入法拦截），
              写 _LAST_FAILURE_SIGNATURE='popup_missing'，调用方快速失败。
    """
    global _LAST_FAILURE_SIGNATURE

    baseline = {w["window_id"] for w in _chrome_windows()}
    send_keystroke(CLIPPER_KEY, CLIPPER_MODS)

    popup = None
    deadline = time.time() + WAIT_POPUP_APPEAR
    while time.time() < deadline:
        popup = _find_clipper_popup(baseline)
        if popup:
            break
        time.sleep(WAIT_POPUP_POLL)

    if not popup:
        print(f"    ❌ 剪藏器弹窗 {WAIT_POPUP_APPEAR:g}s 内未出现——"
              f"快捷键未注册 / 扩展未响应 / 击键未送达")
        return False

    print(f"    ✅ 剪藏器弹窗已出现（window {popup['window_id']}）")
    if _ax_press_add_button(popup):
        print("    ✅ 已 AX 点击 'Add to Obsidian'")
    else:
        print(f"    ⚠️ AX 按钮 {WAIT_AX_BUTTONS:g}s 内未就绪，回退回车键确认")
        time.sleep(1.5)
        send_keystroke("return", [])
    return True


class ConsecutiveFailureBreaker:
    """同签名连续失败熔断器（CUA「绝不盲目重放」的批处理版）。

    同一失败签名连续达到 threshold 次说明是系统性环境故障而非单篇问题
    （扩展被禁用/输入法拦截/击键被 TCC 丢弃），继续重试只会空转——
    2026-08 曾一天 94 篇 × ~90s 全失败重试 1.5 小时才被发现。
    """

    def __init__(self, threshold=CONSECUTIVE_FAIL_ABORT):
        self.threshold = threshold
        self._sig = None
        self._count = 0

    def record_success(self):
        """成功（或 deleted 这类确定性结局）重置计数。"""
        self._sig = None
        self._count = 0

    def record_failure(self, signature):
        """记录一次失败，返回是否应熔断（True=停止处理剩余文章）。"""
        if signature == self._sig:
            self._count += 1
        else:
            self._sig = signature
            self._count = 1
        return self._count >= self.threshold


def _print_failure_remediation(signature):
    """熔断时按失败签名给出可执行的排查指引（快速失败 + 可诊断）。"""
    hints = {
        "popup_missing": (
            "弹窗未出现 → 检查 chrome://extensions/shortcuts 快捷键绑定、"
            "chrome://extensions 扩展启用状态；\n"
            "      launchd 运行还需确认 /opt/homebrew/bin/cliclick 在「系统设置→隐私与安全性→辅助功能」\n"
            "      白名单内（TCC 拒绝时 cliclick rc=0 但事件被静默丢弃）"
        ),
        "file_not_found": (
            "保存动作已触发但 Vault 未落盘 → 检查 Obsidian 是否运行并打开目标 Vault、\n"
            "      剪藏器弹窗 Settings 中 Vault 连接是否指向本机 Vault"
        ),
        "exception": "查看上方错误信息",
    }
    print(f"      {hints.get(signature, '检查上方日志')}", flush=True)


# Chrome Secure Preferences 里修饰键的存储名（saver 常量 → chrome://extensions/
# shortcuts 显示名）：command→Command，option/alt→Alt（Chrome 在 mac 上称 Alt）
_CHROME_MOD_NAME = {"command": "Command", "cmd": "Command",
                    "option": "Alt", "alt": "Alt",
                    "ctrl": "Ctrl", "control": "Ctrl", "shift": "Shift"}


def _expected_chrome_binding(mods, key):
    """由 saver 实际发送的修饰键/主键推导 Chrome 里应绑定的键位串。

    键位比对必须从发送常量推导（单一事实来源）：改 saver 快捷键常量时期望值
    自动跟随，不会出现"saver 发 ⌘⇧O、预检却断言 ⌃⌘O"的口径漂移。
    """
    parts = [_CHROME_MOD_NAME.get(str(m).lower(), str(m).capitalize()) for m in mods]
    parts.append(str(key).upper())
    return "+".join(parts)


def _ensure_daemon_for_receipt():
    """确保 cua-driver daemon 运行（clipper 弹窗回执的 list_windows 依赖它）。

    增量更新流程调 saver 前已 ensure_daemon；单独跑 saver 时 daemon 可能没起，
    不处理的话每篇要白等 ~WAIT_POPUP_APPEAR 秒弹窗超时才发现。best-effort
    拉起并轮询确认；拉不起来返回 False（预检 fail-closed 处理）。
    """
    if is_daemon_running():
        return True
    print("⚠️ cua-driver daemon 未运行，尝试拉起...", flush=True)
    try:
        subprocess.Popen(
            [CUA_DRIVER, "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        print(f"❌ 拉起 daemon 失败: {e}", flush=True)
        return False
    for _ in range(8):
        time.sleep(1)
        if is_daemon_running():
            print("✅ cua-driver daemon 已拉起", flush=True)
            return True
    return False


def preflight_clipper_env(browser_app, mode, chrome_dir=None):
    """保存前环境预检（fail-closed）：扩展安装/启用/快捷键注册/输入法兼容。

    背景（2026-08）：换 Chrome 登录账号后扩展不在激活 Profile、quick_clip 的
    ⌥ 组合被中文输入法拦截——两类故障都让保存 0 落盘且 rc=0 无声，静默了两周。
    预检把环境前提变成显式断言：读得到前提但缺失 → False（main 终止），
    附带可执行修复提示；Local State 读不到（Chrome 未装等罕见场景）仅警告放行。

    Returns:
      True  = 预检通过（或无法检查但已警告）
      False = 存在硬性缺失，继续跑必然全失败
    """
    ok = True

    # cua-driver daemon：clipper 弹窗回执（list_windows 窗口基线/轮询）硬依赖它。
    # daemon 未起时每篇要白等 ~WAIT_POPUP_APPEAR 秒弹窗超时才失败，预检阶段拦下。
    if mode == "clipper" and "Chrome" in browser_app:
        if not _ensure_daemon_for_receipt():
            print("❌ 预检失败：cua-driver daemon 未运行且自动拉起失败，"
                  "弹窗回执依赖它", flush=True)
            print("   手动启动: cua-driver serve &", flush=True)
            ok = False
        else:
            print("✅ 预检：cua-driver daemon 运行中", flush=True)

    if "Chrome" in browser_app:
        prof = read_chrome_profile_info(chrome_dir)
        if not prof:
            print("⚠️ 无法读取 Chrome Profile 信息（Local State），跳过扩展预检", flush=True)
        else:
            desc = f"Profile '{prof['name']}'({prof['dir']})"
            clip = read_web_clipper_status(prof["dir"], chrome_dir)
            if not clip["installed"]:
                print(f"❌ 预检失败：当前激活 {desc} 未安装 Obsidian Web Clipper", flush=True)
                print("   扩展按 Profile 隔离，装在其他 Profile 无效（2026-08-12 换账号后", flush=True)
                print("   即因扩展不在激活 Profile 静默失败 15 天）。请在【该 Profile 的窗口】重装：", flush=True)
                print("   https://chromewebstore.google.com/detail/obsidian-web-clipper/", flush=True)
                ok = False
            elif not clip["enabled"]:
                print(f"❌ 预检失败：{desc} 的 Web Clipper 已安装但未启用", flush=True)
                print("   打开 chrome://extensions 开启该扩展", flush=True)
                ok = False
            else:
                cmd_key = "_execute_action" if mode == "clipper" else "quick_clip"
                binding = clip["commands"].get(cmd_key, "")
                expected = (_expected_chrome_binding(CLIPPER_MODS, CLIPPER_KEY)
                            if mode == "clipper"
                            else _expected_chrome_binding(["option", "shift"], QUICK_CLIP_KEY))
                if not binding:
                    print(f"❌ 预检失败：{desc} 未绑定 '{cmd_key}' 快捷键", flush=True)
                    print("   打开 chrome://extensions/shortcuts 重新绑定", flush=True)
                    ok = False
                elif binding != expected:
                    # 键位被改绑：保存器仍按常量发送旧键位 → 每篇 popup_missing，
                    # 熔断虽能 3 篇止损但当天批次已烧掉，预检阶段直接拦下
                    print(f"❌ 预检失败：{desc} 的 '{cmd_key}' 键位为 {binding}，"
                          f"但保存器发送的是 {expected}", flush=True)
                    print("   打开 chrome://extensions/shortcuts 改回键位，"
                          "或同步修改 saver 的快捷键常量", flush=True)
                    ok = False
                else:
                    print(f"✅ 预检：{desc} Web Clipper v{clip['version']}，"
                          f"{cmd_key}={binding}", flush=True)

    ime = get_ime_source()
    if mode == "quick" and ime_blocks_option_shortcuts(ime):
        print(f"❌ 预检失败：当前输入法 {ime} 会拦截 ⌥+字母 组合，", flush=True)
        print("   quick 模式(⌥⇧O)的按键永远到不了扩展。请改用 --mode clipper"
              "（⌘⇧O 不受输入法影响）", flush=True)
        ok = False
    else:
        print(f"ℹ️  输入法: {ime or '未知'}", flush=True)

    return ok


# ==================== 微信验证页检测 ====================

# 微信「当前环境异常」风控验证页特征词。saver 自动访问会间歇触发该页，导致 quick_clip
# 打在验证页上无文章内容 → 0 落盘（见 Plans/snoopy-pondering-biscuit.md）。
# Chrome execute JS 已开启，故在 quick_clip 前用 JS 检测 + 自动点「确认」。
VERIFY_KEYWORDS = ("当前环境异常", "验证后才能正常访问", "环境异常", "完成验证")
# 「去验证」按钮(id=js_verify)在验证页渲染较慢，click_confirm 首次可能落空，故重试
VERIFY_CLICK_RETRIES = 4


def ensure_chrome_js_enabled(browser_app: str = "Google Chrome") -> bool:
    """确保 Chrome「允许 Apple 事件中的 JavaScript」已开启。

    Chrome 更新后会重置此设置，导致 execute_chrome_js 全部失败（saver 0 落盘）。
    检测到关闭时通过 cua-driver 自动开启（菜单项 AX 点击不生效，须 patch 偏好，
    开启过程会退出并重启 Chrome）。非 Chrome 浏览器直接放行。

    Returns True if JS execution ready（或非 Chrome），False if enabling failed。
    """
    if browser_app != "Google Chrome":
        return True

    # 确保 Chrome 至少有一个窗口（无窗口时 osascript 报 window 错而非 JS 错，无法判断）
    try:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "{browser_app}" to if (count windows) = 0 then make new window'],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        pass

    # 测试 JS 是否可执行
    test_script = f'tell application "{browser_app}" to execute active tab of front window javascript "1"'
    try:
        r = subprocess.run(["osascript", "-e", test_script],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return True  # 已开启
    except Exception:
        pass

    # JS 执行失败 → 通过 cua-driver 自动开启
    print("⚠️  Chrome「允许 Apple 事件中的 JavaScript」未开启，正在自动开启...")
    cua_driver = Path.home() / ".local" / "bin" / "cua-driver"
    if not cua_driver.exists():
        print("❌ 未找到 cua-driver，无法自动开启。")
        print("   请手动开启：Chrome 菜单栏 → 查看 → 开发者 → 允许 Apple 事件中的 JavaScript")
        return False

    try:
        r = subprocess.run(
            [str(cua_driver), "call", "page",
             json.dumps({"action": "enable_javascript_apple_events",
                         "bundle_id": "com.google.Chrome",
                         "user_has_confirmed_enabling": True})],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            print(f"❌ cua-driver 开启失败: {(r.stderr or r.stdout).strip()}")
            return False
    except subprocess.TimeoutExpired:
        print("❌ cua-driver 开启超时")
        return False
    except Exception as e:
        print(f"❌ cua-driver 开启异常: {e}")
        return False

    print("✅ Chrome JavaScript 已开启（Chrome 正在重启）")

    # 等待 Chrome 重启（带标签页恢复时可能 >7s，单次验证会误判失败），重新创建窗口并验证
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{browser_app}" to make new window'],
            capture_output=True, text=True, timeout=5,
        )
        for i in range(5):
            time.sleep(5)
            try:
                r = subprocess.run(["osascript", "-e", test_script],
                                   capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    print(f"✅ 验证通过：Chrome JS 执行正常（第 {i + 1} 轮）")
                    return True
            except Exception:
                pass
        print(f"⚠️  开启后验证仍失败: {(r.stderr or r.stdout).strip()}")
        return False
    except Exception as e:
        print(f"⚠️  开启后验证异常: {e}")
        return False


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
    """读当前页 title + 正文前 800 字 + #publish_time，供验证页/删除判定与发布日期共用。

    publish_time 合并进同一次 AppleScript 往返（原 extract_publish_date_js 独立往返，
    happy-path 每篇省一次 osascript 调用）。失败返回 None。
    """
    js = ("JSON.stringify({title:document.title,"
          "text:(document.body&&document.body.innerText||'').slice(0,800),"
          "publish_time:(document.getElementById('publish_time')||{}).textContent||''})")
    raw = execute_chrome_js(js, browser_app)
    if not raw:
        return None
    try:
        snap = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(snap, dict) and "publish_time" not in snap:
        snap["publish_time"] = ""  # mock 注入的旧格式快照兼容
    return snap


def _href_matches_url(href: str, url: str) -> bool:
    """活动标签页 href 是否已切到目标文章。

    强判据是微信文章唯一标识 sn=<hash>：open -a 之后 Chrome 切换活动标签页有延迟，
    期间 href 停在上篇文章——而所有微信文章 path 同为 /s，仅比路径无法区分新旧页。
    无 sn 的 URL 退化为「去协议后整体前缀命中」（best-effort：目标本身就是刚交给
    Chrome 的地址，主要防的还是停在别的页）。
    """
    if not href:
        return False
    m = re.search(r"[?&]sn=([^&#]+)", url or "")
    if m:
        return f"sn={m.group(1)}" in href
    canon = re.sub(r"^https?://", "", url or "", flags=re.I)
    return bool(canon) and canon.lower() in href.lower()


def wait_page_ready(browser_app: str, max_wait: float = None,
                    require_url: str = None) -> float:
    """open_url 后轮询直到页面就绪，返回实际等待秒数。

    require_url 非空时（save_one_article 主路径），单次往返同取 readyState 和
    location.href，就绪条件为 complete 且活动标签已切到本篇——否则首轮轮询可能打在
    还没切走的上篇文章上（它必然 complete），后续快照读旧页面、删除判定有误伤风险。
    require_url 为空保持只问 readyState 的轻量行为。

    以下情况退化为睡满 max_wait（行为不劣于旧版固定等待）：非 Chrome 内核、连续
    4 次拿不到结果（权限丢失等环境异常）、轮询达 MAX_PAGE_POLLS 硬上限或时间到上限。
    """
    if max_wait is None:
        max_wait = WAIT_PAGE_LOAD_MAX
    start = time.time()
    if "Chrome" not in browser_app:
        time.sleep(max_wait)
        return max_wait
    deadline = start + max_wait
    misses = 0
    polls = 0

    def _degrade() -> float:
        remain = deadline - time.time()
        if remain > 0:
            time.sleep(remain)
        return time.time() - start

    while True:
        polls += 1
        js = ("document.readyState+'|'+location.href"
              if require_url else "document.readyState")
        raw = execute_chrome_js(js, browser_app)
        state, _, href = (raw or "").partition("|")
        ready = bool(state) and "complete" in state.lower()
        matched = True if not require_url else _href_matches_url(href, require_url)
        if ready and matched:
            time.sleep(WAIT_PAGE_SETTLE)
            return time.time() - start
        if raw is None:
            misses += 1
            if misses >= 4:  # 环境异常：不空转烧 CPU，直接把余量睡掉
                return _degrade()
        elif polls >= MAX_PAGE_POLLS:  # 有返回但迟迟不满足（含垃圾字符串空转面）
            return _degrade()
        now = time.time()
        if now >= deadline:
            return now - start
        time.sleep(min(WAIT_PAGE_POLL, deadline - now))


def extract_date_from_snapshot(snapshot: Optional[dict]) -> Optional[str]:
    """从 read_page_snapshot 的 publish_time 字段提取 YYMMDD（如 '2026年7月15日 09:56'→260715）。

    非日期文本/字段缺失返回 None，让上游维持命名兜底日期。
    """
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日",
                  (snapshot or {}).get("publish_time") or "")
    if not m:
        return None
    try:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return dt.strftime("%y%m%d")


# Web Clipper 渐进写入保护：两次采样间隔。实测文章落盘 <1s，双采样一致即可安全认领。
FILE_SETTLE_GAP = 0.35


def _file_write_settled(path: Path, gap: float = None) -> bool:
    """两次采样 size+mtime 一致才认为写盘完成，防认领半成品文件后 rename 撕裂内容。

    取代原固定 WAIT_CLIP_SAVE=4s 起步窗——稳定性检查只在目标候选出现时才付 0.35s，
    文件早已完整时总成本更低且对夜间慢盘同样安全。
    """
    if gap is None:
        gap = FILE_SETTLE_GAP
    try:
        s1 = path.stat()
    except OSError:
        return False
    time.sleep(gap)
    try:
        s2 = path.stat()
    except OSError:
        return False
    return (s1.st_size, s1.st_mtime) == (s2.st_size, s2.st_mtime)


# ==================== 渐进验证：body 长度 debug print 节流 ====================

# 已打印过的 body 长度集合：相同长度只打印一次，避免 launchd/cron 长期跑（1000 篇）
# 累积大量重复 [debug] len(body)=N 噪声（spec §5 实证语义是「采集几个不同长度样本」，
# 相同长度重复打印无信息增益）。
_DEBUG_BODY_LEN_SEEN: Set[int] = set()


# 已打印过疑似漏检自取证的 body 集合：相同 body 只打印一次（单次 run 内去重；
# 跨 launchd run 因进程重启不持久化——这是设计选择：跨 run 重复报警反而对运维友好，
# 不漏报漏检 URL）。
# key 用 body 全文（Set[str]）而非 hash(body)（Set[int]）——消除理论碰撞风险
# （PR #6 review v3 #5）。内存代价可控：只有"含 DELETED 关键词的 body"才被记录
# （合法文章不计），实测每 run < 10 条。
_POSSIBLE_MISS_SEEN: Set[str] = set()


def _log_possible_miss(body: str, url: Optional[str] = None, title: Optional[str] = None) -> None:
    """body >= `_DELETED_REASON_LEN_THRESHOLD`（100）字但含 DELETED 关键词时打自取证诊断（v7 §3.2）。

    不承诺「精准触发排除合法文章」——子串匹配无法区分合法引用整句 vs 真实漏检。
    讨论审查的媒体文章会触发噪声，已知 tradeoff（靠节流 + 接受）。
    body[:200] 截断（对 ≤200 字漏检页覆盖全文含文末 chrome；对 >200 字只截开头，
    chrome 行若在文末仍会丢失——v8 需采集更长样本判断）。

    节流：同一 body 只打一次（单次 run 内去重）。key 用 body 全文（Set[str]）而非
    hash(body)，消除理论碰撞风险（PR #6 review v3 #5）。`_POSSIBLE_MISS_SEEN.add`
    在关键词扫描**后**（仅含关键词才记录）——集合语义清晰（只含可疑漏检 body），
    CPU 影响可忽略；PR #6 review v3 决策保留此模式。

    日志含 url/title 供运维定位漏检文章（PR #6 review v3 #4）。
    不受 IMA_DEBUG_BODY_LEN 门控（PR #6 review v3 #2）——漏检诊断独立于降噪开关，
    两类日志用途相反：[debug] len(body)=N 高频低值（应门控），[疑似漏检自取证]
    低频高值（不应门控）。
    """
    hits = [k for k, _ in _DELETED_REASON_MAP if k in body]
    if not hits:
        return
    is_new = body not in _POSSIBLE_MISS_SEEN
    _POSSIBLE_MISS_SEEN.add(body)  # PR #6 review v3 #5：用 body 全文 key 消除碰撞
    if not is_new:
        return
    url_repr = repr(url) if url else "None"
    title_repr = repr(title)[:80] if title else "None"
    print(f"    [疑似漏检自取证] url={url_repr} title={title_repr} "
          f"len(body)={len(body)} 命中关键词={hits} body[:200]={body[:200]!r}")


# ==================== 永久不可恢复页判定（单源） ====================

# _deleted_reason 的 body 长度阈值：body < 此值才进入关键词匹配。
# 实测违规页最大 65 字（PR #5 首日数据），100 留 +35 余量。
# 提阈值时须同步 _log_possible_miss 调用点（save_one_article 内 if len(body) >= 此值）。
# PR #6 review #3 决策 C：接受误判风险——图片为主的文章（正文 70 字）+ 含 DELETED 关键词
# 整句会被 mark_deleted 不可回滚。实测合法文章最小 496 字，留 ~5 倍边际；[自取证] 日志
# 暴露误判（title+text 片段），运维可发现。v8 可考虑加 undelete 接口（marked_at 时间戳）。
_DELETED_REASON_LEN_THRESHOLD = 100

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
    永久不可恢复页（发布者删除 / 违规不可查看 / 账号屏蔽）。

    判定：len(body) < `_DELETED_REASON_LEN_THRESHOLD`（100）阈值 + _DELETED_REASON_MAP
    关键词子串匹配（k in body，非正则）。只查 body（snapshot['text']），不并 title——
    删除页 title 恒为「微信公众平台」不含关键词，并 title 无益；而合法文章 title 可能含
    「此账号已被屏蔽」等名词性短语（如「评此账号已被屏蔽现象」），并 title 会在慢加载
    body='' 时误杀合法文章。

    阈值是防误判的关键——合法讨论审查的文章前 800 字正文 ≫ 100，靠阈值防 mark_deleted
    永久跳过导致不可逆数据丢失。不得简化成纯关键词匹配（丢阈值 = 误杀合法文章）。
    """
    if not snapshot:
        return None
    body = snapshot.get("text") or ""    # 只查 body，不并 title（防标题误杀）
    if len(body) >= _DELETED_REASON_LEN_THRESHOLD:
        return None
    for keyword, reason in _DELETED_REASON_MAP:    # 顺序敏感：首条命中决定 reason
        if keyword in body:                         # 子串匹配（非正则）
            return reason
    return None


def is_verify_page(snapshot: Optional[dict]) -> bool:
    """判断页面快照是否为微信风控验证页（纯函数）。

    前置 _deleted_reason 排除——在 _deleted_reason 判定范围内（body < _DELETED_REASON_LEN_THRESHOLD（100）字且含
    _DELETED_REASON_MAP 关键词）的永久不可恢复页不是验证页，避免 handle_verify_page
    对屏蔽/违规页浪费 ~12-14s 重试（click_confirm 误点通用按钮 + 两轮 attempt sleep）。
    验证页 body 不含 _DELETED_REASON_MAP 关键词 → _deleted_reason 返回 None → 原逻辑不变。

    text 或 title 含验证词 → 强信号；或 title='微信公众平台' 且 text 短（<50字，验证页
    没渲染——慢加载文章 text 已有长正文时不误判，避免 click_confirm 误点正文按钮）。
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


def click_confirm(browser_app: str = "Google Chrome") -> bool:
    """点掉验证页「去验证」按钮，返回是否点到。

    优先 getElementById('js_verify')——验证页「去验证」a 的稳定 id，不依赖 selector 时机
    （实测 selector 遍历在 saver 自动跑时偶发漏点）。js_verify 不在时退回 selector 文本匹配。
    execute_chrome_js 返回 '1' 表示点到。

    JS 用 IIFE（立即执行函数表达式）而非语句序列——Chrome AppleScript `execute javascript`
    期望单一表达式，语句序列 `var v=...; if(v){v.click();'1'}else{...}` 的 '1' 没被作为
    返回值（systematic-debugging 假设 A10 修复；commit 0babc10 诊断证实 js_verify 存在但
    旧 JS 返回非 '1'）。IIFE `(function(){...})()` 明确 return，是单一表达式。

    诊断（systematic-debugging 假设 A9）：点击失败时 dump DOM 验证「去验证」按钮是否在
    iframe 内。已知矛盾：read_page_snapshot 用 body.innerText 能看到「去验证」，但
    getElementById/querySelectorAll 找不到——疑似 iframe 隔离。修复 A10 后此分支应不再
    触发（js_verify 存在 + IIFE 正确 return '1'）；若仍触发说明 A10 不完整或根因在别处。
    """
    # IIFE：单一表达式，明确 return '1' / '0'
    js_click = ("(function(){"
                "var v=document.getElementById('js_verify');"
                "if(v){v.click();return '1'}"
                "var b=[...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')];"
                "var k=['确认','继续访问','继续','确定','去验证'];"
                "for(var e of b){var t=(e.textContent||e.value||'').trim();"
                "if(k.some(function(x){return t.indexOf(x)>=0})){e.click();return '1'}}"
                "return '0'"
                "})()")
    result = execute_chrome_js(js_click, browser_app)
    if result == "1":
        return True

    # 诊断（systematic-debugging）：点击失败时 dump DOM。修复 A10 后此分支应不再触发
    # （js_verify 存在 + IIFE 正确 return '1'）；若仍触发说明 A10 不完整或根因在别处
    # result_value 用 would-click-1 不触发二次点击（避免 v.click() 副作用）
    js_diag = ("JSON.stringify({"
               "js_verify_exists: !!document.getElementById('js_verify'),"
               "result_value: (function(){var v=document.getElementById('js_verify');if(v){return 'would-click-1'}return '0'})(),"
               "iframe_count: document.querySelectorAll('iframe').length,"
               "iframe_paths: [...document.querySelectorAll('iframe')].map(f => f.src || f.getAttribute('src') || '<no-src>').slice(0, 5),"
               "iframe_texts: [...document.querySelectorAll('iframe')].map(f => { try { return ((f.contentDocument && f.contentDocument.body && f.contentDocument.body.innerText) || '').slice(0, 200) } catch(e) { return '<cross-origin>' } }),"
               "iframe_has_quyanzheng: [...document.querySelectorAll('iframe')].some(f => { try { return ((f.contentDocument && f.contentDocument.body && f.contentDocument.body.innerText) || '').indexOf('去验证') >= 0 } catch(e) { return false } }),"
               "clickable_texts: [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')].map(e => (e.textContent || e.value || '').trim()).filter(t => t).slice(0, 10),"
               "body_text_snippet: ((document.body && document.body.innerText) || '').slice(0, 200)"
               "})")
    diag_raw = execute_chrome_js(js_diag, browser_app)
    print(f"    [诊断 click_confirm 失败 DOM] {diag_raw}", flush=True)

    return False


def handle_verify_page(browser_app: str = "Google Chrome",
                       initial_snap: Optional[dict] = None) -> bool:
    """检测并处理微信验证页。返回是否遇到过验证页（True=遇到过，False=非验证页）。

    initial_snap：调用方若已持有 read_page_snapshot 快照则传入复用，首轮免一次
    AppleScript 往返；不传时行为与旧版完全一致（自行探测）。注意传 None 与不传等价。

    在 quick_clip 前调用：非验证页直接放行；验证页则自动点「确认」，最多 2 轮（应对二次
    确认）。点不掉则放弃——quick_clip 会在验证页失败，save_one_article 返回 False，
    obsidian_saved 保持 0，下次 get_unsaved_articles 自动重试，不丢数据。
    每次命中打印 title+text 片段用于自取证（迭代 VERIFY_KEYWORDS / click_confirm）。
    """
    encountered = False
    pending_snap = initial_snap
    for attempt in range(2):
        # 首轮可用调用方快照；此后页面可能因确认点击跳转，必须重新探测
        snap = pending_snap if pending_snap is not None else read_page_snapshot(browser_app)
        pending_snap = None
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


def _is_verify_clipping(md_path: Path) -> bool:
    """检测 Web Clipper 落盘的 .md 是否为验证页/删除页等干扰内容（非文章）。

    title=微信公众平台 是验证页落盘强标志（文章 title 是文章名）→ 直接判定。
    DELETED marker（整句）仅在正文短（剥 frontmatter 后 <200 字）时单命中可靠——删除页 .md 仅一句提示，
    合法文章即便引用整句正文也很长，避免误判 → 跳过认领 → 静默保存失败。
    VERIFY marker（环境异常等短词）正文也可能含 → 要求 ≥2 个同时命中，避免误伤合法文章。
    """
    try:
        txt = md_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    # 验证页落盘 frontmatter title 恒为"微信公众平台"（文章 title 是文章名）→ 直接判定。
    # 只在首个 frontmatter 块（--- ... ---）内搜，避免正文 YAML 代码块（讲 Web Clipper 等
    # 技术文章引用 frontmatter 示例）误判 → 跳过认领 → 静默保存失败。
    fm = re.match(r'^---\s*\n(.*?)\n---', txt, re.DOTALL)
    fm_text = fm.group(1) if fm else ""
    # 兼容 YAML 引号变体（双引号/单引号/无引号；纯中文 title 常态无引号）
    if re.search(r'^title:\s*["\']?微信公众平台["\']?\s*$', fm_text, re.MULTILINE):
        return True
    # 删除页 .md 仅一句提示（正文很短）；合法文章即便正文引用整句，.md 正文也很长 →
    # 要求正文短才判删除页落盘，避免误伤引用整句的合法文章（静默跳过认领 = 永久卡队列）。
    # 剥 frontmatter 后算长度：真实 Web Clipper frontmatter（source URL 150+字 + author +
    # published + tags）已 ~200 字，含 frontmatter 算长度会让 path ② 永不触发（fix #4）。
    body_text = txt[fm.end():] if fm else txt
    if len(body_text) < 200 and any(k in txt for k in DELETED_CLIPPING_MARKERS):
        return True
    return sum(1 for k in VERIFY_CLIPPING_MARKERS if k in txt) >= 2


def find_and_rename_in_vault(
    title: str,
    date_str: str,
    existing_files: set,
    search_dirs: list = None,
    target_folder: str = None,
    require_stable: bool = False,
):
    """
    在 Obsidian vault 中找到 Web Clipper 刚保存的文件，
    重命名为 YYMMDD title.md 格式，并移动到目标文件夹。

    existing_files: set of (Path, mtime) tuples captured before opening article
    target_folder: 目标文件夹名称（如 "AI"），如果为 None 则保持在原位置
    require_stable: True 时认领前用 _file_write_settled 双采样确认写盘完成，
      半成品文件本轮跳过由外层轮询重试（save_one_article 传入；reclaim 等其他调用方不传保持旧行为）

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
        if require_stable and not _file_write_settled(md_file):
            print("    ⏳ 候选文件仍在写盘，本轮跳过待下轮轮询")
            return False, None
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
        if require_stable and not _file_write_settled(newest):
            print("    ⏳ 新文件仍在写盘，本轮跳过待下轮轮询")
            return False, None
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
    - 'deleted'：永久不可恢复页（发布者删除/违规不可查看/账号屏蔽），date_str=None（调用方调 mark_deleted）
    调用方据 status 分流：saved→mark_saved，deleted→mark_deleted，failed→仅计数。
    """
    global _LAST_FAILURE_SIGNATURE
    _LAST_FAILURE_SIGNATURE = "unknown"

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

    # 1. 命名兜底日期——不再 requests 预取：微信精简页对四种正则必失配（旧日志每篇
    #    「未匹配到发布日期正则」都是这次白付的 HTTP，网络抖动还会触发指数退避）。
    #    真实日期由打开页面后的 snapshot.publish_time 与文件内容 *YYYY年M月D日* 双级覆盖；
    #    全部失败仍回落今日（与旧行为一致）。dry-run 预览仍走 extract_publish_date。
    date_str = datetime.now().strftime("%y%m%d")
    print(f"  提取日期...")
    print(f"    命名兜底日期: {date_str}（真实发布日期待页面/内容覆盖）")

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
    waited = wait_page_ready(browser_app, require_url=url)
    if waited >= WAIT_PAGE_LOAD_MAX - 0.05:
        # 达上限才告警：readyState 提前 complete 时这里静默通过（省时的主路径）
        print(f"    ⚠️ 页面就绪等待达上限 {waited:.1f}s"
              f"（AppleScript 失败/慢渲染/标签页未切换），降级继续")

    # 2.5 快照一次到手：验证页检测、删除判定、发布日期共用同一次 AppleScript 往返
    #   （原链路 handle_verify_page 自探测 + 独立 read_page_snapshot + extract_publish_date_js
    #    共三次往返；合并 + URL 守卫保证读到的是本篇后，happy-path 真正只此一次）
    snap = read_page_snapshot(browser_app)
    # 2.5a 微信验证页检测 + 自动确认（风控验证页会让 quick_clip 打在空页上 → 0 落盘）
    #   is_verify_page 前置 _deleted_reason 排除，屏蔽/违规页不会被误判为验证页 → 不浪费重试
    if handle_verify_page(browser_app, initial_snap=snap):
        snap = read_page_snapshot(browser_app)  # 点确认跳转后复读真页面

    # 2.55 永久不可恢复页检测（发布者删除 / 违规不可查看 / 账号屏蔽）：命中即短路返回，不触发 quick_clip
    #   （此类页 quick_clip 只会 0 落盘；保持未保存会被每次运行反复打开 → failed_count 假告警）
    body = (snap or {}).get("text") or ""  # PR #6 review #5：提取一次，下游复用
    # 渐进验证：<_DELETED_REASON_LEN_THRESHOLD 字阈值对真实屏蔽/违规页是否有效（spec §5 + v7 §3.1）
    # 默认开启；运维嫌吵可设 IMA_DEBUG_BODY_LEN=0/false/no/off 关闭
    # TODO(渐进验证)：首篇屏蔽/违规 URL 命中后，根据日志确认 len(body) 真实长度；
    #   若 ≥阈值过紧需调整 _deleted_reason；若确认阈值有效，移除此 print 与门控
    _debug_body_len_enabled = os.environ.get("IMA_DEBUG_BODY_LEN", "1").lower() not in ("0", "false", "no", "off", "")  # PR #6 review #4：提取一次避免复制粘贴漂移
    if _debug_body_len_enabled:
        body_len = len(body)
        if body_len not in _DEBUG_BODY_LEN_SEEN:
            _DEBUG_BODY_LEN_SEEN.add(body_len)
            print(f"    [debug] len(body)={body_len}")
    reason = _deleted_reason(snap)
    if reason is not None:
        print(f"    🗑️  {reason}，标记 status='deleted' 永久跳过")
        print(f"       [自取证] title={(snap or {}).get('title')!r} "
              f"text={body[:120]!r}")
        close_tab(browser_app)
        time.sleep(WAIT_CLOSE_TAB)
        return "deleted", None

    # v7：漏检自取证——_deleted_reason 返回 None 但 body>=阈值，调 _log_possible_miss
    #     （调用点显式判；不在 _deleted_reason 内部——避免 is_verify_page 双调用时重复打印）
    # PR #6 review v3 #1 #2：_log_possible_miss 调用移出 IMA_DEBUG_BODY_LEN 门控——
    #     两类日志用途相反（[debug] len(body)=N 高频低值 vs [疑似漏检自取证] 低频高值），
    #     不应共享开关；运维降噪时漏检诊断证据源不能丢。
    if len(body) >= _DELETED_REASON_LEN_THRESHOLD:
        _log_possible_miss(body, url=url, title=title)

    # 2.6 发布日期（同一快照内读 #publish_time，免单独 osascript 往返）。
    #     冷启动渲染慢时快照可能取到空 publish_time——短等后经 extract_publish_date_js
    #     兜底重读一次；两级都落空必须显式告警，否则文件以今日命名且无任何信号。
    js_date = extract_date_from_snapshot(snap)
    date_source = "publish_time"
    if js_date is None and "Chrome" in browser_app:
        time.sleep(WAIT_PUBLISH_TIME_RETRY)
        js_date = extract_publish_date_js(browser_app)
        date_source = "publish_time 重试"
    if js_date:
        date_str = js_date
        print(f"    发布日期({date_source}): {date_str}")
    else:
        print("    ⚠️ 真实发布日期未取到（快照与 JS 兜底皆空），先按今日命名兜底；"
              "文件内容含 *YYYY年M月D日* 时重命名阶段仍会覆盖")

    # 3. 触发 Web Clipper
    activate_browser(browser_app)

    if mode == "quick":
        print(f"    触发 quick_clip ({'+'.join(shortcut_mods)}+{QUICK_CLIP_KEY})...")
        # 诊断（systematic-debugging 假设 A5）：打印当前前台应用，验证 launchd 跑时
        # Chrome 是否真的获得焦点。预期：交互式跑 → 'Google Chrome'；launchd 跑若
        # 返回其他值或 <empty>，则 A5 成立（GUI session 隔离）。
        print(f"    [诊断] quick_clip 触发时前台应用={get_frontmost_app()!r}", flush=True)
        trigger_quick_clip(shortcut_mods)
    elif "Chrome" in browser_app:
        print(f"    触发 clipper (Cmd+Shift+{CLIPPER_KEY})...")
        if not trigger_clipper_with_receipt():
            # 弹窗未出现：命令层未响应，文件轮询必然空等，快速失败止损
            _LAST_FAILURE_SIGNATURE = "popup_missing"
            close_tab(browser_app)
            time.sleep(WAIT_CLOSE_TAB)
            return "failed", None
    else:
        print(f"    触发 clipper (Cmd+Shift+{CLIPPER_KEY})...")
        trigger_clipper_and_save(shortcut_mods)

    # 4. 轮询查找新保存的文件（替代固定 sleep，修复夜间 Web Clipper 写盘慢的时序竞争）
    #    交互式通常 2-4s 落盘；launchd 夜间场景（屏幕休眠 / Chrome 后台）常 >6s，
    #    固定等待会误判"未找到"→ 文件稍后落盘滞留 Clippings。改为轮询：文件一到即认领。
    print(f"    查找并重命名（轮询等待落盘，最长 {WAIT_CLIP_TOTAL:g}s）...")
    time.sleep(WAIT_CLIP_SAVE)  # 起步间隔仅 1s：半成品由 require_stable 双采样防护
    renamed = False
    actual_date = None  # find_and_rename 可能从内容提取到真实日期覆盖降级值
    deadline = time.time() + WAIT_CLIP_TOTAL
    while time.time() < deadline:
        renamed, actual_date = find_and_rename_in_vault(
            title, date_str, existing_files, target_folder=target_folder,
            require_stable=True,
        )
        if renamed:
            break
        time.sleep(WAIT_CLIP_POLL)

    if not renamed:
        folder_info = f"{target_folder}/" if target_folder else ""
        print(f"    ⚠️  未找到保存的文件，可能需要手动移动到: {folder_info}{date_str} {sanitize_filename(title)}.md")
        _LAST_FAILURE_SIGNATURE = "file_not_found"

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
    parser.add_argument("--skip-reclaim", action="store_true",
                        help="跳过启动时 reclaim（由增量更新流程用于避免重复扫描）")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="跳过环境预检（扩展安装/启用/快捷键/输入法）")
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

    # review PR#11 #3：saver 独立运行时也确保 AppNap 禁用（不依赖 incremental_update 预调）
    ensure_appnap_disabled()

    # Chrome「允许 Apple 事件中的 JavaScript」检查：Chrome 更新后会重置此设置，
    # 导致 execute_chrome_js 全部失败（saver 0 落盘）。检测到关闭时自动通过
    # cua-driver 开启。非交互模式下尤其关键（无法人工干预）。
    if not args.dry_run:
        if not ensure_chrome_js_enabled(browser_app):
            print("❌ Chrome JS 执行不可用，保存将全部失败，终止。", file=sys.stderr)
            sys.exit(1)

    # 环境预检（fail-closed）：扩展安装/启用/快捷键注册/输入法兼容。
    # dry-run 也执行（信息性输出）但仅在实跑时阻断；--skip-preflight 可强制跳过。
    if not args.skip_preflight:
        preflight_ok = preflight_clipper_env(browser_app, args.mode)
        if preflight_ok is False and not args.dry_run:
            print("❌ 环境预检未通过，继续跑必然全失败，终止。"
                  "（修复后重试，或 --skip-preflight 强制运行）", file=sys.stderr)
            sys.exit(1)

    init_database()

    if not args.skip_reclaim:
        # reclaim 兜底：subprocess 调 reclaim_clippings.py 认领滞留文件
        # （review v4 #1 方案 A：subprocess 避免 saver↔reclaim_clippings 循环引用）
        # review v4 #2 dry-run 门控
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
            if _proc.returncode != 0:
                print("⚠️ reclaim 异常退出（code={}），检查以下输出".format(_proc.returncode))
            if _proc.stdout:
                print(_proc.stdout, end="")
            if _proc.stderr:
                print(_proc.stderr, end="", file=sys.stderr)
            # 解析 RECLAIM_RESULT JSON 行
            _reclaim_stats = {}
            for _line in (_proc.stdout or "").splitlines():
                if _line.startswith("RECLAIM_RESULT: "):
                    try:
                        _reclaim_stats = json.loads(_line[len("RECLAIM_RESULT: "):])
                    except json.JSONDecodeError:
                        pass
                    break
            # reclaim stdout 已含汇总，saver 只补充需要关注的异常信息。
            if _reclaim_stats.get("batch_corrupt_skipped"):
                print("跳过 {} 个批量错乱副本".format(_reclaim_stats["batch_corrupt_skipped"]))
            for _item in _reclaim_stats.get("rollback_failures") or []:
                print("⚠️ reclaim 回滚失败（文件位置不可知）：{}".format(_item))
            if _reclaim_stats.get("aborted"):
                print("⚠️ reclaim 中止：{}（剩余滞留下次再认领）".format(_reclaim_stats["aborted"]))

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
    aborted_by_breaker = False
    breaker = ConsecutiveFailureBreaker()

    def _abort_batch(sig):
        """同签名连续失败熔断：打印指引并终止本批（剩余文章留待下次运行）。"""
        print(f"\n⚠️  熔断：连续 {CONSECUTIVE_FAIL_ABORT} 篇同签名失败（{sig}），"
              f"为系统性故障，跳过剩余文章")
        _print_failure_remediation(sig)

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
                breaker.record_success()
            elif status == "deleted":
                # 永久不可恢复页（发布者删除/违规/屏蔽）：永久跳过，不计 failed（避免触发上游告警）
                if not args.dry_run:
                    mark_deleted(article["id"])
                deleted_count += 1
                print(f"    🗑️  已删除（标记 status='deleted' 永久跳过）")
                breaker.record_success()  # 确定性结局，重置熔断计数
            else:  # failed
                failed_count += 1
                print(f"    ❌ 失败")
                if breaker.record_failure(_LAST_FAILURE_SIGNATURE):
                    aborted_by_breaker = True
                    _abort_batch(_LAST_FAILURE_SIGNATURE)
                    break
        except KeyboardInterrupt:
            print("\n\n⚠️  用户中断")
            break
        except Exception as e:
            failed_count += 1
            print(f"    ❌ 错误: {e}")
            # 先关标签页再判熔断——熔断 break 后这篇的残留标签页无人清理
            try:
                close_tab(browser_app)
            except Exception:
                pass
            if breaker.record_failure("exception"):
                aborted_by_breaker = True
                _abort_batch("exception")
                break

        if i < len(articles):
            time.sleep(WAIT_BETWEEN)

    stats = get_stats(args.kb)
    print("\n" + "=" * 60)
    print("处理完成")
    print("=" * 60)
    print(f"  本次成功: {saved_count} 篇")
    print(f"  本次失败: {failed_count} 篇")
    if aborted_by_breaker:
        print(f"  ⚠️  本批已熔断（连续 {CONSECUTIVE_FAIL_ABORT} 篇同签名失败），"
              f"剩余文章留待修复环境后重试")
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
