#!/usr/bin/env python3
"""
IMA AI 知识库文章 URL 提取器 — AX Tree 版本

基于 cua-driver daemon + get_window_state + element_index 的精确提取方案。
利用 AX 树中文章卡片的固定结构（标题 + "公众号" + 作者名）精确识别文章，
通过 element_index + AXPress 点击打开文章，AXDocument 提取 URL。

依赖:
  - cua-driver daemon 运行中 (cua-driver serve)
  - IMA (ima.copilot) 已打开并位于 AI 知识库列表页
  - 辅助功能权限已授权
"""

import argparse
import asyncio
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ==================== 配置 ====================

DB_FILE = Path(__file__).parent / "ima_articles.db"
CUA_DRIVER = "/Users/berton/.local/bin/cua-driver"
IMA_APP_NAME = "ima.copilot"

WAIT_CLICK_LOAD = 3.0
WAIT_AFTER_CLOSE = 1.5
WAIT_SCROLL = 2.0
WAIT_ACTIVATE = 0.5

MAX_PAGES = 65
MAX_CONSECUTIVE_SEEN = 40 # 连续遇到已存在文章后停止


# ==================== 数据库 ====================

def init_database():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            knowledge_base TEXT,
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            y_position INTEGER,
            status TEXT DEFAULT 'success'
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_url ON articles(url)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_kb ON articles(knowledge_base)")
    conn.commit()
    conn.close()


def url_exists(url: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM articles WHERE url = ? LIMIT 1", (url,))
    exists = c.fetchone() is not None
    conn.close()
    return exists


def save_article(url: str, title: str, kb: str) -> bool:
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            INSERT OR IGNORE INTO articles (url, title, knowledge_base, status)
            VALUES (?, ?, ?, 'success')
        """, (url, title, kb))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"  ⚠️  保存失败: {e}")
        return False


def get_stats() -> Dict:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM articles")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT knowledge_base) FROM articles")
    kb_count = c.fetchone()[0]
    conn.close()
    return {"total": total, "kb_count": kb_count}


# ==================== cua-driver ====================

def run_cua(args: List[str], timeout: int = 30) -> str:
    cmd = [CUA_DRIVER] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"cua-driver failed: {result.stderr.strip() or f'exit {result.returncode}'}")
    return result.stdout


def run_cua_call(tool: str, params: Dict) -> Optional[Dict]:
    """调用 cua-driver tool。click 等命令返回纯文本而非 JSON，统一处理。"""
    try:
        output = run_cua(["call", tool, json.dumps(params)])
        if output.strip():
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                # click 等命令返回纯文本，视为成功
                return {"raw": output.strip()}
    except RuntimeError as e:
        print(f"  ⚠️  cua-driver call {tool} 失败: {e}")
    return None


def is_daemon_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "cua-driver serve"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def get_ima_main_window() -> Optional[Dict]:
    try:
        output = run_cua(["list_windows"])
        data = json.loads(output)
    except Exception as e:
        print(f"  ❌ list_windows 失败: {e}")
        return None

    windows = data.get("windows", [])
    ima_windows = [
        w for w in windows
        if IMA_APP_NAME.lower() in w.get("app_name", "").lower()
        and w.get("bounds", {}).get("height", 0) > 400
    ]

    if not ima_windows:
        return None

    # 选最大的窗口（主窗口）
    return max(ima_windows, key=lambda w: w["bounds"].get("width", 0) * w["bounds"].get("height", 0))


def get_window_state(pid: int, window_id: int) -> Optional[Dict]:
    return run_cua_call("get_window_state", {"pid": pid, "window_id": window_id})


def click_element(pid: int, window_id: int, element_index: int) -> bool:
    result = run_cua_call("click", {
        "pid": pid,
        "window_id": window_id,
        "element_index": element_index
    })
    return result is not None


def scroll_down(pid: int, window_id: int, amount: int = 3):
    run_cua_call("scroll", {
        "pid": pid,
        "window_id": window_id,
        "direction": "down",
        "amount": amount
    })


# ==================== AppleScript ====================

def activate_ima():
    subprocess.run(
        ["osascript", "-e", 'tell application "ima.copilot" to activate'],
        capture_output=True, timeout=5
    )
    time.sleep(WAIT_ACTIVATE)


def extract_url_ax() -> Optional[str]:
    """从 AXDocument 属性提取当前文章 URL"""
    script = '''
tell application "System Events"
    tell process "ima.copilot"
        set wCount to count of windows
        repeat with i from 1 to wCount
            try
                set docUrl to value of attribute "AXDocument" of window i
                if docUrl is not missing value and docUrl starts with "http" then
                    return docUrl
                end if
            end try
        end repeat
    end tell
end tell
return ""
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5
        )
        url = result.stdout.strip()
        if url and url.startswith("http") and "chrome://" not in url:
            return url
    except Exception:
        pass
    return None


def extract_title_ax() -> Optional[str]:
    """从窗口标题提取文章标题"""
    script = '''
tell application "System Events"
    tell process "ima.copilot"
        set wCount to count of windows
        repeat with i from 1 to wCount
            try
                set wTitle to title of window i
                if wTitle is not "" and wTitle does not contain "ima.copilot" then
                    return wTitle
                end if
                if wTitle contains "AI" then
                    return ""
                end if
            end try
        end repeat
    end tell
end tell
return ""
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5
        )
        title = result.stdout.strip()
        return title if title else None
    except Exception:
        return None


def get_kb_window_title(kb_name: str = "") -> str:
    """获取知识库窗口标题"""
    if kb_name:
        # 查找包含指定知识库名称的窗口
        script = f'''
tell application "System Events"
    tell process "ima.copilot"
        set wCount to count of windows
        repeat with i from 1 to wCount
            try
                set wTitle to title of window i
                if wTitle contains "{kb_name}" then
                    return wTitle
                end if
            end try
        end repeat
    end tell
end tell
return ""
'''
    else:
        # 查找任意包含 "AI" 或 "知识库" 的窗口（降级方案）
        script = '''
tell application "System Events"
    tell process "ima.copilot"
        set wCount to count of windows
        repeat with i from 1 to wCount
            try
                set wTitle to title of window i
                if wTitle contains "AI" or wTitle contains "知识库" then
                    return wTitle
                end if
            end try
        end repeat
    end tell
end tell
return ""
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except Exception:
        return ""


def cmd_w_close():
    """Cmd+W 关闭当前文章标签页"""
    subprocess.run(
        ["osascript", "-e", 'tell application "ima.copilot" to activate'],
        capture_output=True, timeout=5
    )
    time.sleep(0.3)
    subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to keystroke "w" using command down'],
        capture_output=True, timeout=5
    )


# ==================== 文章识别 ====================

def parse_articles_from_tree(state: Dict, kb_name: str = "") -> List[Dict]:
    """
    从 tree_markdown 解析文章列表。

    支持两种知识库类型：
    1. AI 知识库（共享）：indent=44
    2. 个人知识库：indent=40

    文章卡片结构特征：
      AXImage (缩略图)
      AXGroup > AXGroup > AXStaticText = "文章标题"
      AXGroup > AXImage (公众号图标)
      AXStaticText = "公众号"
      AXGroup > AXStaticText (作者名)

    通过检测 "公众号" 标记来精确识别文章卡片。
    """
    markdown = state.get("tree_markdown", "")
    if not markdown:
        return []

    lines = markdown.split("\n")
    articles = []

    # 找所有 AXStaticText 及其索引和文本
    static_texts = []  # (line_idx, element_index, text, indent)
    for i, line in enumerate(lines):
        m = re.search(r'\[(\d+)\] AXStaticText = "(.+?)"', line)
        if m:
            elem_idx = int(m.group(1))
            text = m.group(2)
            indent = len(line) - len(line.lstrip())
            static_texts.append((i, elem_idx, text, indent))

    # 检测知识库类型：查找第一个 "公众号" 标记的 indent
    target_indent = None
    for _, elem_idx, text, indent in static_texts:
        if text == "公众号":
            target_indent = indent
            break

    if target_indent is None:
        # 未找到 "公众号" 标记，返回空
        return []

    # 遍历查找 "公众号" 标记，回溯找标题
    for j, (line_idx, elem_idx, text, indent) in enumerate(static_texts):
        if text != "公众号":
            continue

        # 回溯找最近的 indent=target_indent 的 AXStaticText（文章标题）
        title_elem = None
        for k in range(j - 1, max(0, j - 5), -1):
            _, e_idx, t, ind = static_texts[k]
            if ind == target_indent and t != "公众号" and len(t) > 10:
                # 排除已知的非标题文本
                exclude_titles = {"皮皮鲁", kb_name} if kb_name else {"皮皮鲁"}
                if t not in exclude_titles and not re.match(r'^\d{4}年', t):
                    title_elem = (e_idx, t)
                    break

        if title_elem:
            e_idx, t = title_elem
            # 去重：相同 element_index 不重复添加
            if not any(a["element_index"] == e_idx for a in articles):
                articles.append({
                    "element_index": e_idx,
                    "title": t
                })

    return articles


# ==================== 核心提取逻辑 ====================

async def extract_articles(pid: int, window_id: int, kb_name: str = "AI"):
    print("\n" + "=" * 60)
    print(f"开始批量提取（{kb_name} 知识库）")
    print("=" * 60)

    total_new = 0
    total_skipped = 0
    total_failed = 0
    consecutive_seen = 0
    processed_titles: Set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        print(f"\n{'─' * 50}")
        print(f"第 {page} 页")
        print(f"{'─' * 50}")

        # 获取窗口状态（先尝试不激活，减少干扰）
        state = get_window_state(pid, window_id)
        if not state or state.get("element_count", 0) < 100:
            # 失败或元素过少，可能是窗口在其他 Space，激活后重试
            activate_ima()
            state = get_window_state(pid, window_id)
            if not state:
                print("  ❌ 无法获取窗口状态")
                break

        elem_count = state.get("element_count", 0)
        if elem_count < 100:
            print(f"  ⚠️  元素数过少 ({elem_count})，可能窗口不在当前 Space")
            break

        # 解析文章列表
        articles = parse_articles_from_tree(state, kb_name)
        print(f"  识别到 {len(articles)} 篇文章")

        if not articles:
            print("  ⚠️  未找到文章，可能已到列表底部")
            break

        page_new = 0
        page_skipped = 0

        should_stop = False

        for i, article in enumerate(articles, 1):
            elem_idx = article["element_index"]
            title = article["title"]

            # 去重
            if title in processed_titles:
                continue
            processed_titles.add(title)

            print(f"\n  [{i}] {title[:60]}... (element {elem_idx})")

            # 不要在此调用 get_window_state！
            # element_index 缓存会在下次 get_window_state 时被替换，
            # 必须用本次页面解析时的缓存来点击。
            # cua-driver 支持后台点击，无需激活 IMA（减少干扰用户）

            # 点击文章（使用当前缓存中的 element_index）
            print(f"    点击文章 (element {elem_idx})...")
            if not click_element(pid, window_id, elem_idx):
                print("    ❌ 点击失败，刷新状态重试...")
                # 点击失败时才重新获取状态并重新解析
                state = get_window_state(pid, window_id)
                if state and state.get("element_count", 0) > 100:
                    # 缓存已刷新，无法继续用旧索引，跳出本页
                    print("    ⚠️  索引已失效，跳到下一页")
                break

            # 等待加载
            await asyncio.sleep(WAIT_CLICK_LOAD)

            # 提取 URL
            url = extract_url_ax()

            if not url:
                print("    ⚠️  未提取到 URL")
                total_failed += 1
                cmd_w_close()
                await asyncio.sleep(WAIT_AFTER_CLOSE)
                continue

            print(f"    ✅ URL: {url[:80]}...")

            if url_exists(url):
                print("    ℹ️  已存在，跳过")
                total_skipped += 1
                page_skipped += 1
                consecutive_seen += 1

                if consecutive_seen >= MAX_CONSECUTIVE_SEEN:
                    print(f"\n  ⚠️  连续 {consecutive_seen} 篇已存在，可能已全部提取")
                    cmd_w_close()
                    await asyncio.sleep(WAIT_AFTER_CLOSE)
                    should_stop = True
                    break
            else:
                title_extracted = extract_title_ax()
                final_title = title_extracted or title
                print(f"    ✅ 标题: {final_title[:60]}...")

                save_article(url, final_title, kb_name)
                total_new += 1
                page_new += 1
                consecutive_seen = 0
                print(f"    ✅ 新文章已保存 (总计: {total_new})")

            # 关闭文章标签页，返回列表
            cmd_w_close()
            await asyncio.sleep(WAIT_AFTER_CLOSE)

        if should_stop:
            break

        print(f"\n  本页完成: 新增 {page_new}, 跳过 {page_skipped}")

        # 滚动加载更多
        print("  滚动加载更多...")
        for _ in range(10):
            scroll_down(pid, window_id, 3)
            time.sleep(0.1)
        await asyncio.sleep(WAIT_SCROLL)

    # 总结
    stats = get_stats()
    print("\n" + "=" * 60)
    print("提取完成")
    print("=" * 60)
    print(f"  本次新增: {total_new} 篇")
    print(f"  本次跳过: {total_skipped} 篇")
    print(f"  本次失败: {total_failed} 篇")
    print(f"  数据库总计: {stats['total']} 篇 ({stats['kb_count']} 个知识库)")


# ==================== 主函数 ====================

async def main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description="IMA 知识库文章 URL 提取器 — AX Tree 版本"
    )
    parser.add_argument(
        "--src",
        default="AI",
        help="IMA 知识库名称（默认: AI）"
    )
    args = parser.parse_args()

    kb_name = args.src

    print("\n" + "=" * 60)
    print(f"IMA {kb_name} 知识库提取器（AX Tree 版本）")
    print("=" * 60)
    print()

    # 初始化数据库
    init_database()
    stats = get_stats()
    print(f"✅ 数据库: {DB_FILE} (已有 {stats['total']} 篇)")

    # 检查 daemon
    if not is_daemon_running():
        print("❌ cua-driver daemon 未运行")
        print("   请先启动: cua-driver serve &")
        sys.exit(1)
    print("✅ cua-driver daemon 运行中")

    # 获取 IMA 窗口
    print(f"\n查找 IMA 窗口（{kb_name} 知识库）...")
    activate_ima()
    time.sleep(0.5)

    window = get_ima_main_window()
    if not window:
        print("❌ 未找到 IMA 窗口")
        sys.exit(1)

    pid = window["pid"]
    window_id = window["window_id"]
    bounds = window.get("bounds", {})
    print(f"✅ 窗口: PID={pid}, window_id={window_id}, {bounds.get('width')}x{bounds.get('height')}")

    # 验证在指定知识库
    title = get_kb_window_title(kb_name)
    print(f"\n当前窗口标题: {title}")

    if kb_name in title:
        print(f"✅ 确认在 {kb_name} 知识库列表页")
    else:
        print(f"⚠️  窗口标题不包含 '{kb_name}'")
        print(f"   请确保 IMA 已打开 {kb_name} 知识库列表页")
        print("   继续尝试提取...")

    # 获取初始状态验证
    state = get_window_state(pid, window_id)
    if not state:
        print("❌ 无法获取窗口状态")
        sys.exit(1)

    elem_count = state.get("element_count", 0)
    print(f"✅ AX 树元素数: {elem_count}")

    if elem_count < 100:
        print("⚠️  元素数过少，可能窗口不在当前 Space 或权限不足")
        sys.exit(1)

    # 测试解析
    articles = parse_articles_from_tree(state, kb_name)
    if not articles:
        print("⚠️  未识别到文章卡片")
        print(f"   请确认当前在 {kb_name} 知识库列表页且列表中有文章")
        sys.exit(1)

    print(f"✅ 识别到 {len(articles)} 篇文章，准备提取")

    # 开始提取
    await extract_articles(pid, window_id, kb_name)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
