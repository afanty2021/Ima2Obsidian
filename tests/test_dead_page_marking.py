"""微信拦截页（违规/发布者删除/账号屏蔽）提取侧标记 + 单轮失败记忆回归测试。

背景（2026-09-21 两时段定时任务核查）：单篇违规文在 Invest 列表分页重叠下每页
迭代被重试，共磨 17 次 × ~1.5 分钟 ≈ 50 分钟，且违规提示每天重复刷屏。修复：
1) 幻影重试耗尽后探测文章页 AX 文本——命中共享词表（ima_common.DELETED_REASON_MAP，
   与保存器 _deleted_reason 同源同阈值）即写入 dead_articles 永久跳过；
2) 单轮失败记忆 MAX_CARD_ATTEMPTS_PER_RUN=2——非拦截页失败也给一次跨页翻案机会。
验证码/风控页（title=微信公众平台但无拦截文案）不得误标。
"""
import asyncio
import json
import sqlite3

import ima_ax_extractor
import ima_common
from ima_ax_extractor import normalize_title_for_compare
from ima_common import init_database

# 健康/拦截页文章窗口的 AX markdown 形态（探测桩返回值）
VIOLATION_MD = (
    "- [0] AXWindow \"微信公众平台\"\n"
    "- [2] AXStaticText = \"此内容因违规无法查看\"\n"
    "- [3] AXStaticText = \"接相关投诉，此内容违反规定，查看详细内容\"\n"
)
CAPTCHA_MD = (
    "- [0] AXWindow \"微信公众平台\"\n"
    "- [1] AXTextField 地址和搜索栏\n"
)
T_JIA = "新文章甲的标题足够长以通过幻影校验"
T_BING = "新文章丙的标题长度也足以通过幻影校验"


def _patch_flow(monkeypatch, *, pages, urls, titles=None, probe_md="", max_pages=6):
    """提取流程公共 mock；probe_md 覆盖拦截页探测返回值。"""
    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", max_pages)
    monkeypatch.setattr(ima_ax_extractor, "MAX_CARD_ATTEMPTS_PER_RUN", 2)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_CLICK_LOAD", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_AFTER_CLOSE", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_SCROLL", 0)
    monkeypatch.setattr(ima_ax_extractor, "close_all_article_tabs", lambda: 0)
    monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window", lambda _kb: True)
    monkeypatch.setattr(ima_ax_extractor, "_probe_article_window_md", lambda: probe_md)
    # 幻影重试路径的脱落探针：健康形态（非脱落），不消费 get_window_state 序列
    monkeypatch.setattr(ima_ax_extractor, "_read_main_window_md",
                        lambda: "- [0] AXWindow\n- [1] AXStaticText = \"列表文本\"")
    monkeypatch.setattr(ima_ax_extractor, "get_ima_main_window",
                        lambda: {"pid": 9, "window_id": 9,
                                 "bounds": {"width": 1512, "height": 949}})
    monkeypatch.setattr(ima_ax_extractor, "activate_ima", lambda: None)
    monkeypatch.setattr(ima_ax_extractor, "cmd_w_close", lambda **_kw: None)
    monkeypatch.setattr(ima_ax_extractor, "scroll_down", lambda *_a: None)
    monkeypatch.setattr(ima_ax_extractor.time, "sleep", lambda _s: None)
    states = [dict({"element_count": 500, "tree_markdown": "- [0] AXWindow"})] * (len(pages) + 2)
    it = iter(states)

    monkeypatch.setattr(ima_ax_extractor, "get_window_state", lambda _p, _w: next(it))
    monkeypatch.setattr(ima_ax_extractor, "parse_articles_from_tree",
                        lambda _state, _kb: pages.pop(0))
    monkeypatch.setattr(ima_ax_extractor, "click_element", lambda _p, _w, _e: True)
    monkeypatch.setattr(ima_ax_extractor, "extract_url_ax", lambda *_a: next(urls))
    if titles is not None:
        monkeypatch.setattr(ima_ax_extractor, "extract_title_ax", lambda: next(titles))


def _saved_and_dead(temp_db):
    with sqlite3.connect(temp_db) as conn:
        saved = {r[0] for r in conn.execute("SELECT url FROM articles")}
        dead = {r[0]: r[1] for r in conn.execute("SELECT title_norm, reason FROM dead_articles")}
    return saved, dead


def test_violation_page_marked_dead_and_never_reclicked(temp_db, monkeypatch):
    """点击打开违规拦截页（URL 读不到）→ 探测命中词表 → 写 dead_articles、
    计删除不计失败；下一页迭代不再点击。"""
    init_database()
    page1 = [{"element_index": 1, "title": T_JIA},
             {"element_index": 2, "title": T_BING}]
    pages = [page1, list(page1)]  # 分页重叠：第 2 页重复出现甲丙
    # 甲 1 个 URL；丙 幻影重试耗尽需 3 次 None（CLICK_VERIFY_ATTEMPTS=3）
    urls = iter(["https://mp.weixin.qq.com/s/jia", None, None, None])
    clicked = []
    _patch_flow(monkeypatch, pages=pages, urls=urls, probe_md=VIOLATION_MD)
    monkeypatch.setattr(ima_ax_extractor, "click_element",
                        lambda _p, _w, e: clicked.append(e) or True)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "AI"))

    saved, dead = _saved_and_dead(temp_db)
    assert saved == {"https://mp.weixin.qq.com/s/jia"}
    assert normalize_title_for_compare(T_BING) in dead
    assert dead[normalize_title_for_compare(T_BING)] == "违规不可查看"
    # 甲 1 次；丙 第 1 页走查内 3 次幻影点击（CLICK_VERIFY_ATTEMPTS 既有语义）后
    # 探测标记 dead，第 2 页迭代零点击——dead 标记省的是跨页迭代重试
    assert clicked == [1, 2, 2, 2]


def test_captcha_page_not_marked_dead_counts_failed(temp_db, monkeypatch):
    """验证码/风控页（title=微信公众平台但无拦截文案）不得误标——走普通失败路径
    计失败，且同轮失败记忆生效（第 2 次失败后不再点击）。"""
    init_database()
    page = [{"element_index": 3, "title": T_BING}]
    pages = [list(page)] * 4  # 同卡重复出现 4 个页迭代
    # 每次走查 3 次幻影尝试全 None；生成器供无限 None（验证 cap 截停而非迭代器耗尽）
    urls = (None for _ in iter(int, 1))
    clicked = []
    _patch_flow(monkeypatch, pages=pages, urls=urls, probe_md=CAPTCHA_MD)
    monkeypatch.setattr(ima_ax_extractor, "click_element",
                        lambda _p, _w, e: clicked.append(e) or True)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "AI"))

    saved, dead = _saved_and_dead(temp_db)
    assert saved == set()
    assert dead == {}  # 无拦截文案不标记
    # cap=2：前两个页迭代各走查一次（每次 3 次幻影点击），第 3/4 页迭代跳过
    assert clicked == [3, 3, 3, 3, 3, 3]


def test_dead_title_from_db_skipped_at_walk(temp_db, monkeypatch):
    """库内已有 dead 标记的标题：页级预检不算未知候选、逐卡不点击。"""
    init_database()
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO dead_articles (title_norm, title, reason) VALUES (?, ?, ?)",
            (normalize_title_for_compare(T_BING), T_BING, "违规不可查看"),
        )
    page1 = [{"element_index": 1, "title": T_JIA},
             {"element_index": 2, "title": T_BING}]
    pages = [page1]
    urls = iter(["https://mp.weixin.qq.com/s/jia"])
    clicked = []
    _patch_flow(monkeypatch, pages=pages, urls=urls, max_pages=1)
    monkeypatch.setattr(ima_ax_extractor, "click_element",
                        lambda _p, _w, e: clicked.append(e) or True)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "AI"))

    saved, dead = _saved_and_dead(temp_db)
    assert saved == {"https://mp.weixin.qq.com/s/jia"}
    assert clicked == [1]  # 只有甲被点；丙 dead 跳过


def test_shared_word_table_single_source():
    """保存器与提取器共用同一词表/阈值（saver 为别名引用，防止两表漂移）。"""
    import ima_obsidian_saver as sv
    assert sv._DELETED_REASON_MAP is ima_common.DELETED_REASON_MAP
    assert sv._DELETED_REASON_LEN_THRESHOLD == ima_common.DELETED_REASON_LEN_THRESHOLD
    # 阈值防误杀语义：长正文含拦截文案不误判
    assert ima_common.detect_deleted_reason("正文" * 200 + "此内容因违规无法查看") is None
    assert ima_common.detect_deleted_reason("此内容因违规无法查看") == "违规不可查看"
    assert ima_common.detect_deleted_reason("") is None
