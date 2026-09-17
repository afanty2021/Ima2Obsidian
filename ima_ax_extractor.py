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
from contextlib import closing
import subprocess
import sys
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

# 导入公共模块
from ima_common import (
    DB_FILE, CUA_DRIVER, IMA_APP_NAME, run_cua, is_daemon_running, init_database,
    get_ima_main_window, find_cliclick,
)

# ==================== 配置 ====================

WAIT_CLICK_LOAD = 3.0
WAIT_AFTER_CLOSE = 1.5
WAIT_SCROLL = 2.0
WAIT_ACTIVATE = 0.5

MAX_PAGES = 65
# 连续已存在早停阈值。语义（2026-09-17 起）：每页先做标题预检，全部命中直接停；
# 有未知候选的页整页走完，本阈值放宽为「候选数 + MAX_CONSECUTIVE_SEEN」，
# 仅作候选全为误报（标题匹配系统性失准）时的保险丝
MAX_CONSECUTIVE_SEEN = 2
# 点击后校验"打开的是本篇"的最大尝试次数（含首次点击；失败即重试点击）
CLICK_VERIFY_ATTEMPTS = 3


_CLICLICK_PATH = find_cliclick()


# ==================== 幻影跳过防护 ====================
#
# 2026-09-17 实证（英语教与学 首屏第 2 篇不在库却报"已存在"）：点击未生效/未切换时，
# extract_url_ax 读到旧文章标签页的 URL，url_exists 误判"已存在"计入连续命中，
# 两连即触发 MAX_CONSECUTIVE_SEEN 早停——列表顶部真新文章被静默跳过。
# 因此点击后必须先确认打开的是本篇，幻影 URL 绝不进入 url_exists。

# 列表标题 vs 文章页标题常有空白/标点/前后缀微差，比较前统一剥离
_TITLE_COMPARE_NOISE = re.compile(
    r"[\s「」『』“”‘’\"'《》【】()\[\]（）·：:，,。．、！!？?\-—–~～*…]+"
)


def normalize_title_for_compare(title: Optional[str]) -> str:
    """剥离空白与中英文标点后转小写，供标题容错比较。"""
    return _TITLE_COMPARE_NOISE.sub("", title or "").lower()


def is_same_article_page(
    list_title: Optional[str],
    page_title: Optional[str],
    url: Optional[str],
    prev_url: Optional[str],
) -> bool:
    """判断点击后打开的文章页是否就是列表里点的那篇。

    - page_title 非空：归一化后相等，或短方(≥6字)为长方子串（容忍截断/前后缀差异）。
    - page_title 为空（AXDocument 未就绪；launchd 下 osascript 读 AX 被 TCC 拦截，
      extract_title_ax 恒 None）：退化为 URL 基线——与上一篇已验证 URL 相同即判幻影，
      不同则放行。首篇无基线可被会话残留标签页漏过，但该卡失败路径的
      cmd_w_close(None) 会关闭任意残留标签页，且单发幻影不足以触发早停。
    """
    norm_page = normalize_title_for_compare(page_title)
    if not norm_page:
        return bool(url) and url != prev_url
    norm_list = normalize_title_for_compare(list_title)
    if norm_page == norm_list:
        return True
    short, long_ = sorted((norm_page, norm_list), key=len)
    return len(short) >= 6 and short in long_


def ax_tree_menu_bar_only(md: str) -> bool:
    """AX 树是否已脱落成「仅菜单栏」形态（有 AXMenuBar 而 AXStaticText 为 0）。

    2026-09-17 实证：新版 ima 的 web AX 会被 macOS 回收（App Nap 类机制）成
    仅菜单栏，element_count 仍 >100（菜单 265 项），数量阈值区分不了；此态下
    点击按在死元素上、状态读取耗满 run_cua 30s 超时，每张卡可磨 8-10 分钟。
    空 md 视为未知，不算脱落（不误触发中止）。"""
    return bool(md) and "AXMenuBar" in md and "AXStaticText" not in md


def _read_main_window_md() -> str:
    """廉价读取主窗口 AX markdown（脱落探测用）；失败返回空串（视为未知）。"""
    try:
        w = get_ima_main_window()
        if not w:
            return ""
        st = get_window_state(w["pid"], w["window_id"])
        return (st or {}).get("tree_markdown", "")
    except Exception:
        return ""


# ==================== URL 规范化 ====================

def normalize_url(url: str) -> str:
    """
    规范化 URL，去除不影响文章内容的动态参数。

    保证以下性质：
      - 幂等：反复应用得到相同结果
      - 顺序无关：长格式核心参数 (__biz/mid/idx/sn) 任意顺序都规范化到同一结果
      - 无碰撞：不把两条不同 URL 折叠到同一规范形式

    主要处理平台：
      - 微信短格式 (/s/ARTICLE_ID)：去除全部 ? 参数和 # 锚点
      - 微信长格式 (/s?__biz=...&mid=...&idx=...&sn=...)：仅保留四个核心参数并排序
      - 知乎：去除全部动态参数
      - 通用：去除追踪参数（utm_*, ref, source, from, scene, _t, ...），保留其余并排序

    已知限制（不在本层修复）：
      微信短格式 /s/ID 与长格式 /s?__biz=..&mid=.. 指向同一篇文章时，
      在不发起网络请求的前提下无法判定等价。跨形式去重需要在 saver/extractor
      层通过 URL 解析回调或额外索引列处理。

    Args:
        url: 原始 URL

    Returns:
        规范化后的 URL；空输入原样返回
    """
    if not url:
        return url

    # 先去除 #fragment（如微信 #rd），保证所有后续分支不必重复处理
    url = url.split('#', 1)[0]

    # hostname 精确匹配（与 save_article 白名单一致，review PR#12 #2：
    # 子串匹配会让含 mp.weixin.qq.com 的非微信 URL 误入微信规范化分支）
    if urlparse(url).hostname == 'mp.weixin.qq.com':
        # 短格式: /s/ARTICLE_ID — 去除所有查询参数
        if '/s/' in url:
            return url.split('?', 1)[0]

        # 长格式: /s?__biz=...&mid=...&idx=...&sn=...（核心参数按固定顺序规范）
        if '?' in url:
            base, params = url.split('?', 1)
            param_list = params.split('&')
            # 按 (__biz, mid, idx, sn) 固定顺序排列核心参数；
            # 任意输入顺序都映射到同一规范形式（顺序无关性）
            core_order = ('__biz', 'mid', 'idx', 'sn')
            core_params = []
            for key in core_order:
                for p in param_list:
                    if p.split('=', 1)[0] == key:
                        core_params.append(p)
                        break
            if core_params:
                return f"{base}?{'&'.join(core_params)}"
            # 无核心参数：fall through 到通用处理，避免不同 URL 折叠到 bare base
            # （例如 /s?scene=1 与 /s?scene=2 不应都折叠成 /s）

    # 知乎：去除所有动态参数
    if 'zhihu.com' in url:
        return url.split('?', 1)[0]

    # 通用（含上面 fall-through 的微信长格式无核心参数情形）：
    # 去除追踪参数，保留内容参数。两类匹配：
    #   (1) 前缀匹配：仅 'utm_'（行业标准追踪前缀，无歧义）
    #   (2) 精确匹配：'ref'/'source' 等单字追踪参数必须精确匹配，
    #       不能 startswith——否则会误剥 'ref_id'/'source_id' 等内容参数，
    #       导致不同内容的 URL 折叠到同一规范形式 → UNIQUE 约束触发误判重复 → 漏存。
    if '?' in url:
        base, params = url.split('?', 1)
        param_list = params.split('&')
        # 仅 'utm_' 用前缀匹配（无歧义）；其余单字追踪参数用精确匹配
        TRACKING_EXACT = {
            'ref', 'ref_src', 'ref_url',  # 不剥 'ref_id'（内容参数）
            'source', 'from', 'scene', 'sessionid',
            'share', 'clicktime', '_t', 'timestamp',
        }
        kept = []
        for param in param_list:
            key = param.split('=', 1)[0]
            if key.startswith('utm_'):
                continue
            if key in TRACKING_EXACT:
                continue
            kept.append(param)
        kept.sort()
        return f"{base}?{'&'.join(kept)}" if kept else base

    return url


# ==================== 数据库 ====================

def _is_wechat_url(url: str) -> bool:
    """判断 URL 是否为允许写入/参与去重的微信公众号文章地址。"""
    return urlparse(url).hostname == "mp.weixin.qq.com"

def url_exists(url: str) -> bool:
    if not _is_wechat_url(url):
        return False
    # 使用规范化后的 URL 进行去重检查
    normalized_url = normalize_url(url)
    with closing(sqlite3.connect(DB_FILE)) as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM articles WHERE url = ? LIMIT 1", (normalized_url,))
        return c.fetchone() is not None


def save_article(url: str, title: str, kb: str) -> bool:
    # 白名单：只接受微信公众号文章（hostname 精确匹配 mp.weixin.qq.com）。
    # 排除 IMA 界面公告等非文章卡片被误识别（如"copilot 功能上线"→ github.com）。
    # 用 hostname 精确匹配而非子串（review PR#12 #5：子串可被
    # /path?redirect=mp.weixin.qq.com 绕过）。若未来知识库含其他平台文章，扩展此检查。
    if not _is_wechat_url(url):
        print("  ⚠️  跳过非微信 URL（疑似非文章卡片）：{}".format(url[:60]))
        return False
    try:
        # 使用规范化后的 URL 进行保存
        normalized_url = normalize_url(url)
        # closing 保证异常路径也关闭连接（避免长提取任务里 fd 泄漏）
        with closing(sqlite3.connect(DB_FILE)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT OR IGNORE INTO articles (url, title, knowledge_base, status)
                VALUES (?, ?, ?, 'success')
            """, (normalized_url, title, kb))
            conn.commit()
        return True
    except Exception as e:
        print(f"  ⚠️  保存失败: {e}")
        return False


def get_stats() -> Dict:
    with closing(sqlite3.connect(DB_FILE)) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM articles")
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(DISTINCT knowledge_base) FROM articles")
        kb_count = c.fetchone()[0]
    return {"total": total, "kb_count": kb_count}


# ==================== cua-driver ====================

def run_cua_call(tool: str, params: Dict) -> Optional[Dict]:
    """
    调用 cua-driver tool，统一处理三种成功响应与失败：

      - JSON 输出 → 解析为 dict
      - 非 JSON 文本（如 click 的纯文本回复）→ 包装为 {"raw": ...}
      - 空/仅空白输出（click/scroll 成功时常如此）→ 返回 {}（视为成功，无 payload）
      - run_cua 抛 RuntimeError（非零退出码）→ 返回 None（调用方据此判失败）
      - run_cua 抛 subprocess.TimeoutExpired（cua-driver 超时）→ 返回 None
        历史问题：旧实现只捕 RuntimeError，TimeoutExpired 穿透到 extractor main
        把整个提取流程崩掉（cua-driver 卡死场景）。

    历史问题：旧实现把空输出当作"无返回"返回 None，导致 click_element 在
    cua-driver 静默成功时误判失败，触发不必要的 AX Tree re-fetch。
    """
    try:
        output = run_cua(["call", tool, json.dumps(params)])
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"  ⚠️  cua-driver call {tool} 失败: {e}")
        return None

    stripped = output.strip()
    if not stripped:
        # 空 stdout + exit 0：cua-driver 已成功执行，仅无 stdout 输出（click/scroll 常见）
        return {}
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # 非 JSON 文本（如 click 返回的 "clicked"），视为成功并包装
        return {"raw": stripped}


def get_window_state(pid: int, window_id: int) -> Optional[Dict]:
    return run_cua_call("get_window_state", {"pid": pid, "window_id": window_id})


def click_element(pid: int, window_id: int, element_index: int) -> bool:
    result = run_cua_call("click", {
        "pid": pid,
        "window_id": window_id,
        "element_index": element_index
    })
    return result is not None


# 定向滚轮的窗口局部坐标缓存（窗口尺寸整个会话恒定，算一次即可）
_SCROLL_LOCAL_COORDS: Optional[dict] = None


def _scroll_local_coords(window_id: int) -> dict:
    """取定向滚轮的窗口局部坐标：宽度中点、y≈45%（列表区，避开左侧 KB 侧边栏）。"""
    global _SCROLL_LOCAL_COORDS
    if _SCROLL_LOCAL_COORDS:
        return _SCROLL_LOCAL_COORDS
    width, height = 1400, 900  # 兜底（自动化窗口长期为 1512x949）
    try:
        wins = json.loads(run_cua(["list_windows"]))["windows"]
        for w in wins:
            if w.get("window_id") == window_id:
                b = w.get("bounds", {})
                width, height = b.get("width", width), b.get("height", height)
                break
    except Exception:
        pass
    _SCROLL_LOCAL_COORDS = {"x": int(width * 0.5), "y": int(height * 0.45)}
    return _SCROLL_LOCAL_COORDS


def scroll_down(pid: int, window_id: int, amount: int = 3):
    # 新版 ima 列表是嵌套 overflow 滚动区，不吃合成按键（PageDown/箭头/无目标
    # 滚轮三路由实测全部无效，2026-09-17）；必须用 cua-driver 定向滚轮路径：
    # window-local x/y 处合成真实滚轮事件（CGEventCreateScrollWheelEvent），
    # 按光标命中测试落在列表滚动区上。
    coords = _scroll_local_coords(window_id)
    run_cua_call("scroll", {
        "pid": pid,
        "window_id": window_id,
        "direction": "down",
        "amount": amount,
        **coords,
    })


# ==================== AppleScript ====================

def activate_ima():
    subprocess.run(
        ["osascript", "-e", 'tell application "ima.copilot" to activate'],
        capture_output=True, timeout=5
    )
    time.sleep(WAIT_ACTIVATE)


def extract_url_ax(pid: int = 0, window_id: int = 0) -> Optional[str]:
    """用 cua-driver 读标签页窗口地址栏 AXTextField 的文章 URL。

    launchd 后台 osascript（System Events 读 AXDocument）无 Accessibility 权限（-25211），
    故改用 cua-driver（com.trycua.driver，已授权 Accessibility）。
    新版 IMA：文章在独立标签页窗口打开，地址栏 AXTextField 含完整 URL。
    流程：bring_to_front 前台化 → 遍历 IMA 大窗口 get_window_state → 找含 http 的 AXTextField。
    pid/window_id 保留兼容调用点。失败返回 None，不 fallback 到正文链接（避免错误数据）。
    """
    import re
    # 前台化 IMA（标签页窗口需前台，cua-driver 才能读到地址栏 AX）
    try:
        mw = get_ima_main_window()
        if mw and mw.get("pid"):
            run_cua(["call", "bring_to_front", json.dumps({"pid": mw["pid"]})])
            time.sleep(1.0)
    except Exception as e:
        print(f"  ⚠️  extract_url_ax bring_to_front 失败: {e}")
    # 地址栏 AXTextField 失焦只显示域名，聚焦(click)后才显示完整 URL。
    # 遍历找标签页窗口(含"地址和搜索栏") → click 地址栏聚焦 → 重读完整 URL。
    for attempt in range(3):
        try:
            wins = json.loads(run_cua(["list_windows"]))["windows"]
        except Exception as e:
            print(f"  ⚠️  extract_url_ax list_windows 失败: {e}")
            return None
        for w in wins:
            if "ima" not in w.get("app_name", "").lower():
                continue
            if w.get("bounds", {}).get("height", 0) <= 200:
                continue
            try:
                st = json.loads(run_cua(["call", "get_window_state", json.dumps({"pid": w["pid"], "window_id": w["window_id"]})]))
            except Exception:
                continue
            md = st.get("tree_markdown", "")
            if "地址和搜索栏" not in md:
                continue  # 非标签页窗口（如 KB 列表主窗口）
            m_addr = re.search(r'\[(\d+)\] AXTextField.*地址和搜索栏', md)
            if not m_addr:
                continue
            # click 地址栏聚焦
            try:
                run_cua(["call", "click", json.dumps({"pid": w["pid"], "window_id": w["window_id"], "element_index": int(m_addr.group(1))})])
                time.sleep(1.0)
            except Exception:
                continue
            # 重读：聚焦后 AXTextField = 完整 URL
            try:
                st2 = json.loads(run_cua(["call", "get_window_state", json.dumps({"pid": w["pid"], "window_id": w["window_id"]})]))
            except Exception:
                continue
            for line in st2.get("tree_markdown", "").split("\n"):
                if "AXTextField" in line:
                    m_url = re.search(r'AXTextField\s*=\s*"(https?://[^"]+)"', line)
                    if m_url and "chrome://" not in m_url.group(1):
                        return m_url.group(1)
        time.sleep(1.5)
    return None


def close_all_article_tabs(max_tabs: int = 5) -> int:
    """关闭所有文章标签页，返回关闭数（探测不到标签页立即返回 0）。

    中断/幻影重试会留下残留标签页，而 extract_url_ax 按 list_windows 顺序读
    第一个含地址栏的窗口——残留标签页排在前面时，校验循环每次都读到旧 URL、
    每次都判幻影，表现为「永远卡在同一篇文章」（2026-09-17 学困生 实证：
    上一轮 Ctrl+C 残留的标签页让下一轮所有卡片幻影化）。
    幻影重试前与走库开始时调用本函数，保证「点击后唯一打开的标签页」无歧义。
    """
    closed = 0
    for _ in range(max_tabs):
        if extract_url_ax() is None:
            break
        cmd_w_close()
        closed += 1
    return closed


def extract_title_ax() -> Optional[str]:
    """从文章窗口标题提取标题（仅对含 http AXDocument 的窗口，避免误取列表窗口标题）"""
    script = '''
tell application "System Events"
    tell process "ima.copilot"
        set wCount to count of windows
        repeat with i from 1 to wCount
            try
                set docUrl to value of attribute "AXDocument" of window i
                if docUrl is not missing value and docUrl starts with "http" then
                    set wTitle to title of window i
                    if wTitle is not "" then
                        return wTitle
                    end if
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
        # Electron 恒给窗口标题追加 " - ima.copilot" 后缀，剥离得到干净文章标题
        # （http AXDocument 门控已确保只取文章窗口，无需再按 app 名过滤）
        suffix = " - ima.copilot"
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
        return title if title else None
    except Exception:
        return None



def cmd_w_close(article_url: Optional[str] = None, max_retries: int = 2) -> bool:
    """Cmd+W 关闭当前文章标签页，校验+重试，返回是否确认关闭。

    keystroke 依赖 ima.copilot 在前台焦点；提取流程多为 cua-driver 后台点击打开
    文章（不激活 IMA），Cmd+W 常因焦点不在 IMA 而失效，导致标签页堆积。故先尝试
    后台 keystroke，校验失败再激活 IMA 重试。

    校验判据：传入 article_url 时，关闭后 extract_url_ax() 不再返回该 URL 即视为
    已关闭（实测 IMA 关闭文章标签后 AXDocument 立即变 None）。未传时用"仍有任意
    文章标签"判据（extract_url_ax() 非 None），保证 URL 提取失败时也校验+激活
    重试，避免标签堆积。
    """
    def send_cmd_w():
        # extract_url_ax 点击地址栏读取完整 URL 后，地址栏（AXTextField）保留键盘焦点。
        # 先发 Escape 让地址栏失焦（焦点回到网页内容区），再发 Cmd+W。
        # 用 cliclick（CGEventPost）替代 osascript keystroke——osascript 在 launchd
        # 后台被 TCC 拦截（与 saver close_tab 同理），cliclick 有 Accessibility 授权。
        if _CLICLICK_PATH:
            subprocess.run(
                [_CLICLICK_PATH, "kp:esc"],
                capture_output=True, timeout=5
            )
            subprocess.run(
                [_CLICLICK_PATH, "kd:cmd", "t:w", "ku:cmd"],
                capture_output=True, timeout=5
            )
        else:
            # 降级：osascript keystroke（仅交互式终端可用，launchd 下 TCC 拦截）
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to tell process "ima.copilot" '
                 'to keystroke escape'],
                capture_output=True, timeout=5
            )
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to tell process "ima.copilot" '
                 'to keystroke "w" using command down'],
                capture_output=True, timeout=5
            )

    def still_open() -> bool:
        time.sleep(WAIT_AFTER_CLOSE)
        if article_url:
            return extract_url_ax() == article_url   # 已知 URL：精确比对
        return extract_url_ax() is not None           # 未知 URL：仍有任意文章标签 → 未关

    # 第 1 次：激活 IMA + cliclick（cliclick 是全局按键，须 IMA 在前台）
    activate_ima()
    send_cmd_w()
    if not still_open():
        return True

    # 重试：激活 IMA 确保焦点落在 IMA，再发 Cmd+W
    for attempt in range(1, max_retries + 1):
        print(f"    ⚠️  标签页未关闭，激活 IMA 重试 ({attempt}/{max_retries})...")
        activate_ima()
        send_cmd_w()
        if not still_open():
            print(f"    ✓ 标签页已关闭（重试 {attempt}）")
            return True

    print(f"    ❌ 标签页关闭失败，请手动检查 IMA")
    return False


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
        m = re.search(r'\[(\d+)\] AXStaticText = "(.+)"', line)
        if m:
            elem_idx = int(m.group(1))
            text = m.group(2)
            indent = len(line) - len(line.lstrip())
            static_texts.append((i, elem_idx, text, indent))

    # 检测知识库类型：查找 "公众号" 标记的 indent
    # 容错：收集所有 "公众号" 的缩进，选择最常见的
    indent_counts = {}
    for _, elem_idx, text, indent in static_texts:
        if text == "公众号":
            indent_counts[indent] = indent_counts.get(indent, 0) + 1

    if not indent_counts:
        # 未找到 "公众号" 标记，返回空
        return []

    # 选择最常见的缩进层级
    target_indent = max(indent_counts.items(), key=lambda x: x[1])[0]

    # 遍历查找 "公众号" 标记，回溯找标题
    for j, (line_idx, elem_idx, text, indent) in enumerate(static_texts):
        if text != "公众号":
            continue

        # 容错：允许缩进有少量差异（±2 空格）
        if abs(indent - target_indent) > 2:
            continue

        # 回溯找最近的 indent=target_indent 的 AXStaticText（文章标题）
        title_elem = None
        for k in range(j - 1, max(-1, j - 6), -1):
            _, e_idx, t, ind = static_texts[k]
            if abs(ind - target_indent) <= 2 and t != "公众号" and len(t) > 10:
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

def _kb_visible_in_any_window(kb_name: str) -> Optional[bool]:
    """CG 标题层面判断目标 KB 是否仍显示在任一 ima 大窗口（走库漂移守卫用）。

    用 list_windows 的 CG 标题（launchd 下可读），不依赖 System Events。
    返回 True/False；无法判定（标题全空/读取失败，如 Electron 冷启动）返回
    None——调用方对 None 放行，仅对明确的 False 中止，避免误中止。"""
    try:
        wins = json.loads(run_cua(["list_windows"]))["windows"]
        ima_names = {"ima", "ima.copilot"}
        titles = [
            w.get("title", "") for w in wins
            if w.get("app_name", "").lower() in ima_names
            and w.get("bounds", {}).get("height", 0) > 200
        ]
        if not any(titles):
            return None
        return any(kb_name in t for t in titles if t)
    except Exception:
        return None


def _verify_kb_or_exit(kb_name: str) -> None:
    """运行前 KB 预检：与走库漂移守卫同数据源（list_windows CG 标题，launchd 可读），
    两层防线真正叠加，而不是 System Events 标题的平行实现。

    True=在目标库；False=窗口标题可读但均不含目标库（停在别的知识库），立即中止——
    绝不能继续：整轮提取会把别的库的文章张冠李戴入库
    （2026-09-17 实证：15 篇皮皮鲁文章被挂到英语教与学名下）；
    None=标题全空/读取失败（Electron 冷启动），放行但显式声明，走库守卫兜底。"""
    check = _kb_visible_in_any_window(kb_name)
    if check is True:
        print(f"✅ 确认在 {kb_name} 知识库列表页")
        return
    if check is False:
        print(f"❌ IMA 窗口标题均不含目标知识库 '{kb_name}'，中止（防跨库污染）")
        print(f"   请先在 IMA 中打开 {kb_name} 知识库列表页再运行")
        sys.exit(2)
    print(f"⚠️  无法从窗口标题确认在 '{kb_name}' 知识库，继续尝试提取...")


def _load_db_titles_norm() -> set:
    """全部文章标题的归一化集合（页级标题预检用）。

    跨库包含：URL 唯一约束下，任一库已提取即视为已知（跨库重复是独立已知限制）。"""
    with closing(sqlite3.connect(DB_FILE)) as conn:
        return {
            normalize_title_for_compare(t)
            for (t,) in conn.execute("SELECT title FROM articles WHERE title IS NOT NULL")
        }


async def extract_articles(pid: int, window_id: int, kb_name: str = "AI"):
    print("\n" + "=" * 60)
    print(f"开始批量提取（{kb_name} 知识库）")
    print("=" * 60)

    total_new = 0
    total_skipped = 0
    total_failed = 0
    consecutive_seen = 0
    # 上一篇通过幻影校验的文章 URL：launchd 下标题校验不可用时的幻影判定基线
    prev_verified_url: Optional[str] = None
    processed_titles: Set[str] = set()

    # 走库前清掉残留文章标签页（上一轮中断/失败的遗留会让首卡读到旧 URL）
    leftover = close_all_article_tabs()
    if leftover:
        print(f"  ⚠️  已关闭 {leftover} 个残留文章标签页")

    db_titles = _load_db_titles_norm()
    # 连续「整页标题均在库」的页数：达到 2 判定列表走完（见页级标题预检注释）
    confirmed_known_pages = 0

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

        # AX 脱落检测：仅菜单栏形态下继续走库只会死点击 + 30s/次读取超时
        if ax_tree_menu_bar_only(state.get("tree_markdown", "")):
            print("  ⚠️  AX 树呈仅菜单栏形态（AX 已脱落），激活 IMA 后重读...")
            activate_ima()
            state = get_window_state(pid, window_id)
            if not state or ax_tree_menu_bar_only(state.get("tree_markdown", "")):
                print("  ❌ IMA AX 树已脱落且激活无效，中止本库提取"
                      "（已提取文章照常进入保存阶段；根治见 ima NSAppSleepDisabled）")
                break

        # 解析文章列表
        articles = parse_articles_from_tree(state, kb_name)
        print(f"  识别到 {len(articles)} 篇文章")

        if not articles:
            print("  ⚠️  未找到文章，可能已到列表底部")
            break

        # KB 漂移守卫：窗口已离开目标知识库（如误点侧边栏/重启后停在别的库）
        # 时立即中止——防止把别的库的文章张冠李戴入库（2026-09-17 实证 15 篇）
        if _kb_visible_in_any_window(kb_name) is False:
            print(f"  ❌ IMA 窗口已离开 '{kb_name}' 知识库，中止本库提取（防跨库污染）")
            break

        # 页级标题预检：本页标题与 DB 比对，全部命中则不点击任何卡片。
        # 背景（2026-09-17 英语教与学 实证）：列表顶部可能是 2 篇已知文章、其下才是
        # 7+ 篇从未提取的——「连续 2 篇已存在即停」的旧启发式会把它们全部漏掉。
        # 标题匹配只用于"是否存在未知候选"的页级决策，skip/新增仍以 URL 为准。
        # 注意：预检只能看到可视区卡片——全已知页不立即停止，而是滚动一页确认
        # 折叠线以下；连续两页全已知才判定列表走完（scroll 需真实推进）。
        unknown_titles = [
            a["title"] for a in articles
            if normalize_title_for_compare(a["title"]) not in db_titles
        ]
        if not unknown_titles:
            confirmed_known_pages += 1
            if confirmed_known_pages >= 2:
                print("  连续两页所有标题均已在库，判定列表已走完，停止翻页")
                break
            print("  本页所有标题均已在库，滚动一页确认折叠线以下……")
        else:
            confirmed_known_pages = 0
        # 连续早停阈值放宽：候选未消化完之前不轻易停（详见 MAX_CONSECUTIVE_SEEN 注释）
        consecutive_stop_at = len(unknown_titles) + MAX_CONSECUTIVE_SEEN

        page_new = 0
        page_skipped = 0
        page_failed = 0

        should_stop = False

        # 全已知页不点击（无候选可提取），但仍要走滚动路径确认折叠线以下
        walk_target = articles if unknown_titles else []

        for i, article in enumerate(walk_target, 1):
            elem_idx = article["element_index"]
            title = article["title"]

            # 去重：本次运行内已处理过的标题直接跳过。
            # 标题重复只代表列表分页重叠，不参与数据库命中计数。
            # 注意：仅在 URL 成功提取后才标记已处理（见下方 add），
            # 点击/URL 失败的标题允许在后续页面重试。
            if title in processed_titles:
                continue

            print(f"\n  [{i}] {title[:60]}... (element {elem_idx})")

            # 不要在此调用 get_window_state！
            # element_index 缓存会在下次 get_window_state 时被替换，
            # 必须用本次页面解析时的缓存来点击。
            # 前台点击：launchd 后台 cua-driver AXPress 点击文章常不生效（IMA 非前台时不打开），
            # 致 extract_url_ax 读不到 AXDocument。激活 IMA 到前台再点击（定时任务无人值守，可接受干扰）。
            activate_ima()

            # 点击文章（使用当前缓存中的 element_index）
            print(f"    点击文章 (element {elem_idx})...")
            if not click_element(pid, window_id, elem_idx):
                print("    ❌ 点击失败，刷新状态重试...")
                page_failed += 1
                # 点击失败时才重新获取状态并重新解析
                state = get_window_state(pid, window_id)
                if state and state.get("element_count", 0) > 100:
                    # 缓存已刷新，无法继续用旧索引，跳出本页
                    print("    ⚠️  索引已失效，跳到下一页")
                break

            # 等待加载
            await asyncio.sleep(WAIT_CLICK_LOAD)

            article_url: Optional[str] = None
            retry_click_failed = False
            try:
                # 幻影防护：确认打开的是本篇才信任 URL（is_same_article_page docstring）。
                # 幻影/未读到 URL 都重试点击；幻影 URL 绝不进入 url_exists。
                url = None
                verified_page_title: Optional[str] = None
                for verify_attempt in range(1, CLICK_VERIFY_ATTEMPTS + 1):
                    if verify_attempt > 1:
                        # 脱落态下点击必落空、状态读取必耗满超时——先廉价探测，脱落则放弃重试
                        if ax_tree_menu_bar_only(_read_main_window_md()):
                            print("    ❌ IMA AX 树已脱落（仅菜单栏），放弃重试")
                            break
                        # 残留标签页投毒防护：关掉所有文章标签页再重试点击，
                        # 保证重读到的必然是本次点击打开的那篇
                        closed = close_all_article_tabs()
                        if closed:
                            print(f"    ⚠️  已清理 {closed} 个残留标签页后重试")
                        print(f"    ⚠️  打开的不是本篇（幻影）或未读到 URL，重试点击 ({verify_attempt}/{CLICK_VERIFY_ATTEMPTS})...")
                        if not click_element(pid, window_id, elem_idx):
                            print("    ❌ 重试点击失败，刷新状态重试...")
                            retry_click_failed = True
                            break
                        await asyncio.sleep(WAIT_CLICK_LOAD)
                    url = extract_url_ax(pid, window_id)
                    if not url:
                        continue  # 没读到 URL：可能点击未生效，重试
                    page_title = extract_title_ax()
                    if is_same_article_page(title, page_title, url, prev_verified_url):
                        verified_page_title = page_title
                        break
                    url = None  # 幻影：读到的是其他文章的 URL，丢弃不计

                if not url:
                    print("    ⚠️  未提取到本篇 URL")
                    total_failed += 1
                    page_failed += 1
                    consecutive_seen = 0  # 失败时重置计数器
                    if retry_click_failed:
                        # 与首次点击失败同语义：索引缓存已可疑，跳出本页由下页重新解析
                        break  # finally 负责关闭标签页
                    continue  # finally 负责关闭标签页

                print(f"    ✅ URL: {url[:80]}...")
                article_url = url
                prev_verified_url = url
                # URL 成功提取后才标记已处理——点击/URL 失败的标题
                # 不在此标记，允许分页重叠时在后续页面重试。
                processed_titles.add(title)

                if url_exists(url):
                    print("    ℹ️  已存在，跳过")
                    total_skipped += 1
                    page_skipped += 1
                    consecutive_seen += 1

                    if consecutive_seen >= consecutive_stop_at:
                        print(f"\n  ⚠️  连续 {consecutive_seen} 篇已存在（本页 {len(unknown_titles)} 个"
                              f"疑似未知标题均为误报），可能已全部提取")
                        should_stop = True
                        break  # finally 负责关闭标签页
                else:
                    # 只有确认 URL 不在数据库中时，才打断连续命中计数。
                    consecutive_seen = 0
                    # 页面标题已在幻影校验时读取并通过匹配，直接复用（省一次 osascript；
                    # launchd 下为 None，落回列表标题，与旧 extract_title_ax 失败路径一致）
                    final_title = verified_page_title or title
                    print(f"    ✅ 标题: {final_title[:60]}...")

                    if save_article(url, final_title, kb_name):
                        total_new += 1
                        page_new += 1
                        print(f"    ✅ 新文章已保存 (总计: {total_new})")
                    else:
                        # save_article 返回 False：白名单拒绝（非微信）或 DB 异常。
                        # 两者靠函数内部打印区分（"跳过非微信" vs "保存失败"）。
                        # review PR#12 #1：total_skipped 累加，汇总不矛盾
                        total_skipped += 1
                        page_skipped += 1
                        print(f"    ⏭️  未保存（本页跳过: {page_skipped}）")
            finally:
                # 无论成功/异常/continue/break，都关闭文章标签页（异常路径不再漏关）
                cmd_w_close(article_url=article_url)
                await asyncio.sleep(WAIT_AFTER_CLOSE)

        if should_stop:
            break

        if unknown_titles:
            print(f"\n  本页完成: 新增 {page_new}, 跳过 {page_skipped}")

            # 本页没有新增、跳过或失败，说明列表没有继续推进（通常是滚动卡住，
            # 或页面内容已完全由本次运行处理过）。有失败时继续尝试，避免 AX 临时
            # 故障导致整页 URL 提取失败后被误判为列表已卡住。
            if page_new == 0 and page_skipped == 0 and page_failed == 0:
                print("  ⚠️  本页无进展，停止继续滚动")
                break

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

    # URL 规范化自检：normalize_url 改格式后，若旧 DB 未迁移，
    # url_exists 会因新/旧 URL 不一致而判定新文章 → 大量重复入库。
    # 此处守卫检测未规范 URL 并终止提取，提示先跑 migrate_normalize_urls.py。
    from ima_common import verify_urls_canonical
    non_canonical = verify_urls_canonical()
    if non_canonical:
        print(f"❌ 检测到 {len(non_canonical)} 行 URL 未规范（normalize_url 口径变更后需迁移）")
        print(f"   示例: id={non_canonical[0][0]}")
        print(f"     当前: {non_canonical[0][1][:80]}")
        print(f"     规范: {non_canonical[0][2][:80]}")
        print(f"   请先运行: python3 migrate_normalize_urls.py")
        print(f"   迁移完成后再执行提取，否则会重复入库。")
        sys.exit(1)
    print("✅ URL 规范化自检通过")

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

    # 验证在指定知识库（CG 标题三态判定，与走库漂移守卫同源）
    _verify_kb_or_exit(kb_name)

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
