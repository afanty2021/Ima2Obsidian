"""幻影跳过防护回归测试。

背景：2026-09-17 英语教与学 实证——点击未生效时 extract_url_ax 读到旧文章标签页
的 URL，url_exists 误判"已存在"计入连续命中，两连即触发早停，列表顶部真新文章
被静默跳过。修复：点击后先校验打开的是本篇（标题匹配；launchd 下标题不可用则
退化为 URL 基线），幻影即重试点击，且幻影 URL 绝不进入 url_exists/连续计数。
"""
import asyncio
import sqlite3

import ima_ax_extractor
from ima_ax_extractor import is_same_article_page, normalize_title_for_compare


class TestNormalizeTitleForCompare:
    def test_strips_whitespace_punctuation_quotes(self):
        assert normalize_title_for_compare("「AI」时代，学习!") == "ai时代学习"
        assert normalize_title_for_compare("  耐性为本，天资为辅 ") == "耐性为本天资为辅"

    def test_none_and_empty(self):
        assert normalize_title_for_compare(None) == ""
        assert normalize_title_for_compare("") == ""


class TestIsSameArticlePage:
    def test_exact_match_after_noise(self):
        assert is_same_article_page(
            "耐性为本，天资为辅---如何培养学生英语学习的耐性",
            "耐性为本，天资为辅——如何培养学生英语学习的耐性",
            "https://mp.weixin.qq.com/s/x", None,
        )

    def test_truncated_list_title_contained_in_page_title(self):
        assert is_same_article_page(
            "如何培养学生的“汇词”能力",
            "如何培养学生的“汇词”能力？一线教师的三条实操路径",
            "https://mp.weixin.qq.com/s/x", None,
        )

    def test_mismatch_is_phantom(self):
        assert not is_same_article_page(
            "新文章甲", "已有文章 A", "https://mp.weixin.qq.com/s/stale", None,
        )

    def test_short_title_below_containment_threshold(self):
        # 短方 <6 字不启用子串容忍，避免误放行
        assert not is_same_article_page(
            "AI", "AI时代的记忆工程", "https://mp.weixin.qq.com/s/x", None,
        )

    def test_no_page_title_same_url_as_prev_is_phantom(self):
        # launchd 基线路径：读到与上一篇相同的 URL 即幻影（2026-09-17 实证场景）
        assert not is_same_article_page(
            "新文章甲", None,
            "https://mp.weixin.qq.com/s/prev", "https://mp.weixin.qq.com/s/prev",
        )

    def test_no_page_title_different_url_passes(self):
        assert is_same_article_page(
            "新文章甲", None,
            "https://mp.weixin.qq.com/s/new", "https://mp.weixin.qq.com/s/prev",
        )

    def test_no_page_title_no_url_fails(self):
        assert not is_same_article_page("新文章甲", None, None, None)


def test_phantom_retry_saves_real_article_and_skips_counter(temp_db, monkeypatch):
    """幻影读取须重试点击、救回真新文章，且不计入连续已存在（否则两连误早停）。"""
    from ima_common import init_database

    init_database()
    existing = [
        ("https://mp.weixin.qq.com/s/existing-a", "已有文章 A"),
        ("https://mp.weixin.qq.com/s/existing-b", "已有文章 B"),
    ]
    with sqlite3.connect(temp_db) as conn:
        conn.executemany(
            "INSERT INTO articles (url, title, knowledge_base, status) VALUES (?, ?, ?, 'success')",
            [(url, title, "英语教与学") for url, title in existing],
        )

    pages = [
        [
            {"element_index": 1, "title": "新文章甲"},
            {"element_index": 2, "title": "已有文章 A"},
        ],
        [
            {"element_index": 1, "title": "已有文章 B"},
        ],
        [
            {"element_index": 1, "title": "已有文章 B"},
        ],
    ]
    # 卡1 第1次读到幻影 URL（=existing-a 的旧标签页），重试后读到真 URL；
    # 卡2、页2 正常
    urls = iter([
        "https://mp.weixin.qq.com/s/existing-a",   # 卡1 幻影
        "https://mp.weixin.qq.com/s/new-a",        # 卡1 重试后真 URL
        "https://mp.weixin.qq.com/s/existing-a",   # 卡2 已存在
        "https://mp.weixin.qq.com/s/existing-b",   # 页2 已存在（两连 → 正常早停）
    ])
    # 幻影时文章页标题是旧文章的，与列表标题不符；重试后是本篇
    # （每次校验恰好消费 1 个标题；保存路径复用校验时已读的标题，不再单独调用）
    page_titles = iter(["已有文章 A", "新文章甲", "已有文章 A", "已有文章 B"])
    clicked = []
    parsed_pages = []
    close_all_calls = []

    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 5)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_CLICK_LOAD", 0)
    monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window", lambda _kb: True)

    def fake_close_all():
        close_all_calls.append(True)
        return 0

    monkeypatch.setattr(ima_ax_extractor, "close_all_article_tabs", fake_close_all)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_AFTER_CLOSE", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_SCROLL", 0)
    monkeypatch.setattr(
        ima_ax_extractor, "get_window_state",
        lambda _pid, _wid: {"element_count": 100},
    )

    def parse_page(_state, _kb_name):
        parsed_pages.append(True)
        return pages.pop(0)

    monkeypatch.setattr(ima_ax_extractor, "parse_articles_from_tree", parse_page)
    monkeypatch.setattr(ima_ax_extractor, "activate_ima", lambda: None)

    def click(_pid, _wid, element_index):
        clicked.append(element_index)
        return True

    monkeypatch.setattr(ima_ax_extractor, "click_element", click)
    monkeypatch.setattr(ima_ax_extractor, "extract_url_ax", lambda *_a: next(urls))
    monkeypatch.setattr(ima_ax_extractor, "extract_title_ax", lambda: next(page_titles))
    monkeypatch.setattr(ima_ax_extractor, "cmd_w_close", lambda **_kw: None)
    monkeypatch.setattr(ima_ax_extractor, "scroll_down", lambda *_a: None)
    monkeypatch.setattr(ima_ax_extractor.time, "sleep", lambda _s: None)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "英语教与学"))

    with sqlite3.connect(temp_db) as conn:
        saved_urls = {row[0] for row in conn.execute("SELECT url FROM articles")}

    # 真新文章被救回（幻影 URL 未被误判"已存在"）
    assert "https://mp.weixin.qq.com/s/new-a" in saved_urls
    # 卡1 发生了幻影重试（初始点击 + 1 次重试）；
    # 页2 的卡片标题已在库 → 标题预检直接停止，不再点击
    assert clicked == [1, 1, 2]
    # 幻影未计入连续命中：卡2 跳过后页2 被解析（页2 全已知 → 不点击，
    # 滚动确认页3 亦全已知 → 停止）
    assert len(parsed_pages) == 3
    # 残留标签页清理：走库开始 1 次 + 幻影重试前 1 次
    assert len(close_all_calls) == 2


def test_close_all_article_tabs_closes_until_none(monkeypatch):
    """逐个关闭探测到的文章标签页，探测不到即停；cmd_w_close 调用数与标签页数一致"""
    urls = iter(["https://mp.weixin.qq.com/s/t1", "https://mp.weixin.qq.com/s/t2", None])
    closed_calls = []
    monkeypatch.setattr(ima_ax_extractor, "extract_url_ax", lambda *_a: next(urls))
    monkeypatch.setattr(ima_ax_extractor, "cmd_w_close", lambda **_kw: closed_calls.append(1))

    assert ima_ax_extractor.close_all_article_tabs() == 2
    assert len(closed_calls) == 2


def test_close_all_article_tabs_noop_when_no_tab(monkeypatch):
    """无标签页时立即返回 0，不触碰 cmd_w_close"""
    closed_calls = []
    monkeypatch.setattr(ima_ax_extractor, "extract_url_ax", lambda *_a: None)
    monkeypatch.setattr(ima_ax_extractor, "cmd_w_close", lambda **_kw: closed_calls.append(1))

    assert ima_ax_extractor.close_all_article_tabs() == 0
    assert closed_calls == []


def test_phantom_exhausted_retries_counts_failure_not_skip(temp_db, monkeypatch):
    """重试耗尽仍是幻影 → 计失败（重置连续计数），绝不按"已存在"跳过。"""
    from ima_common import init_database

    init_database()

    pages = [
        [{"element_index": 1, "title": "新文章甲"}],
        [{"element_index": 2, "title": "后续新文章"}],
    ]
    # 卡1 三次尝试都读到同一幻影 URL；页2 正常
    urls = iter([
        "https://mp.weixin.qq.com/s/stale",
        "https://mp.weixin.qq.com/s/stale",
        "https://mp.weixin.qq.com/s/stale",
        "https://mp.weixin.qq.com/s/next-page-new",
    ])
    page_titles = iter(["旧文章", "旧文章", "旧文章", "后续新文章"])
    clicked = []

    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 2)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_CLICK_LOAD", 0)
    monkeypatch.setattr(ima_ax_extractor, "close_all_article_tabs", lambda: 0)
    monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window", lambda _kb: True)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_AFTER_CLOSE", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_SCROLL", 0)
    monkeypatch.setattr(
        ima_ax_extractor, "get_window_state",
        lambda _pid, _wid: {"element_count": 100},
    )
    monkeypatch.setattr(
        ima_ax_extractor, "parse_articles_from_tree",
        lambda _state, _kb: pages.pop(0),
    )
    monkeypatch.setattr(ima_ax_extractor, "activate_ima", lambda: None)

    def click(_pid, _wid, element_index):
        clicked.append(element_index)
        return True

    monkeypatch.setattr(ima_ax_extractor, "click_element", click)
    monkeypatch.setattr(ima_ax_extractor, "extract_url_ax", lambda *_a: next(urls))
    monkeypatch.setattr(ima_ax_extractor, "extract_title_ax", lambda: next(page_titles))
    monkeypatch.setattr(ima_ax_extractor, "cmd_w_close", lambda **_kw: None)
    monkeypatch.setattr(ima_ax_extractor, "scroll_down", lambda *_a: None)
    monkeypatch.setattr(ima_ax_extractor.time, "sleep", lambda _s: None)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "AI"))

    with sqlite3.connect(temp_db) as conn:
        saved_urls = {row[0] for row in conn.execute("SELECT url FROM articles")}

    # 幻影卡计失败（未保存其幻影 URL），后续页面正常继续
    assert "https://mp.weixin.qq.com/s/stale" not in saved_urls
    assert "https://mp.weixin.qq.com/s/next-page-new" in saved_urls
    # 卡1 三次尝试均点击（耗尽重试），页2 单次点击
    assert clicked == [1, 1, 1, 2]
