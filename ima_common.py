#!/usr/bin/env python3
"""
IMA 公共模块 — 共享函数和工具

包含在多个脚本中重复使用的函数，避免代码重复。
"""

import json
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime
from pathlib import Path

# ==================== 配置 ====================

CUA_DRIVER = "/Users/berton/.local/bin/cua-driver"
IMA_APP_NAME = "ima.copilot"
DB_FILE = Path(__file__).parent / "ima_articles.db"


def now_saved_at() -> str:
    """
    返回 obsidian_saved_at 列的统一时间戳格式：ISO 8601，秒精度，T 分隔符。

    所有写入 obsidian_saved_at 的代码（saver.mark_saved、reclaim_clippings）
    都应使用本函数，保证：
      - 跨写者格式一致（无 T 与 空格 混存）
      - 可被 datetime.fromisoformat() 在所有 Python 3.7+ 解析
      - 字典序与时间序一致（便于 ORDER BY 字符串列）
    """
    return datetime.now().isoformat(timespec="seconds")


# ==================== cua-driver ====================

def run_cua(args, timeout: int = 30) -> str:
    """运行 cua-driver 命令"""
    cmd = [CUA_DRIVER] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"cua-driver failed: {result.stderr.strip() or f'exit {result.returncode}'}")
    return result.stdout


def is_daemon_running() -> bool:
    """检查 cua-driver daemon 是否在运行"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "cua-driver serve"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def init_database():
    """初始化数据库：建表、索引，并对旧库补齐缺失列（向后兼容）

    closing 保证 ALTER 抛 OperationalError（如 'database is locked'，见 F8）
    时连接仍被关闭——避免 raise 跳过 close 造成 fd 泄漏。
    """
    with closing(sqlite3.connect(DB_FILE)) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                knowledge_base TEXT,
                extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                y_position INTEGER,
                status TEXT DEFAULT 'success',
                obsidian_saved INTEGER DEFAULT 0,
                obsidian_saved_at TEXT,
                published_date TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_url ON articles(url)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_kb ON articles(knowledge_base)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_obsidian_saved ON articles(obsidian_saved)")
        # 向后兼容：对已有数据库添加缺失的列
        for col, type_def in [
            ("obsidian_saved", "INTEGER DEFAULT 0"),
            ("obsidian_saved_at", "TEXT"),
            ("published_date", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE articles ADD COLUMN {col} {type_def}")
            except sqlite3.OperationalError as e:
                # 仅吞 "duplicate column name"（列已存在的预期兼容场景）；
                # 其他 OperationalError（database is locked / disk I/O error / no such table）
                # 必须传播，否则下游 INSERT/SELECT 会在缺列时报 "no such column"，
                # 根因被静默掩盖，难以排查。
                if "duplicate column" in str(e).lower():
                    continue
                raise
        conn.commit()


def verify_urls_canonical(db_file=None):
    """
    自检 articles 表所有 URL 是否已规范化。

    用途：normalize_url 改格式后，旧 DB 里的 URL 在新口径下"未规范"，
    下次提取会因 url_exists 匹配不到（旧 URL 与新规范 URL 不同）而
    重复入库。本函数在提取前自检，返回未规范行列表。

    Returns:
      List[Tuple[int, str, str]] — (id, current_url, canonical_url)
      空列表表示所有 URL 已规范。
    """
    # 局部导入避免循环依赖（ima_ax_extractor 导入 ima_common）
    from ima_ax_extractor import normalize_url

    db_path = db_file or DB_FILE
    non_canonical = []
    with closing(sqlite3.connect(db_path)) as conn:
        c = conn.cursor()
        c.execute("SELECT id, url FROM articles")
        for aid, url in c.fetchall():
            if not url:
                continue
            canonical = normalize_url(url)
            if canonical != url:
                non_canonical.append((aid, url, canonical))
    return non_canonical


def get_ima_main_window():
    """
    获取 IMA 主窗口（KB 列表窗口），排除文章标签页独立窗口。

    IMA 改版后文章在独立标签页窗口打开（含浏览器地址栏"地址和搜索栏"），与 KB 列表
    主窗口面积相近甚至更大。旧逻辑只按面积 max 会误选面积更大的文章窗口，导致
    navigate_to_kb 在文章窗口里找不到知识库元素 → 0 产出。故需逐一 get_window_state
    排除含地址栏者，再按面积选最大。get_window_state 失败则保留该候选（宁可不排除
    也不误删主窗口）；若全部被排除（异常）则回退到原始候选，避免返回 None 卡死。

    返回: 窗口信息字典 {pid, window_id, bounds, ...} 或 None
    """
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

    # 排除文章标签页独立窗口（含浏览器地址栏）；state 读失败则保留候选
    candidates = [w for w in ima_windows if not _is_article_tab_window(w)]
    if not candidates:
        candidates = ima_windows  # 全被识别为文章窗口（异常），回退避免返回 None

    # 选面积最大的窗口（主窗口）
    return max(candidates, key=lambda w: w["bounds"].get("width", 0) * w["bounds"].get("height", 0))


def _is_article_tab_window(window: dict) -> bool:
    """判断是否为文章标签页独立窗口（AX 树含浏览器地址栏"地址和搜索栏"）。

    get_window_state 失败返回 False（不排除：宁保留主窗口候选也不误删）。
    """
    try:
        st = json.loads(run_cua([
            "call", "get_window_state",
            json.dumps({"pid": window["pid"], "window_id": window["window_id"]}),
        ]))
        return "地址和搜索栏" in st.get("tree_markdown", "")
    except Exception:
        return False


# ==================== AppleScript ====================

def get_kb_window_title(kb_name: str = "") -> str:
    """
    获取知识库窗口标题

    Args:
        kb_name: 知识库名称，如果指定则查找包含该名称的窗口

    Returns:
        窗口标题字符串
    """
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
