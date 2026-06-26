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
import time
from datetime import datetime
from pathlib import Path

# 导入公共模块
from ima_common import get_ima_main_window, get_kb_window_title

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

# cua-driver 路径
CUA_DRIVER = "/Users/berton/.local/bin/cua-driver"
IMA_APP_NAME = "ima.copilot"

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

def run_cua(args: list, timeout: int = 30) -> str:
    cmd = [CUA_DRIVER] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"cua-driver failed: {result.stderr.strip()}")
    return result.stdout


def is_daemon_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "cua-driver serve"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


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
            time.sleep(3)  # 额外等待应用初始化
            return True
    log("⚠️  IMA 启动超时")
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
        for line in md.split("\n"):
            # 完全匹配
            m = re.search(
                r'\[(\d+)\] (?:AXStaticText|AXButton|AXLink|AXRow) = "' + re.escape(kb_name) + '"',
                line
            )
            if m:
                elem_idx = int(m.group(1))
                break
            # 包含匹配（知识库名可能带前缀/后缀）
            m = re.search(
                r'\[(\d+)\] (?:AXStaticText|AXButton|AXLink|AXRow) = ".*' + re.escape(kb_name) + r'.*"',
                line
            )
            if m:
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
                title = get_kb_window_title(kb_name)
                if kb_name in title:
                    log(f"  ✅ 已导航到 {kb_name} 知识库")
                    time.sleep(2)
                    return True
                if wait < 7:
                    log(f"    等待页面加载... ({(wait+1)*2.5}秒)")

            log(f"  ⚠️  点击后窗口标题不包含 '{kb_name}'，标题: '{title}'")
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

    # 先检查是否已经在目标知识库
    title = get_kb_window_title(kb_name)
    if kb_name in title:
        log(f"✅ 已在 {kb_name} 知识库")
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

def save_to_obsidian(kb_name: str = None, dry_run: bool = False) -> dict:
    """
    调用 Obsidian 保存器

    返回统计信息: {saved, failed}
    """
    log(f"\n{'─'*40}")
    log(f"保存到 Obsidian...")

    cmd = [
        "python3",
        "-u",  # 禁用输出缓冲，实时查看保存进度
        Path(__file__).parent / "ima_obsidian_saver.py",
        "--limit", "1000",  # 每次最多保存 1000 篇
    ]

    # 指定知识库：只保存该 KB 的文章，并存入对应文件夹（避免不同 KB 混入同一文件夹）
    if kb_name:
        cmd.extend(["--kb", kb_name, "--des", kb_name])

    if dry_run:
        cmd.append("--dry-run")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=7200,  # 2小时超时
            cwd=Path(__file__).parent
        )

        # 记录输出
        if result.stdout:
            for line in result.stdout.split("\n"):
                if line.strip():
                    log(f"  {line}", print_too=False)

        if result.returncode != 0:
            log(f"❌ Obsidian 保存失败")
            if result.stderr:
                log(f"错误: {result.stderr}")
            return {"saved": 0, "failed": 0}

        # 解析统计
        saved_count = 0
        for line in result.stdout.split("\n"):
            if "本次成功" in line:
                try:
                    saved_count = int(line.split(":")[-1].strip().split()[0])
                except (ValueError, IndexError):
                    pass

        log(f"✅ 保存完成: {saved_count} 篇")
        return {"saved": saved_count, "failed": 0}

    except subprocess.TimeoutExpired:
        log(f"❌ Obsidian 保存超时")
        return {"saved": 0, "failed": 0}
    except Exception as e:
        log(f"❌ Obsidian 保存失败: {e}")
        return {"saved": 0, "failed": 0}


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
            return {"new": 0, "skipped": 0, "failed": 0}

    # 获取 IMA 窗口
    window = get_ima_main_window()
    if not window:
        log("❌ 未找到 IMA 窗口")
        return {"new": 0, "skipped": 0, "failed": 0}

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
            return {"new": 0, "skipped": 0, "failed": 0}

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
        return {"new": 0, "skipped": 0, "failed": 0}
    except Exception as e:
        log(f"❌ {kb_name} 提取失败: {e}")
        return {"new": 0, "skipped": 0, "failed": 0}


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

    # 总计统计
    total_new = 0
    total_skipped = 0
    total_saved = 0

    # 逐个处理知识库
    for i, kb_name in enumerate(kbs, 1):
        log(f"\n[{i}/{len(kbs)}] 处理知识库: {kb_name}")

        # 提取文章
        stats = update_knowledge_base(kb_name, args.dry_run)
        total_new += stats["new"]
        total_skipped += stats["skipped"]

        # 如果有新文章且不是 --no-save 模式，保存到 Obsidian
        if stats["new"] > 0 and not args.no_save and not args.dry_run:
            save_stats = save_to_obsidian(kb_name)
            total_saved += save_stats["saved"]

        # 知识库之间等待
        if i < len(kbs):
            log(f"等待 {WAIT_BETWEEN_KB} 秒后处理下一个知识库...")
            if not args.dry_run:
                time.sleep(WAIT_BETWEEN_KB)

    # 注释掉统一保存，避免重复处理已在上面逐个保存过的文章
    # 如果需要统一保存，可以在不指定 --des 的情况下单独调用保存器
    # if not args.no_save and len(kbs) > 1:
    #     log(f"\n{'='*50}")
    #     log(f"统一保存到 Obsidian...")
    #     save_stats = save_to_obsidian(dry_run=args.dry_run)
    #     total_saved += save_stats["saved"]

    # 总结
    log(f"\n{'='*60}")
    log(f"增量更新完成")
    log(f"{'='*60}")
    log(f"总计新增: {total_new} 篇")
    log(f"总计跳过: {total_skipped} 篇")
    log(f"保存到 Obsidian: {total_saved} 篇")


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
