#!/usr/bin/env python3
"""
IMA AI 知识库 URL 提取器 - UI-TARS API 版本

集成本地 UI-TARS API 服务（端口 8001）进行界面元素识别。

优势：
- 自动识别界面元素，坐标变化时也能适应
- 可用于其他知识库的提取
- 可视化调试能力
"""

import asyncio
import sys
import json
import subprocess
import sqlite3
import time
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List
from PIL import Image
import base64
from io import BytesIO


# ==================== 配置 ====================

DB_FILE = Path("/Users/berton/Github/cua/ima_articles.db")
SCREENSHOT_DIR = Path("/Users/berton/Github/cua/screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

CUA_DRIVER = "/Users/berton/.local/bin/cua-driver"
IMA_APP_NAME = "ima.copilot"

# UI-TARS API 配置
UITARS_API_URL = "http://localhost:8001/v1/chat/completions"

# 时间配置
WAIT_AFTER_CLICK = 5.0  # 增加等待时间
WAIT_AFTER_BACK = 1.0
WAIT_AFTER_NAVIGATION = 2.0

# 停止条件
MAX_ARTICLES = 500
MAX_EMPTY_URLS = 5


# ==================== 数据库函数 ====================

def init_database():
    """初始化数据库"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
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

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_url ON articles(url)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_kb ON articles(knowledge_base)")

    conn.commit()
    conn.close()


def article_exists(url: str) -> bool:
    """检查 URL 是否已存在"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM articles WHERE url = ? LIMIT 1", (url,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists


def save_article(url: str, title: str, kb: str, y_pos: int):
    """保存文章到数据库"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO articles
            (url, title, knowledge_base, y_position, status)
            VALUES (?, ?, ?, ?, 'success')
        """, (url, title, kb, y_pos))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"  ⚠️  保存失败: {e}")
        return False


# ==================== cua-driver 函数 ====================

def run_driver(command: str, args: List[str] = None) -> str:
    """运行 cua-driver 命令"""
    cmd = [CUA_DRIVER, command]
    if args:
        cmd.extend(args)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30
    )

    if result.returncode != 0:
        raise RuntimeError(f"cua-driver {command} failed: {result.stderr}")

    return result.stdout


def list_windows() -> List[Dict]:
    """获取窗口列表"""
    output = run_driver("list_windows")
    data = json.loads(output)
    return data.get("windows", [])


def get_ima_window() -> Optional[Dict]:
    """获取 IMA 窗口"""
    windows = list_windows()
    ima_windows = [
        w for w in windows
        if IMA_APP_NAME.lower() in w.get("app_name", "").lower()
    ]

    if not ima_windows:
        return None

    MIN_WIDTH, MIN_HEIGHT = 500, 400
    for window in ima_windows:
        bounds = window.get("bounds", {})
        if bounds.get("width", 0) >= MIN_WIDTH and bounds.get("height", 0) >= MIN_HEIGHT:
            return window

    return ima_windows[0]


def capture_window_screenshot(window_id: str) -> Path:
    """截取窗口截图"""
    from PIL import Image

    windows = list_windows()
    target = next((w for w in windows if w.get("window_id") == window_id), None)

    if not target:
        raise RuntimeError(f"窗口 {window_id} 不存在")

    bounds = target.get("bounds", {})
    x, y = bounds.get("x", 0), bounds.get("y", 0)
    width, height = bounds.get("width", 0), bounds.get("height", 0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    full_path = SCREENSHOT_DIR / f"full_{timestamp}.png"
    crop_path = SCREENSHOT_DIR / f"crop_{timestamp}.png"

    # 截取全屏
    subprocess.run(["screencapture", "-x", str(full_path)], check=True)

    # 裁剪窗口区域
    with Image.open(full_path) as img:
        img_width, img_height = img.size
        crop_x = max(0, min(x, img_width))
        crop_y = max(0, min(y, img_height))
        crop_width = min(width, img_width - crop_x)
        crop_height = min(height, img_height - crop_y)

        cropped = img.crop((crop_x, crop_y, crop_x + crop_width, crop_y + crop_height))
        cropped.save(crop_path)

    full_path.unlink()
    return crop_path


def click_at(x: int, y: int, window_id: str):
    """点击指定坐标"""
    windows = list_windows()
    target = next((w for w in windows if w.get("window_id") == window_id), None)

    if not target:
        raise RuntimeError(f"窗口 {window_id} 不存在")

    pid = target.get("pid")
    if not pid:
        raise RuntimeError(f"窗口 {window_id} 没有 pid")

    params = json.dumps({"pid": pid, "x": x, "y": y})
    run_driver("call", ["click", params])


def press_key(key: str, modifiers: List[str] = None, window_id: str = None):
    """按下键盘按键"""
    pid = None

    if window_id:
        windows = list_windows()
        target = next((w for w in windows if w.get("window_id") == window_id), None)
        if target:
            pid = target.get("pid")

    if not pid:
        ima_window = get_ima_window()
        if ima_window:
            pid = ima_window.get("pid")

    if not pid:
        raise RuntimeError("无法确定目标进程 PID")

    params = {"pid": pid, "key": key}
    if modifiers:
        params["modifiers"] = modifiers

    try:
        run_driver("call", ["press_key", json.dumps(params)])
    except RuntimeError as e:
        # 如果按键失败，尝试使用替代键
        if key == "page_down":
            # 使用滚动代替
            scroll_window(window_id if window_id else get_ima_window().get("window_id"), 500)
        else:
            raise e


def scroll_window(window_id: str, amount: int = 500):
    """滚动窗口"""
    windows = list_windows()
    target = next((w for w in windows if w.get("window_id") == window_id), None)

    if not target:
        raise RuntimeError(f"窗口 {window_id} 不存在")

    pid = target.get("pid")
    if not pid:
        raise RuntimeError(f"窗口 {window_id} 没有 pid")

    # 使用 direction 和 amount
    direction = "down" if amount > 0 else "up"
    params = json.dumps({"pid": pid, "direction": direction, "amount": abs(amount)})
    run_driver("call", ["scroll", params])


# ==================== UI-TARS API 函数 ====================

def image_to_base64(image_path: Path) -> str:
    """将图像转换为 base64"""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def call_uitars_api(image_path: Path, instruction: str) -> Optional[Dict]:
    """调用 UI-TARS API 分析图像"""

    try:
        # 读取并编码图像
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        # 构建请求
        payload = {
            "model": "ui-tars-1.5-7B-6bit",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}}
                    ]
                }
            ],
            "max_tokens": 512
        }

        # 发送请求
        response = requests.post(UITARS_API_URL, json=payload, timeout=30)
        response.raise_for_status()

        # 解析响应
        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")

        # 尝试解析 JSON
        import re
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            return json.loads(json_match.group(0))

        return None

    except Exception as e:
        print(f"  ⚠️  UI-TARS API 调用失败: {e}")
        return None


def find_kb_icon(screenshot_path: Path) -> Optional[tuple]:
    """使用 UI-TARS 查找知识库图标"""
    instruction = """
找到界面左侧的知识库图标（通常是一个灯泡形状）。
返回图标中心的点击坐标：
{"x": 坐标x, "y": 坐标y}
"""

    result = call_uitars_api(screenshot_path, instruction)
    if result:
        x = result.get("x")
        y = result.get("y")
        if x and y:
            return int(x), int(y)

    return None


def analyze_articles_list(screenshot_path: Path) -> List[Dict]:
    """分析文章列表"""
    instruction = """
分析这个文章列表界面，识别所有可见的文章。
返回格式：
{
  "articles": [
    {"title": "文章标题", "x": 坐标x, "y": 坐标y},
    ...
  ]
}
"""

    result = call_uitars_api(screenshot_path, instruction)
    if result:
        return result.get("articles", [])

    return []


def verify_ai_kb(screenshot_path: Path) -> Dict:
    """验证当前是否在 AI 知识库"""
    instruction = """
分析这个界面，判断：
1. 当前是否在知识库文章列表页面
2. 如果是，当前选中的是哪个知识库（例如"AI"、"默认"等）
3. 界面标题显示什么

返回格式：
{
  "is_kb_list": true/false,
  "kb_name": "知识库名称",
  "page_title": "页面标题",
  "confidence": "确信度(high/medium/low)"
}
"""

    result = call_uitars_api(screenshot_path, instruction)
    return result or {}


def find_ai_kb_entry(screenshot_path: Path) -> Optional[tuple]:
    """查找 AI 知识库条目的点击位置"""
    instruction = """
在左侧知识库列表中找到包含"AI"关键词的知识库条目（如"AI"、"开源与AI"等）。
直接返回该条目的点击坐标，格式为：
{"x": 数字, "y": 数字}
"""

    result = call_uitars_api(screenshot_path, instruction)
    if result:
        x = result.get("x")
        y = result.get("y")
        if x and y:
            print(f"  ✅ 找到 AI 知识库条目 at ({x}, {y})")
            return int(x), int(y)

    print("  ⚠️  未找到 AI 知识库条目")
    return None


# ==================== AppleScript 提取函数 ====================

def activate_ima():
    """激活 IMA 应用到前台"""
    script = '''
    tell application "ima.copilot"
        activate
    end tell
    delay 0.5
    '''
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        return True
    except:
        return False


def extract_url_ax() -> Optional[str]:
    """使用 AppleScript 从 AXDocument 提取 URL"""
    activate_ima()

    script = '''
    tell application "System Events"
        tell process "ima.copilot"
            if exists window 1 then
                try
                    set docUrl to value of attribute "AXDocument" of window 1
                    return docUrl
                on error
                    return ""
                end try
            end if
        end tell
    end tell
    return ""
    '''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5
        )

        url = result.stdout.strip()

        if url and "mp.weixin.qq.com" in url:
            return url
        elif url and url.startswith("http"):
            return url

        return None
    except Exception as e:
        return None


def extract_url_clipboard(window_id: str) -> Optional[str]:
    """通过剪贴板提取 URL"""
    try:
        press_key("l", ["command"], window_id)
        time.sleep(0.3)

        press_key("a", ["command"], window_id)
        time.sleep(0.2)

        press_key("c", ["command"], window_id)
        time.sleep(0.3)

        clipboard = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True
        ).stdout.strip()

        if clipboard and "mp.weixin.qq.com" in clipboard:
            return clipboard
        elif clipboard and clipboard.startswith("http"):
            return clipboard

        return None
    except Exception as e:
        return None


def extract_title_ax() -> Optional[str]:
    """使用 AppleScript 从窗口标题提取文章标题"""
    script = '''
    tell application "System Events"
        set frontApp to name of first process whose frontmost is true
        tell process frontApp
            if exists window 1 then
                set winTitle to title of window 1
                return winTitle
            end if
        end tell
    end tell
    return ""
    '''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5
        )

        title = result.stdout.strip()
        return title if title else None
    except Exception as e:
        return None


# ==================== 核心提取逻辑 ====================

async def navigate_to_ai_kb(window_id: str):
    """导航到 AI 知识库（使用 UI-TARS 识别并验证）"""
    print("\n【步骤 1】查找并点击知识库图标")

    # 截图
    screenshot = capture_window_screenshot(window_id)
    print(f"  ✅ 截图已保存: {screenshot.name}")

    # 使用 UI-TARS 查找知识库图标
    print("  正在使用 UI-TARS 识别知识库图标...")
    kb_pos = find_kb_icon(screenshot)

    if kb_pos:
        x, y = kb_pos
        print(f"  ✅ 找到知识库图标: ({x}, {y})")
    else:
        # 使用默认位置
        x, y = 32, 75
        print(f"  ⚠️  未识别到图标，使用默认位置: ({x}, {y})")

    # 点击知识库图标
    click_at(x, y, window_id)
    await asyncio.sleep(WAIT_AFTER_NAVIGATION)

    print("\n【步骤 2】查找并点击 AI 知识库条目")
    # 截图知识库列表
    kb_list_screenshot = capture_window_screenshot(window_id)
    print("  正在使用 UI-TARS 查找 AI 知识库条目...")

    ai_entry_pos = find_ai_kb_entry(kb_list_screenshot)

    if ai_entry_pos:
        x, y = ai_entry_pos
        print(f"  点击 AI 知识库条目: ({x}, {y})")
        click_at(x, y, window_id)
    else:
        # 使用调试脚本找到的坐标
        print("  使用已知的 AI 知识库坐标: (100, 300)")
        click_at(100, 300, window_id)

    await asyncio.sleep(WAIT_AFTER_NAVIGATION)

    print("\n【步骤 3】验证是否进入 AI 知识库")
    # 验证当前位置
    verify_screenshot = capture_window_screenshot(window_id)
    verification = verify_ai_kb(verify_screenshot)

    if verification:
        is_kb = verification.get("is_kb_list", False)
        kb_name = verification.get("kb_name", "未知")
        confidence = verification.get("confidence", "low")

        print(f"  验证结果:")
        print(f"    - 在知识库列表: {is_kb}")
        print(f"    - 知识库名称: {kb_name}")
        print(f"    - 确信度: {confidence}")

        if is_kb and "AI" in kb_name:
            print(f"\n✅ 已成功进入 AI 知识库")
            return True
        elif is_kb:
            print(f"\n⚠️  当前在 '{kb_name}' 知识库，不是 AI 知识库")
            print("   将继续尝试提取，但可能无法获取正确的文章...")
            return False
        else:
            print(f"\n⚠️  无法确认当前位置")
            return False
    else:
        print("  ⚠️  验证失败，无法识别当前界面")
        return False


async def extract_articles_with_uitars(window_id: str):
    """使用 UI-TARS 提取文章"""

    print("\n" + "=" * 60)
    print("开始批量提取（UI-TARS 版本）")
    print("=" * 60)

    total_new = 0
    total_skipped = 0
    page_num = 1

    while page_num <= 20:  # 最多 20 页
        print(f"\n第 {page_num} 页")

        # 截图并分析文章列表
        print("  正在截图并分析文章列表...")
        screenshot = capture_window_screenshot(window_id)

        articles = analyze_articles_list(screenshot)

        if not articles:
            print("  ⚠️  未识别到文章，可能已到列表底部")
            break

        print(f"  ✅ 识别到 {len(articles)} 篇文章")

        # 处理每篇文章
        for i, article in enumerate(articles):
            title = article.get("title", f"文章{i+1}")
            x = article.get("x", 400)
            y = article.get("y", 300)

            print(f"\n{'─'*50}")
            print(f"[{total_new + total_skipped + 1}] {title[:50]}...")
            print(f"{'─'*50}")

            # 点击文章
            print(f"  点击位置: ({x}, {y})")
            click_at(x, y, window_id)

            print(f"  等待加载... ({WAIT_AFTER_CLICK}秒)")
            await asyncio.sleep(WAIT_AFTER_CLICK)

            # 提取 URL
            print("  提取 URL...")
            url = extract_url_ax()

            if not url:
                print("  尝试剪贴板方法...")
                url = extract_url_clipboard(window_id)

            if url:
                print(f"  ✅ URL: {url[:60]}...")

                # 检查是否已存在
                if article_exists(url):
                    print(f"  ℹ️  文章已存在，跳过")
                    total_skipped += 1
                else:
                    # 提取标题
                    title_extracted = extract_title_ax()
                    print(f"  ✅ 标题: {title_extracted[:50] if title_extracted else 'Unknown'}...")

                    # 保存
                    save_article(url, title_extracted or title, "AI", y)
                    total_new += 1
                    print(f"  ✅ 新文章已保存 (总计: {total_new})")
            else:
                print(f"  ⚠️  未提取到 URL")

            # 返回列表
            print("  返回列表...")
            press_key("escape", [], window_id)
            await asyncio.sleep(WAIT_AFTER_BACK)

        # 翻页
        print(f"\n本页进度: 新增 {total_new}, 跳过 {total_skipped}")
        print("滚动加载更多...")

        # 使用滚动翻页
        scroll_window(window_id, 800)
        await asyncio.sleep(2)
        page_num += 1

    # 总结
    print("\n" + "=" * 60)
    print("提取完成")
    print("=" * 60)

    print(f"\n本次运行统计:")
    print(f"  新提取: {total_new} 篇")
    print(f"  已跳过: {total_skipped} 篇")
    print(f"  翻页数: {page_num} 页")


async def main():
    """主函数"""

    print("\n" + "=" * 60)
    print("IMA AI 知识库 URL 提取器（UI-TARS API 版本）")
    print("=" * 60)
    print()

    # 初始化数据库
    init_database()
    print(f"✅ 数据库已初始化: {DB_FILE}")

    # 检查 UI-TARS API
    try:
        response = requests.get(UITARS_API_URL.replace("/v1/chat/completions", "/health"), timeout=5)
        print(f"✅ UI-TARS API 服务正常")
    except:
        print(f"⚠️  无法连接到 UI-TARS API ({UITARS_API_URL})")
        print("   请确保 UI-TARS 服务正在运行")

    # 检查 cua-driver
    try:
        version = run_driver("--version")
        print(f"✅ cua-driver 版本: {version.strip()}")
    except Exception as e:
        print(f"❌ cua-driver 不可用: {e}")
        sys.exit(1)

    # 获取 IMA 窗口
    print("\n查找 IMA 窗口...")
    ima_window = get_ima_window()

    if not ima_window:
        print("❌ 未找到 IMA 窗口")
        sys.exit(1)

    print(f"✅ 找到 IMA 窗口:")
    print(f"   应用: {ima_window.get('app_name')}")

    window_id = ima_window.get("window_id")

    # 导航到 AI 知识库
    await navigate_to_ai_kb(window_id)

    # 开始批量提取
    await extract_articles_with_uitars(window_id)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
