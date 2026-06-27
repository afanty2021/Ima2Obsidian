#!/usr/bin/env python3
"""
IMA 公共模块 — 共享函数和工具

包含在多个脚本中重复使用的函数，避免代码重复。
"""

import json
import sqlite3
import subprocess
from pathlib import Path

# ==================== 配置 ====================

CUA_DRIVER = "/Users/berton/.local/bin/cua-driver"
IMA_APP_NAME = "ima.copilot"
DB_FILE = Path(__file__).parent / "ima_articles.db"


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
    """初始化数据库：建表、索引，并对旧库补齐缺失列（向后兼容）"""
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
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.commit()
    conn.close()


def get_ima_main_window():
    """
    获取 IMA 主窗口信息

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

    # 选最大的窗口（主窗口）
    return max(ima_windows, key=lambda w: w["bounds"].get("width", 0) * w["bounds"].get("height", 0))


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
