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
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ==================== 配置 ====================

# 要监控的知识库列表
KNOWLEDGE_BASES = [
    "AI",
    "投资人生",
    "英语教与学",
    "Andrew",
    "皮皮鲁的知识库",
]

LOG_FILE = Path(__file__).parent / "incremental_update.log"

# cua-driver 路径
CUA_DRIVER = "/Users/berton/.local/bin/cua-driver"
IMA_APP_NAME = "ima.copilot"

WAIT_BETWEEN_KB = 5.0  # 知识库之间等待时间


# ==================== 日志 ====================

def log(message: str, print_too: bool = True):
    """写入日志文件"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    if print_too:
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


def get_ima_main_window():
    try:
        output = run_cua(["list_windows"])
        data = json.loads(output)
    except Exception as e:
        log(f"list_windows 失败: {e}")
        return None

    windows = data.get("windows", [])
    ima_windows = [
        w for w in windows
        if IMA_APP_NAME.lower() in w.get("app_name", "").lower()
        and w.get("bounds", {}).get("height", 0) > 400
    ]

    if not ima_windows:
        return None
    return max(ima_windows, key=lambda w: w["bounds"].get("width", 0) * w["bounds"].get("height", 0))


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


def activate_ima():
    """激活 IMA 应用，如果未运行则启动"""
    if not is_ima_running():
        launch_ima()

    subprocess.run(
        ["osascript", "-e", 'tell application "ima.copilot" to activate'],
        capture_output=True, timeout=5
    )
    time.sleep(1)


def navigate_to_kb(kb_name: str) -> bool:
    """
    通过 cua-driver 自动导航到指定知识库

    在侧边栏找到知识库名称并点击切换

    返回: True 表示成功点击
    """
    window = get_ima_main_window()
    if not window:
        log("❌ 未找到 IMA 窗口，无法导航")
        return False

    pid = window["pid"]
    window_id = window["window_id"]

    # 获取窗口状态
    import re
    state_result = run_cua(["call", "get_window_state", json.dumps({"pid": pid, "window_id": window_id})])
    state = json.loads(state_result)
    md = state.get("tree_markdown", "")

    # 在侧边栏查找知识库名称的 element_index
    # 侧边栏知识库列表结构: AXStaticText = "知识库名称"
    for line in md.split("\n"):
        # 匹配 AXStaticText 且文本完全等于知识库名称
        m = re.search(r'\[(\d+)\] AXStaticText = "' + re.escape(kb_name) + '"', line)
        if m:
            elem_idx = int(m.group(1))
            log(f"找到知识库 '{kb_name}' (element {elem_idx})，点击导航...")

            # 点击知识库名称
            click_result = run_cua(["call", "click", json.dumps({
                "pid": pid,
                "window_id": window_id,
                "element_index": elem_idx
            })])

            # 等待页面加载并验证
            for wait in range(5):  # 最多等待 5 次，每次 2 秒
                time.sleep(2)
                title = get_kb_window_title(kb_name)
                if kb_name in title:
                    log(f"✅ 已导航到 {kb_name} 知识库 (等待 {(wait+1)*2}秒)")
                    # 额外等待文章列表渲染
                    time.sleep(2)
                    return True
                log(f"  等待页面加载... ({(wait+1)*2}秒)")

            log(f"⚠️  点击后窗口标题不包含 '{kb_name}'，标题: {title}")
            return False

    log(f"⚠️  在侧边栏未找到知识库 '{kb_name}'")
    return False


def ensure_ima_ready(kb_name: str, timeout: int = 60) -> bool:
    """
    确保 IMA 已就绪并位于目标知识库

    先尝试自动导航，如果失败则等待

    返回: True 表示就绪，False 表示超时
    """
    log(f"确保 IMA 位于 {kb_name} 知识库...")

    # 先检查是否已经在目标知识库
    title = get_kb_window_title(kb_name)
    if kb_name in title:
        log(f"✅ 已确认在 {kb_name} 知识库")
        return True

    # 尝试自动导航
    activate_ima()
    time.sleep(1)
    if navigate_to_kb(kb_name):
        return True

    # 自动导航失败，等待用户手动切换
    log(f"⏳ 自动导航失败，等待手动切换...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        title = get_kb_window_title(kb_name)
        if kb_name in title:
            log(f"✅ 已确认在 {kb_name} 知识库")
            return True
        elapsed = int(time.time() - start_time)
        if elapsed % 10 == 0 and elapsed > 0:
            log(f"⏳ 等待切换到 {kb_name} 知识库... ({elapsed}/{timeout}秒)")
        time.sleep(2)

        time.sleep(2)

    log(f"⚠️  等待超时，未能确认在 {kb_name} 知识库")
    return False


def get_kb_window_title(kb_name: str) -> str:
    """获取知识库窗口标题"""
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
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except Exception:
        return ""


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
        Path(__file__).parent / "ima_obsidian_saver.py",
        "--limit", "1000",  # 每次最多保存 1000 篇
    ]

    # 指定目标文件夹
    if kb_name:
        cmd.extend(["--des", kb_name])

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

    # 检查 cua-driver
    if not args.dry_run and not is_daemon_running():
        log("❌ cua-driver daemon 未运行")
        log("   请先启动: cua-driver serve &")
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

    # 如果不是逐个保存，最后统一保存
    if not args.no_save and len(kbs) > 1:
        log(f"\n{'='*50}")
        log(f"统一保存到 Obsidian...")
        save_stats = save_to_obsidian(dry_run=args.dry_run)
        total_saved += save_stats["saved"]

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
