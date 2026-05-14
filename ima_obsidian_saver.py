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
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests


# ==================== 配置 ====================

DB_FILE = Path(__file__).parent / "ima_articles.db"
VAULT_DIR = Path("/Users/berton/Documents/Obsidian Vault")
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
WAIT_CLIP_SAVE = 4.0
WAIT_FILE_APPEAR = 2.0
WAIT_CLOSE_TAB = 1.0
WAIT_BETWEEN = 1.5

DEFAULT_LIMIT = 1300


# ==================== 日期提取 ====================

def extract_publish_date(url: str) -> str:
    """从微信文章页面提取发布日期，返回 YYMMDD 格式"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=15)
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

    except Exception as e:
        print(f"    ⚠️  日期提取失败: {e}")

    # 降级: 使用当前日期
    return datetime.now().strftime("%y%m%d")


def sanitize_filename(title: str) -> str:
    """清理文件名中的非法字符"""
    # 移除或替换不适合文件名的字符
    cleaned = re.sub(r'[/\\:*?"<>|]', '-', title)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # 截断过长标题
    if len(cleaned) > 100:
        cleaned = cleaned[:100]
    return cleaned


# ==================== 数据库 ====================

def ensure_schema():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for col, type_def in [
        ("obsidian_saved", "INTEGER DEFAULT 0"),
        ("obsidian_saved_at", "TEXT"),
        ("published_date", "TEXT"),
    ]:
        try:
            c.execute(f"ALTER TABLE articles ADD COLUMN {col} {type_def}")
        except sqlite3.OperationalError:
            pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_obsidian_saved ON articles(obsidian_saved)")
    conn.commit()
    conn.close()


def get_unsaved_articles(limit: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
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
    conn.close()
    return [{"id": r[0], "url": r[1], "title": r[2], "kb": r[3]} for r in rows]


def mark_saved(article_id: int, published_date: str = None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "UPDATE articles SET obsidian_saved = 1, obsidian_saved_at = ?, published_date = ? WHERE id = ?",
        (datetime.now().isoformat(), published_date, article_id),
    )
    conn.commit()
    conn.close()


def get_stats():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM articles WHERE status = 'success' AND url LIKE '%mp.weixin.qq.com%'")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM articles WHERE obsidian_saved = 1")
    saved = c.fetchone()[0]
    conn.close()
    return {"total": total, "saved": saved, "unsaved": max(0, total - saved)}


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


def close_tab(browser_app: str = None):
    """关闭浏览器标签页，优先使用后台方式"""
    if browser_app:
        # 尝试使用 AppleScript 后台关闭（不激活应用）
        script = f'''
tell application "{browser_app}"
    if (count of windows) > 0 then
        close active tab of window 1
    end if
end tell
'''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return  # 成功后台关闭
        except Exception:
            pass  # 降级到快捷键方式

    # 降级方案：使用快捷键（需要浏览器在前台）
    send_keystroke("w", ["command"])


def trigger_quick_clip(mods: list):
    send_keystroke(QUICK_CLIP_KEY, mods)


def trigger_clipper_and_save(mods: list):
    send_keystroke(CLIPPER_KEY, CLIPPER_MODS)
    time.sleep(2.0)
    send_keystroke("return", [])


# ==================== Vault 文件重命名 ====================

def find_and_rename_in_vault(
    title: str,
    date_str: str,
    existing_files: set,
    search_dirs: list = None,
    target_folder: str = None,
) -> bool:
    """
    在 Obsidian vault 中找到 Web Clipper 刚保存的文件，
    重命名为 YYMMDD title.md 格式，并移动到目标文件夹。

    existing_files: set of (Path, mtime) tuples captured before opening article
    target_folder: 目标文件夹名称（如 "AI"），如果为 None 则保持在原位置
    """
    if search_dirs is None:
        search_dirs = [CLIPPINGS_DIR, VAULT_DIR]

    target_name = f"{date_str}{sanitize_filename(title)}.md"

    # 确定目标路径
    if target_folder:
        # 创建目标文件夹路径
        folder_path = VAULT_DIR / target_folder
        folder_path.mkdir(parents=True, exist_ok=True)
        final_target_path = folder_path / target_name
    else:
        # 不移动，只重命名
        final_target_path = None

    # 第一步：精确匹配 —— 文件名与标题匹配的最近创建文件
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for md_file in search_dir.glob("*.md"):
            try:
                mtime = os.path.getmtime(md_file)
                if time.time() - mtime > 30:
                    continue
            except OSError:
                continue
            stem = md_file.stem
            if title in stem or stem in title:
                if target_folder:
                    # 移动到目标文件夹
                    if md_file != final_target_path:
                        md_file.rename(final_target_path)
                        print(f"    移动: {stem[:40]}... → {target_folder}/{target_name[:50]}...")
                else:
                    # 只重命名
                    new_path = md_file.parent / target_name
                    if md_file != new_path:
                        md_file.rename(new_path)
                        print(f"    重命名: {stem[:40]}... → {target_name[:50]}...")
                return True

    # 第二步：找新文件（不存在于保存前的快照中）
    existing_paths = {ef[0] for ef in existing_files}
    new_files = []
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for md_file in search_dir.glob("*.md"):
            if md_file in existing_paths:
                continue
            try:
                mtime = os.path.getmtime(md_file)
                if time.time() - mtime < 60:
                    new_files.append((md_file, mtime))
            except OSError:
                pass

    if new_files:
        # 取最新创建的文件
        new_files.sort(key=lambda x: x[1], reverse=True)
        newest = new_files[0][0]
        if target_folder:
            # 移动到目标文件夹
            if newest != final_target_path:
                newest.rename(final_target_path)
                print(f"    移动(新文件): {newest.stem[:40]}... → {target_folder}/{target_name[:50]}...")
        else:
            # 只重命名
            new_path = newest.parent / target_name
            if newest != new_path:
                newest.rename(new_path)
                print(f"    重命名(新文件): {newest.stem[:40]}... → {target_name[:50]}...")
        return True

    return False


# ==================== 核心保存逻辑 ====================

def save_one_article(
    article: dict,
    browser_config: dict,
    mode: str = "quick",
    dry_run: bool = False,
    target_folder: str = None,
) -> bool:
    url = article["url"]
    title = article.get("title", "Unknown") or "Unknown"
    browser_app = browser_config["app"]
    shortcut_mods = browser_config["shortcut_mods"]

    if dry_run:
        date_str = extract_publish_date(url)
        folder_info = f" → {target_folder}/" if target_folder else ""
        new_name = f"{date_str}{sanitize_filename(title)}.md"
        print(f"  [DRY RUN] {title[:50]}...")
        print(f"    发布日期: {date_str}")
        print(f"    目标位置: {folder_info}{new_name[:60]}")
        return True

    # 1. 提取发布日期
    print(f"  提取日期...")
    date_str = extract_publish_date(url)
    print(f"    发布日期: {date_str}")

    # 记录 vault 中当前 .md 文件列表（用于后续检测新文件）
    existing_files = set()
    for d in [CLIPPINGS_DIR, VAULT_DIR]:
        if d.exists():
            for f in d.glob("*.md"):
                try:
                    existing_files.add((f, os.path.getmtime(f)))
                except OSError:
                    pass

    # 2. 打开文章
    print(f"  打开: {title[:50]}...")
    open_url(browser_app, url)
    time.sleep(WAIT_PAGE_LOAD)

    # 3. 触发 Web Clipper
    activate_browser(browser_app)

    if mode == "quick":
        print(f"    触发 quick_clip ({'+'.join(shortcut_mods)}+{QUICK_CLIP_KEY})...")
        trigger_quick_clip(shortcut_mods)
    else:
        print(f"    触发 clipper (Cmd+Shift+{CLIPPER_KEY})...")
        trigger_clipper_and_save(shortcut_mods)

    time.sleep(WAIT_CLIP_SAVE)

    # 4. 查找新保存的文件并重命名/移动
    print(f"    查找并重命名...")
    time.sleep(WAIT_FILE_APPEAR)

    renamed = find_and_rename_in_vault(title, date_str, existing_files, target_folder=target_folder)

    if not renamed:
        folder_info = f"{target_folder}/" if target_folder else ""
        print(f"    ⚠️  未找到保存的文件，可能需要手动移动到: {folder_info}{date_str}{sanitize_filename(title)}.md")

    # 5. 关闭标签页（尝试后台关闭，不激活浏览器）
    close_tab(browser_app)
    time.sleep(WAIT_CLOSE_TAB)

    return True


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
    args = parser.parse_args()

    browser_config = BROWSERS[args.browser]
    browser_app = browser_config["app"]

    print("\n" + "=" * 60)
    print("IMA 微信文章 → Obsidian 自动保存器")
    print("=" * 60)

    ensure_schema()
    stats = get_stats()

    print(f"\n数据库统计:")
    print(f"  微信文章总数: {stats['total']}")
    print(f"  已保存到 Obsidian: {stats['saved']}")
    print(f"  待保存: {stats['unsaved']}")
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

    articles = get_unsaved_articles(args.limit)
    print(f"\n本次处理: {len(articles)} 篇\n")

    if not args.dry_run:
        print("请确保:")
        print(f"  1. {browser_app} 已安装 Obsidian Web Clipper 扩展")
        print("  2. Obsidian 应用已运行并打开了目标 Vault")
        print("  3. Web Clipper 已在扩展中连接到 Obsidian")
        print("  4. 保存期间不要操作键盘和鼠标")
        print()
        try:
            input("按 Enter 开始，Ctrl+C 取消...")
        except KeyboardInterrupt:
            print("\n已取消")
            return

    saved_count = 0
    failed_count = 0

    for i, article in enumerate(articles, 1):
        print(f"\n[{i}/{len(articles)}]", end=" ")
        try:
            success = save_one_article(
                article, browser_config, mode=args.mode, dry_run=args.dry_run,
                target_folder=args.des
            )
            if success:
                if not args.dry_run:
                    mark_saved(article["id"])
                saved_count += 1
                print(f"    ✅ 完成")
            else:
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

    stats = get_stats()
    print("\n" + "=" * 60)
    print("处理完成")
    print("=" * 60)
    print(f"  本次成功: {saved_count} 篇")
    print(f"  本次失败: {failed_count} 篇")
    print(f"  剩余待保存: {stats['unsaved']}")


if __name__ == "__main__":
    main()
