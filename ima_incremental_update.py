#!/usr/bin/env python3
"""
IMA 增量更新脚本 — 每日自动提取新增文章并保存到 Obsidian

支持多个知识库批量处理，自动跳过已存在文章。
提取完成后自动调用 Obsidian 保存器。

使用:
  python3 ima_incremental_update.py                    # 更新所有知识库
  python3 ima_incremental_update.py --kb AI Python     # 只更新指定知识库
  python3 ima_incremental_update.py --dry-run          # 预览模式
  python3 ima_incremental_update.py --no-save          # 只提取不保存到 Obsidian
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# 导入公共模块
from ima_common import (
    CUA_DRIVER, IMA_APP_NAME, run_cua, is_daemon_running,
    get_ima_main_window,
)

# ==================== 配置 ====================

# 要监控的知识库列表
KNOWLEDGE_BASES = [
    "AI",
    "Invest",
    "英语教与学",
    "Andrew",
    "皮皮鲁的知识库",
]

LOG_FILE = Path(__file__).parent / "incremental_update.log"
LOCK_FILE = Path(__file__).parent / "incremental_update.lock"
LOG_MAX_BYTES = 2 * 1024 * 1024  # 日志文件最大 2MB

WAIT_BETWEEN_KB = 5.0  # 知识库之间等待时间


# ==================== 日志 ====================

def rotate_log_if_needed():
    """日志文件超过阈值时轮转，保留旧日志为 .1 后缀"""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            backup = LOG_FILE.with_suffix(".log.1")
            if backup.exists():
                backup.unlink()
            LOG_FILE.rename(backup)
    except OSError:
        pass


def log(message: str, print_too: bool = True):
    """写入日志文件"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    # 只在交互模式（TTY）下输出到 stdout，避免与 launchd 重定向重复
    if print_too and sys.stdout.isatty():
        print(log_line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_line + "\n")


# ==================== cua-driver ====================

def start_daemon() -> bool:
    """启动 cua-driver daemon"""
    log("启动 cua-driver daemon...")
    try:
        # 使用 nohup 启动，输出到 /dev/null
        subprocess.Popen(
            [CUA_DRIVER, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        # 等待 daemon 启动：进程存在后，再做一次握手确认 IPC socket 就绪
        for i in range(10):
            time.sleep(1)
            if is_daemon_running():
                try:
                    run_cua(["list_windows"], timeout=10)
                    log("✅ cua-driver daemon 已就绪")
                    return True
                except RuntimeError:
                    log(f"  daemon 进程已存在，等待 IPC socket 就绪...")
                    continue
        log("⚠️  cua-driver daemon 启动超时")
        return False
    except Exception as e:
        log(f"❌ 启动 cua-driver daemon 失败: {e}")
        return False


def ensure_daemon() -> bool:
    """确保 cua-driver daemon 正在运行"""
    if is_daemon_running():
        return True
    return start_daemon()


def is_ima_running() -> bool:
    """检查 IMA 是否正在运行"""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "ima.copilot"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def launch_ima():
    """启动 IMA 应用"""
    log("启动 IMA 应用...")
    subprocess.run(
        ["open", "-a", "ima.copilot"],
        capture_output=True, timeout=10
    )
    # 等待应用启动
    for i in range(30):  # 最多等待 30 秒
        time.sleep(1)
        if is_ima_running():
            log("✅ IMA 已启动")
            wait_for_ax_ready()  # 硬等待窗口 AX 树渲染就绪再导航
            return True
    log("⚠️  IMA 启动超时")
    return False


def wait_for_ax_ready(min_elements: int = 5, timeout: int = 30) -> bool:
    """
    硬等待 IMA 窗口 AX 树就绪（AXStaticText 元素数超过阈值）

    在启动 IMA 后调用，持续轮询主窗口 AX 树，
    直到 AXStaticText 数量 >= min_elements 或超时。
    阈值口径与 navigate_to_kb 的完整性判断一致（AXStaticText >= 5）。

    返回: True 表示就绪，False 表示超时（调用方应降级为原行为，不阻断）
    """
    import re

    deadline = time.time() + timeout
    log(f"等待 IMA 窗口 AX 树就绪（阈值 {min_elements} 个元素，超时 {timeout}秒）...")

    while time.time() < deadline:
        window = get_ima_main_window()
        if not window:
            time.sleep(1)
            continue
        try:
            state_result = run_cua(
                ["call", "get_window_state", json.dumps({
                    "pid": window["pid"],
                    "window_id": window["window_id"],
                })],
                timeout=10,  # 单次探测限时，避免一次阻塞吃满整个等待预算
            )
            state = json.loads(state_result)
            md = state.get("tree_markdown", "")
            count = len(re.findall(r'AXStaticText', md))
            if count >= min_elements:
                log(f"✅ AX 树就绪（{count} 个元素）")
                return True
        except Exception as e:
            log(f"  AX 树探测异常，重试中: {e}")
        time.sleep(1)

    log(f"⚠️  AX 树就绪等待超时（{timeout}秒内未达 {min_elements} 个元素），降级继续")
    return False


def get_ax_window_title() -> str:
    """
    用 cua-driver AX API 读取 IMA 主窗口标题

    Electron 应用（IMA）的窗口标题对 System Events 不可靠（冷启动后常读空），
    AX API 能稳定读到 AXWindow 标题。供导航判断使用，替代 get_kb_window_title。

    返回: 窗口标题字符串（如 "AI - ima.copilot"），失败返回 ""
    """
    import re
    try:
        window = get_ima_main_window()
        if not window:
            return ""
        state_result = run_cua(
            ["call", "get_window_state", json.dumps({
                "pid": window["pid"],
                "window_id": window["window_id"],
            })],
            timeout=10,
        )
        md = json.loads(state_result).get("tree_markdown", "")
        m = re.search(r'AXWindow "([^"]*)"', md)
        return m.group(1) if m else ""
    except Exception:
        return ""


def is_on_kb_list(kb_name: str) -> bool:
    """
    可靠判断当前是否在目标知识库的【文章列表页】（而非文章详情页）。

    旧判断 `kb_name in title` 对短名（如 "AI"）假阳性：文章详情页标题常含 "AI"
    （如"让 AI 每次都…"），会被误判为已在 AI 知识库。新判断要求标题含 KB 名
    **且** 当前是列表页（parse_articles_from_tree 能解析出文章卡片；详情页为 0）。
    """
    title = get_ax_window_title()
    if not title or kb_name not in title:
        return False
    window = get_ima_main_window()
    if not window:
        return False
    try:
        from ima_ax_extractor import get_window_state, parse_articles_from_tree
        state = get_window_state(window["pid"], window["window_id"])
        cards = parse_articles_from_tree(state, kb_name) if state else []
        return len(cards) > 0
    except Exception:
        return False


def wake_screen():
    """轻量唤醒屏幕（应对显示器休眠）"""
    try:
        subprocess.run(
            ["caffeinate", "-u", "-t", "5"],
            capture_output=True, timeout=5
        )
        time.sleep(1)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 123'],
            capture_output=True, timeout=5
        )
        time.sleep(1)
    except Exception:
        pass


def activate_ima():
    """激活 IMA 应用，如果未运行则启动"""
    if not is_ima_running():
        launch_ima()

    wake_screen()

    subprocess.run(
        ["osascript", "-e", 'tell application "ima.copilot" to activate'],
        capture_output=True, timeout=5
    )
    time.sleep(2)


def navigate_to_kb(kb_name: str, max_attempts: int = 5) -> bool:
    """
    通过 cua-driver 自动导航到指定知识库

    在侧边栏找到知识库名称并点击切换，支持多次尝试和滚动查找。
    使用 scroll 工具定向滚动侧边栏区域。

    返回: True 表示成功导航
    """
    import re

    window = get_ima_main_window()
    if not window:
        log("❌ 未找到 IMA 窗口，无法导航")
        return False

    pid = window["pid"]
    window_id = window["window_id"]

    # 先找到侧边栏滚动区域（AXScrollArea），用于定向滚动
    sidebar_elem = None

    for attempt in range(1, max_attempts + 1):
        log(f"导航尝试 {attempt}/{max_attempts}...")

        # 激活窗口确保 AX Tree 完整
        subprocess.run(
            ["osascript", "-e", 'tell application "ima.copilot" to activate'],
            capture_output=True, timeout=5
        )
        time.sleep(2)

        # 获取窗口状态
        state_result = run_cua(["call", "get_window_state", json.dumps({"pid": pid, "window_id": window_id})])
        state = json.loads(state_result)
        md = state.get("tree_markdown", "")

        # 验证 AX Tree 是否包含窗口内容（未激活时只有菜单栏）
        static_text_count = len(re.findall(r'AXStaticText', md))
        if static_text_count < 5:
            log(f"  ⚠️  AX Tree 不完整（仅 {static_text_count} 个元素），窗口可能未激活，等待重试...")
            time.sleep(3)
            # 再次获取
            state_result = run_cua(["call", "get_window_state", json.dumps({"pid": pid, "window_id": window_id})])
            state = json.loads(state_result)
            md = state.get("tree_markdown", "")
            static_text_count = len(re.findall(r'AXStaticText', md))
            if static_text_count < 5:
                log(f"  ⚠️  AX Tree 仍不完整（{static_text_count} 个元素），跳过本次尝试")
                continue

        # 首次尝试时定位侧边栏滚动区域
        if sidebar_elem is None:
            for line in md.split("\n"):
                m = re.search(r'\[(\d+)\] AXScrollArea', line)
                if m:
                    sidebar_elem = int(m.group(1))
                    break

        # 在全文查找知识库名称的 element_index
        # 匹配多种 AX 类型：AXStaticText, AXButton, AXLink 等
        elem_idx = None
        # 第一遍：完全匹配（优先匹配侧边栏知识库入口，避免误点含关键词的文章卡片）
        for line in md.split("\n"):
            m = re.search(
                r'\[(\d+)\] (?:AXStaticText|AXButton|AXLink|AXRow) = "' + re.escape(kb_name) + r'"',
                line
            )
            if m:
                elem_idx = int(m.group(1))
                break
        # 第二遍：包含匹配（仅当完全匹配未命中，知识库名可能带前缀/后缀）
        # 短名（如 "AI"）的包含匹配易误命中文章标题/链接（如"人人会AI-智能体"），
        # 故只接受短文本（KB 入口名通常 ≤ KB名+8 字符），跳过长文章标题。
        if elem_idx is None:
            for line in md.split("\n"):
                m = re.search(
                    r'\[(\d+)\] (?:AXStaticText|AXButton|AXLink|AXRow) = "(.*' + re.escape(kb_name) + r'.*)"',
                    line
                )
                if m:
                    text_val = m.group(2)
                    if len(text_val) <= len(kb_name) + 8:
                        elem_idx = int(m.group(1))
                        break

        if elem_idx is not None:
            log(f"  找到知识库 '{kb_name}' (element {elem_idx})，点击...")

            click_result = run_cua(["call", "click", json.dumps({
                "pid": pid,
                "window_id": window_id,
                "element_index": elem_idx
            })])

            for wait in range(8):
                time.sleep(2.5)
                title = get_ax_window_title()
                if is_on_kb_list(kb_name):
                    log(f"  ✅ 已导航到 {kb_name} 知识库（列表页验证通过）")
                    time.sleep(2)
                    return True
                if wait < 7:
                    log(f"    等待页面加载... ({(wait+1)*2.5}秒) 标题: '{title}'")

            log(f"  ⚠️  点击后未到达 {kb_name} 列表页，标题: '{title}'")
        else:
            log(f"  ⚠️  在侧边栏未找到知识库 '{kb_name}'")
            log(f"  尝试滚动侧边栏...")
            try:
                # 使用 scroll 工具定向滚动侧边栏（参数形状与提取器 scroll_down 一致）
                scroll_params = {
                    "pid": pid,
                    "window_id": window_id,
                    "direction": "down",
                    "amount": 3
                }
                if sidebar_elem is not None:
                    scroll_params["element_index"] = sidebar_elem
                run_cua(["call", "scroll", json.dumps(scroll_params)])
                time.sleep(1.5)
            except Exception as e:
                log(f"    滚动失败: {e}")

    log(f"❌ 导航到 '{kb_name}' 失败（已尝试 {max_attempts} 次）")
    return False


def ensure_ima_ready(kb_name: str, timeout: int = 60) -> bool:
    """
    确保 IMA 已就绪并位于目标知识库

    完全自动化：先检查，再尝试自动导航，失败则跳过

    返回: True 表示就绪，False 表示失败
    """
    log(f"确保 IMA 位于 {kb_name} 知识库...")

    # 先检查是否已经在目标知识库的【列表页】（is_on_kb_list 避免"AI"等短名假阳性）
    if is_on_kb_list(kb_name):
        log(f"✅ 已在 {kb_name} 知识库列表页")
        return True

    # 尝试自动导航
    activate_ima()
    time.sleep(2)  # 增加等待时间，确保窗口完全激活

    if navigate_to_kb(kb_name):
        return True

    # 自动导航失败，直接跳过（不等待手动操作）
    log(f"⚠️  自动导航失败，跳过 {kb_name} 知识库")
    return False


# ==================== Obsidian 保存器 ====================

def is_obsidian_running() -> bool:
    """检查 Obsidian 是否正在运行"""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Obsidian"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def launch_obsidian(timeout: int = 30) -> bool:
    """
    启动 Obsidian 并等待 Vault 加载（与 launch_ima 同构）

    Obsidian 未运行时 Web Clipper 扩展无法连接 Vault，所有文章保存必然失败。
    启动后额外等待 5 秒，给 Vault 自动加载留时间，Web Clipper 才能连上。

    返回: True 表示就绪，False 表示启动超时
    """
    log("启动 Obsidian 应用...")
    subprocess.run(
        ["open", "-a", "Obsidian"],
        capture_output=True, timeout=10
    )
    for _ in range(timeout):
        time.sleep(1)
        if is_obsidian_running():
            log("✅ Obsidian 已启动，等待 Vault 加载...")
            time.sleep(5)  # 让 Vault 完成加载，Web Clipper 才能连上
            return True
    log("⚠️  Obsidian 启动超时")
    return False


def ensure_obsidian_ready() -> bool:
    """确保 Obsidian 已运行（未运行则自动启动），供保存器前置检查使用"""
    if is_obsidian_running():
        return True
    return launch_obsidian()


def save_to_obsidian(kb_name: str = None, dry_run: bool = False) -> dict:
    """
    调用 Obsidian 保存器（行级实时透传 saver 输出，避免长时间无输出被误判"卡死"）

    返回统计信息: {saved, failed}
    """
    log(f"\n{'─'*40}")
    log(f"保存到 Obsidian...")

    # 保存前确保 Obsidian 已运行（没开则自动启动，与 IMA 同构），
    # 否则 Web Clipper 连不上 Vault，全部文章必然失败白跑
    if not dry_run:
        if not ensure_obsidian_ready():
            log("❌ Obsidian 无法启动，跳过本次保存")
            return {"saved": 0, "failed": 1}

    cmd = [
        "python3",
        "-u",  # 禁用输出缓冲，配合下面的行级透传实时显示保存进度
        Path(__file__).parent / "ima_obsidian_saver.py",
        "--limit", "1000",  # 每次最多保存 1000 篇
    ]

    # 指定知识库：只保存该 KB 的文章，并存入对应文件夹（避免不同 KB 混入同一文件夹）
    if kb_name:
        cmd.extend(["--kb", kb_name, "--des", kb_name])

    if dry_run:
        cmd.append("--dry-run")

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,  # 隔离父进程 tty，让 saver 判定非交互自动开始，否则卡在 input() 等 Enter
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=Path(__file__).parent,
        )
    except Exception as e:
        log(f"❌ 启动 Obsidian 保存器失败: {e}")
        return {"saved": 0, "failed": 1}

    # 行级实时透传 saver stdout → 日志：替代 capture_output=True 的全量缓冲，
    # 让每篇文章的提取日期/触发 clipper/落盘轮询进度立即可见，
    # 避免 13 篇 × 30-40s 期间无输出被误判"卡死"
    captured_lines = []
    stderr_lines = []

    def _stream(stream, sink, log_too: bool):
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                line = line.rstrip("\n")
                sink.append(line)
                if log_too and line.strip():
                    log(f"  {line}", print_too=True)  # 实时显示 saver 进度到终端（launchd 下 isatty=False 自动只写日志，不会重复）
        finally:
            stream.close()

    stdout_t = threading.Thread(target=_stream, args=(proc.stdout, captured_lines, True), daemon=True)
    stderr_t = threading.Thread(target=_stream, args=(proc.stderr, stderr_lines, False), daemon=True)
    stdout_t.start()
    stderr_t.start()

    try:
        proc.wait(timeout=1800)  # 30 分钟超时（13 篇正常 6-8 分钟，原 7200s 过长）
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        stdout_t.join(timeout=3)
        stderr_t.join(timeout=3)
        log(f"❌ Obsidian 保存超时（1800s），已终止 saver")
        return {"saved": 0, "failed": 1}

    stdout_t.join(timeout=5)
    stderr_t.join(timeout=5)
    returncode = proc.returncode

    # 解析统计（saver 现按统计退出：全失败 exit1、部分失败 exit2，stdout 始终含统计行）
    def _parse_count(marker: str) -> int:
        for line in captured_lines:
            if marker in line:
                try:
                    return int(line.split(":")[-1].strip().split()[0])
                except (ValueError, IndexError):
                    return 0
        return 0

    saved_count = _parse_count("本次成功")
    failed_count = _parse_count("本次失败")

    if returncode != 0:
        log(f"❌ Obsidian 保存失败（退出码 {returncode}：成功 {saved_count}，失败 {failed_count}）")
        if stderr_lines:
            log(f"错误: {chr(10).join(stderr_lines)}")
    else:
        log(f"✅ 保存完成: {saved_count} 篇")
    return {"saved": saved_count, "failed": failed_count}


def count_unsaved_articles(kb_name: str) -> int:
    """统计该知识库未保存到 Obsidian 的微信文章数（含历史漏存，用于决定是否触发保存重试）"""
    import sqlite3
    from contextlib import closing
    try:
        from ima_common import DB_FILE
        # closing 保证异常路径也关闭连接，避免 launchd 长跑累积 fd 泄漏
        with closing(sqlite3.connect(DB_FILE)) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT COUNT(*) FROM articles
                WHERE knowledge_base = ?
                  AND status = 'success'
                  AND url LIKE '%mp.weixin.qq.com%'
                  AND (obsidian_saved = 0 OR obsidian_saved IS NULL)
            """, (kb_name,))
            return c.fetchone()[0]
    except Exception:
        return 0


# ==================== 增量更新逻辑 ====================

def update_knowledge_base(kb_name: str, dry_run: bool = False) -> dict:
    """
    更新单个知识库

    返回统计信息: {new, skipped, failed}
    """
    log(f"\n{'='*50}")
    log(f"开始更新知识库: {kb_name}")
    log(f"{'='*50}")

    # 激活 IMA 并确保就绪
    activate_ima()

    # 等待确认在目标知识库
    if not dry_run:
        if not ensure_ima_ready(kb_name, timeout=60):
            log(f"⚠️  无法确认在 {kb_name} 知识库，跳过")
            return {"new": 0, "skipped": 0, "failed": 1}

    # 获取 IMA 窗口
    window = get_ima_main_window()
    if not window:
        log("❌ 未找到 IMA 窗口")
        return {"new": 0, "skipped": 0, "failed": 1}

    pid = window["pid"]
    window_id = window["window_id"]

    log(f"✅ 确认在 {kb_name} 知识库")

    if dry_run:
        log(f"[DRY RUN] 将提取 {kb_name} 知识库新增文章")
        return {"new": 0, "skipped": 0, "failed": 0}

    # 调用提取器前再次激活 IMA，确保窗口在前台
    log("激活 IMA 窗口...")
    activate_ima()
    time.sleep(2)  # 等待窗口完全激活

    # 调用提取器脚本
    cmd = [
        "python3",
        "-u",  # 禁用输出缓冲，实时查看提取进度
        Path(__file__).parent / "ima_ax_extractor.py",
        "--src", kb_name
    ]

    log(f"执行提取器...")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,
            cwd=Path(__file__).parent
        )

        if result.stdout:
            for line in result.stdout.split("\n"):
                if line.strip():
                    log(f"  {line}", print_too=False)

        if result.returncode != 0:
            log(f"❌ 提取器执行失败")
            if result.stderr:
                log(f"错误: {result.stderr}")
            return {"new": 0, "skipped": 0, "failed": 1}

        # 解析统计信息
        new_count = 0
        skipped_count = 0

        for line in result.stdout.split("\n"):
            if "本次新增" in line:
                try:
                    new_count = int(line.split(":")[-1].strip().split()[0])
                except (ValueError, IndexError):
                    pass
            elif "本次跳过" in line:
                try:
                    skipped_count = int(line.split(":")[-1].strip().split()[0])
                except (ValueError, IndexError):
                    pass

        log(f"✅ {kb_name} 更新完成: 新增 {new_count}, 跳过 {skipped_count}")
        return {"new": new_count, "skipped": skipped_count, "failed": 0}

    except subprocess.TimeoutExpired:
        log(f"❌ {kb_name} 提取超时")
        return {"new": 0, "skipped": 0, "failed": 1}
    except Exception as e:
        log(f"❌ {kb_name} 提取失败: {e}")
        return {"new": 0, "skipped": 0, "failed": 1}


# ==================== 主函数 ====================

def main():
    # 文件锁防止并发执行
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("⚠️  另一个增量更新实例正在运行，退出")
        sys.exit(0)

    # 日志轮转
    rotate_log_if_needed()

    parser = argparse.ArgumentParser(
        description="IMA 增量更新脚本 — 每日自动提取新增文章并保存到 Obsidian"
    )
    parser.add_argument(
        "--kb",
        nargs="+",
        help="指定要更新的知识库（默认: 所有配置的知识库）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式，不实际执行"
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="只提取，不保存到 Obsidian"
    )
    args = parser.parse_args()

    # 确定要处理的知识库
    if args.kb:
        kbs = args.kb
    else:
        kbs = KNOWLEDGE_BASES

    if not kbs:
        log("❌ 没有配置知识库，请使用 --kb 指定或在脚本中添加 KNOWLEDGE_BASES")
        sys.exit(1)

    log(f"\n{'='*60}")
    log(f"IMA 增量更新开始")
    log(f"{'='*60}")
    log(f"知识库: {', '.join(kbs)}")
    log(f"模式: {'预览' if args.dry_run else '正式'}")
    log(f"保存到 Obsidian: {'否' if args.no_save else '是'}")

    # 确保 cua-driver daemon 运行
    if not args.dry_run:
        if not ensure_daemon():
            log("❌ 无法启动 cua-driver daemon")
            sys.exit(1)

    log("✅ cua-driver daemon 运行中")

    # URL 规范化入口自检：避免 normalize_url 改格式后旧 DB 未迁移导致重复入库。
    # 必须先 init_database 自建 schema（fresh DB / 首次安装 / DB 被删场景），
    # 否则 verify_urls_canonical 在空 DB 上抛 'no such table: articles'。
    # 此处守卫是唯一的"未规范 DB 短路"机制——update_knowledge_base 内不再做
    # 基于 stdout 子串的判定（守卫-通过行 '✅ URL 规范化自检通过' 含相同子串，
    # 会让 extractor 后续任何 exit 1（daemon/窗口/AX 失败）被误判为守卫触发，
    # 静默跳过剩余 KB）。
    if not args.dry_run:
        from ima_common import init_database, verify_urls_canonical
        init_database()
        non_canonical = verify_urls_canonical()
        if non_canonical:
            log(f"❌ 检测到 {len(non_canonical)} 行 URL 未规范（normalize_url 口径变更后需迁移）")
            log(f"   示例 id={non_canonical[0][0]}:")
            log(f"     当前: {non_canonical[0][1][:80]}")
            log(f"     规范: {non_canonical[0][2][:80]}")
            log(f"   请先运行: python3 migrate_normalize_urls.py")
            sys.exit(1)
        log("✅ URL 规范化自检通过")

    # 总计统计
    total_new = 0
    total_skipped = 0
    total_saved = 0
    total_failed = 0       # 保存失败（saver）
    total_kb_failed = 0    # 知识库处理失败（导航/窗口/提取）

    # 逐个处理知识库
    for i, kb_name in enumerate(kbs, 1):
        log(f"\n[{i}/{len(kbs)}] 处理知识库: {kb_name}")

        # 提取文章
        stats = update_knowledge_base(kb_name, args.dry_run)
        total_new += stats["new"]
        total_skipped += stats["skipped"]
        total_kb_failed += stats["failed"]

        # 触发保存：有新文章，或该 KB 有历史漏存（之前保存失败/超时未保存）。
        # 后者让失败文章能在后续运行中自动重试，避免 new=0 时永久漏存。
        unsaved = count_unsaved_articles(kb_name) if (not args.no_save and not args.dry_run) else 0
        if (stats["new"] > 0 or unsaved > 0) and not args.no_save and not args.dry_run:
            if stats["new"] == 0 and unsaved > 0:
                log(f"检测到 {kb_name} 有 {unsaved} 篇历史漏存未保存，触发保存重试")
            save_stats = save_to_obsidian(kb_name)
            total_saved += save_stats["saved"]
            total_failed += save_stats["failed"]

        # 知识库之间等待
        if i < len(kbs):
            log(f"等待 {WAIT_BETWEEN_KB} 秒后处理下一个知识库...")
            if not args.dry_run:
                time.sleep(WAIT_BETWEEN_KB)

    # 总结
    log(f"\n{'='*60}")
    log(f"增量更新完成")
    log(f"{'='*60}")
    log(f"总计新增: {total_new} 篇")
    log(f"总计跳过: {total_skipped} 篇")
    log(f"保存到 Obsidian: {total_saved} 篇")
    log(f"保存失败: {total_failed} 篇")
    log(f"知识库处理失败: {total_kb_failed} 个")

    # 退出码：保存失败或 KB 处理失败（如夜间锁屏致导航全跪）时非零退出，让 launchd 暴露静默失败（dry-run 不告警）
    if (total_failed > 0 or total_kb_failed > 0) and not args.dry_run:
        log(f"⚠️  存在失败（保存 {total_failed} 篇，KB 处理失败 {total_kb_failed} 个），非零退出以触发告警")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n⚠️  用户中断")
        sys.exit(1)
    except Exception as e:
        log(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
