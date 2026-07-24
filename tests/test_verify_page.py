"""验证页检测 + 自动确认：微信「当前环境异常」风控验证页的识别与点确认。

背景：微信文章页对 saver 自动访问间歇触发风控验证页，saver 卡在验证页上 quick_clip
无效（2026-07-23 皮皮鲁库 11 篇因此 0 落盘）。Chrome execute JS 已开启，可在 quick_clip
前检测验证页 + 自动点「确认」。详见 Plans/snoopy-pondering-biscuit.md。
"""
from unittest.mock import patch

import ima_obsidian_saver as saver


class TestIsVerifyPage:
    def test_hit_current_env_keyword(self):
        """正文含「当前环境异常」→ 命中"""
        assert saver.is_verify_page({"text": "当前环境异常，要验证后才能正常访问"}) is True

    def test_hit_in_title(self):
        """标题含「完成验证」→ 命中（关键词同时扫 title）"""
        assert saver.is_verify_page({"title": "请完成验证", "text": ""}) is True

    def test_miss_normal_article(self):
        """正常文章页不命中"""
        assert saver.is_verify_page({"title": "别只循环听英文歌", "text": "正文内容"}) is False

    def test_none_snapshot(self):
        assert saver.is_verify_page(None) is False

    def test_empty_snapshot(self):
        assert saver.is_verify_page({}) is False


class TestReadPageSnapshot:
    def test_parse_json(self):
        with patch("ima_obsidian_saver.execute_chrome_js",
                   return_value='{"title":"T","text":"正文"}'):
            snap = saver.read_page_snapshot()
        assert snap == {"title": "T", "text": "正文"}

    def test_none_when_js_fails(self):
        with patch("ima_obsidian_saver.execute_chrome_js", return_value=None):
            assert saver.read_page_snapshot() is None

    def test_none_on_bad_json(self):
        with patch("ima_obsidian_saver.execute_chrome_js", return_value="not json"):
            assert saver.read_page_snapshot() is None


class TestClickConfirm:
    def test_returns_true_when_clicked(self):
        """execute_chrome_js 返回 '1' → 点到了"""
        with patch("ima_obsidian_saver.execute_chrome_js", return_value="1") as m:
            assert saver.click_confirm() is True
        js_sent = m.call_args[0][0]
        assert "querySelectorAll" in js_sent   # 遍历可点击元素
        assert "click" in js_sent              # 真正的点击动作

    def test_returns_false_when_no_button(self):
        with patch("ima_obsidian_saver.execute_chrome_js", return_value="0"):
            assert saver.click_confirm() is False

    def test_js_includes_verify_button_keyword(self):
        """验证页按钮文本是「去验证」，click_confirm 的 JS 须含该关键词（实测 2922 暴露）"""
        with patch("ima_obsidian_saver.execute_chrome_js", return_value="1") as m:
            saver.click_confirm()
        assert "去验证" in m.call_args[0][0]


class TestHandleVerifyPage:
    def test_no_verify_page_skips_click(self):
        """非验证页：不调 click_confirm，返回 False（无需处理）"""
        with patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "文章", "text": "正文"}), \
             patch("ima_obsidian_saver.click_confirm") as mock_click:
            assert saver.handle_verify_page("Google Chrome") is False
        mock_click.assert_not_called()

    def test_verify_page_clicks_then_leaves(self):
        """验证页：点确认成功，二次读已离开 → 返回 True，click 调 1 次"""
        snaps = [
            {"title": "验证", "text": "当前环境异常"},
            {"title": "文章", "text": "正文"},  # 点确认后已离开验证页
        ]
        with patch("ima_obsidian_saver.read_page_snapshot", side_effect=snaps), \
             patch("ima_obsidian_saver.click_confirm", return_value=True) as mock_click, \
             patch("ima_obsidian_saver.time.sleep"):
            assert saver.handle_verify_page("Google Chrome") is True
        assert mock_click.call_count == 1

    def test_verify_page_no_confirm_button_gives_up(self):
        """验证页但找不到确认按钮 → 返回 True（遇到过），点 1 次失败即放弃不重试"""
        with patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "验证", "text": "当前环境异常"}), \
             patch("ima_obsidian_saver.click_confirm", return_value=False) as mock_click, \
             patch("ima_obsidian_saver.time.sleep"):
            assert saver.handle_verify_page("Google Chrome") is True
        assert mock_click.call_count == 1


class TestExtractPublishDateJs:
    """execute JS 读微信文章页 #publish_time（如 '2026年7月15日 09:56'）→ YYMMDD。

    requests 抓到的是微信精简页（无 create_time 字段，extract_publish_date 必失败）；
    浏览器渲染后 #publish_time 元素才有发布日期，故改用 execute JS。
    """

    def test_parses_weixin_publish_time(self):
        with patch("ima_obsidian_saver.execute_chrome_js", return_value="2026年7月15日 09:56"):
            assert saver.extract_publish_date_js() == "260715"

    def test_single_digit_month_day(self):
        with patch("ima_obsidian_saver.execute_chrome_js", return_value="2026年7月5日 10:00"):
            assert saver.extract_publish_date_js() == "260705"

    def test_none_when_empty(self):
        with patch("ima_obsidian_saver.execute_chrome_js", return_value=None):
            assert saver.extract_publish_date_js() is None

    def test_none_when_not_a_date(self):
        """验证页/未加载页读到的非日期文本 → None（让上游降级）"""
        with patch("ima_obsidian_saver.execute_chrome_js", return_value="微信公众平台"):
            assert saver.extract_publish_date_js() is None
