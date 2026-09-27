"""验证页检测 + 自动确认：微信「当前环境异常」风控验证页的识别与点确认。

背景：微信文章页对 saver 自动访问间歇触发风控验证页，saver 卡在验证页上 quick_clip
无效（2026-07-23 皮皮鲁库 11 篇因此 0 落盘）。Chrome execute JS 已开启，可在 quick_clip
前检测验证页 + 自动点「确认」。详见 Plans/snoopy-pondering-biscuit.md。
"""
from unittest.mock import patch

import pytest

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

    def test_hit_verify_title_only(self):
        """验证页 text 没渲染只剩 title='微信公众平台' → 也命中（修实测 33 篇漏检）"""
        assert saver.is_verify_page({"title": "微信公众平台", "text": ""}) is True

    def test_verify_title_with_long_text_not_misjudged(self):
        """慢加载文章 title 暂时'微信公众平台'但 text 已有长正文 → 不误判（修 code-review #3）"""
        long_text = "这是文章正文内容，已经加载出来了。" * 5  # >50 字
        assert saver.is_verify_page({"title": "微信公众平台", "text": long_text}) is False

    def test_verify_page_excludes_blocked_account_page(self):
        """屏蔽页（body 含「此账号已被屏蔽」）→ 不是验证页（前置 _deleted_reason 排除）

        防止 handle_verify_page 对屏蔽页浪费 ~12-14s 重试（click_confirm 误点通用按钮
        + 两轮 attempt sleep）。spec §3.3 决策 4。
        """
        snap = {"title": "微信公众平台", "text": "此账号已被屏蔽，内容无法查看"}
        assert saver.is_verify_page(snap) is False

    def test_verify_page_excludes_violation_unavailable_page(self):
        """违规页（新文案）→ 不是验证页"""
        snap = {"title": "微信公众平台", "text": "此内容因违规无法查看"}
        assert saver.is_verify_page(snap) is False

    def test_verify_page_excludes_publisher_deleted_page(self):
        """发布者删除页 → 不是验证页（回归保护）"""
        snap = {"title": "微信", "text": "该内容已被发布者删除"}
        assert saver.is_verify_page(snap) is False

    def test_verify_page_keeps_real_verify_page(self):
        """真验证页（body 不含 DELETED 关键词）→ 仍命中（验证后转删除链路保留）"""
        snap = {"title": "微信公众平台", "text": "当前环境异常，完成验证"}
        assert saver.is_verify_page(snap) is True


class TestReadPageSnapshot:
    def test_parse_json(self):
        with patch("ima_obsidian_saver.execute_chrome_js",
                   return_value='{"title":"T","text":"正文","publish_time":"2026年7月15日"}'):
            snap = saver.read_page_snapshot()
        assert snap == {"title": "T", "text": "正文", "publish_time": "2026年7月15日"}

    def test_old_format_snapshot_gets_empty_publish_time(self):
        """旧格式（无 publish_time 字段，如外部 mock 注入）→ 补空串键，下游 .get 不炸"""
        with patch("ima_obsidian_saver.execute_chrome_js",
                   return_value='{"title":"T","text":"正文"}'):
            snap = saver.read_page_snapshot()
        assert snap["publish_time"] == ""

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

    def test_js_prioritizes_js_verify_id(self):
        """click_confirm 应优先 getElementById('js_verify')（实测 selector 遍历在 saver 自动跑时漏点）"""
        with patch("ima_obsidian_saver.execute_chrome_js", return_value="1") as m:
            saver.click_confirm()
        assert "getElementById('js_verify')" in m.call_args[0][0]


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
        """验证页但找不到确认按钮 → 返回 True（遇到过）；重试耗尽才放弃（a 渲染慢）"""
        with patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "验证", "text": "当前环境异常"}), \
             patch("ima_obsidian_saver.click_confirm", return_value=False) as mock_click, \
             patch("ima_obsidian_saver.time.sleep"):
            assert saver.handle_verify_page("Google Chrome") is True
        assert mock_click.call_count == saver.VERIFY_CLICK_RETRIES  # 重试耗尽才放弃

    def test_retries_click_confirm_until_button_renders(self):
        """「去验证」a 渲染慢，click_confirm 首次落空 → 重试到成功"""
        snaps = [
            {"title": "验证", "text": "当前环境异常"},
            {"title": "文章", "text": "正文"},  # 点确认后离开验证页
        ]
        with patch("ima_obsidian_saver.read_page_snapshot", side_effect=snaps), \
             patch("ima_obsidian_saver.click_confirm", side_effect=[False, True]) as mock_click, \
             patch("ima_obsidian_saver.time.sleep"):
            assert saver.handle_verify_page("Google Chrome") is True
        assert mock_click.call_count == 2  # 首次落空，重试成功


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


# ==================== 验证页剪藏垃圾（2026-09-27 滑块墙事故）====================
#
# 背景：滑块墙当日两轮自动确认后仍停留验证/滑块页，quick_clip 打在滑块页上落盘
# title=微信公众平台 的垃圾 .md——认领按文章标题匹配失配计失败，但垃圾滞留
# Clippings 永不清理（单轮 12 个；最早同类垃圾潜伏 7 周）。两层修复：
# ① save_one_article 复读快照后复检 is_verify_page，仍验证页 → 放弃剪藏（Fix A）；
# ② 认领失败分支清理本轮新落盘的微信平台页剪藏（Fix B）。

JUNK_VERIFY_MD = (
    "---\n"
    'title: "微信公众平台"\n'
    "source: \"https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=X"
    "&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%3F__biz%3DM\"\n"
    "author:\n"
    "published:\n"
    "created: 2026-09-27\n"
    "description:\n"
    "tags:\n"
    "  - \"clippings\"\n"
    "---\n"
    "## 环境异常\n\n"
    "当前环境异常，完成验证后即可继续访问。\n"
)

REAL_ARTICLE_MD = (
    "---\n"
    'title: "正常文章"\n'
    "source: \"https://mp.weixin.qq.com/s?__biz=M\"\n"
    "created: 2026-09-27\n"
    "tags:\n"
    "  - \"clippings\"\n"
    "---\n"
    "这是正文，远长于验证页提示。\n"
)


class TestWechatPlatformTitleHit:
    """_wechat_platform_title_hit：frontmatter title 精确判据（引号变体 + 误判面）。"""

    def test_junk_md_hits(self):
        assert saver._wechat_platform_title_hit(JUNK_VERIFY_MD) is True

    def test_unquoted_title_hits(self):
        txt = "---\ntitle: 微信公众平台\nsource: x\n---\n正文"
        assert saver._wechat_platform_title_hit(txt) is True

    def test_single_quoted_title_hits(self):
        txt = "---\ntitle: '微信公众平台'\nsource: x\n---\n正文"
        assert saver._wechat_platform_title_hit(txt) is True

    def test_real_article_title_misses(self):
        assert saver._wechat_platform_title_hit(REAL_ARTICLE_MD) is False

    def test_no_frontmatter_misses(self):
        assert saver._wechat_platform_title_hit("微信公众平台\n正文") is False

    def test_yaml_code_block_in_body_misses(self):
        """正文里的 frontmatter 示例代码块不误判（只看首个 frontmatter 块）"""
        txt = ("---\ntitle: \"Web Clipper 使用指南\"\nsource: x\n---\n"
               "```yaml\ntitle: 微信公众平台\n```\n正文")
        assert saver._wechat_platform_title_hit(txt) is False

    def test_path_version_oserror_returns_false(self, tmp_path):
        assert saver._is_wechat_platform_clipping(tmp_path / "不存在.md") is False


class TestPurgeNewVerifyClippings:
    """_purge_new_verify_clippings：只清本轮新增的微信公众平台剪藏，不碰存量与真文章。"""

    @pytest.fixture
    def clip_dir(self, tmp_path, monkeypatch):
        vault = tmp_path / "Vault"
        clip = vault / "Clippings"
        clip.mkdir(parents=True)
        monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
        monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip)
        return clip

    def _snapshot(self, clip_dir):
        return {(f, f.stat().st_mtime) for f in clip_dir.rglob("*.md")}

    def test_purges_only_new_junk(self, clip_dir):
        old_junk = clip_dir / "微信公众平台 17.md"
        old_junk.write_text(JUNK_VERIFY_MD, encoding="utf-8")
        real = clip_dir / "正常文章.md"
        real.write_text(REAL_ARTICLE_MD, encoding="utf-8")
        existing = self._snapshot(clip_dir)

        new_junk = clip_dir / "微信公众平台 18.md"
        new_junk.write_text(JUNK_VERIFY_MD, encoding="utf-8")

        purged = saver._purge_new_verify_clippings(existing)

        assert purged == 1
        assert not new_junk.exists()
        assert old_junk.exists()      # 存量垃圾不碰（不可逆操作收窄到本轮新增）
        assert real.exists()

    def test_no_junk_noop(self, clip_dir):
        (clip_dir / "正常文章.md").write_text(REAL_ARTICLE_MD, encoding="utf-8")
        existing = self._snapshot(clip_dir)
        assert saver._purge_new_verify_clippings(existing) == 0

    def test_empty_existing_snapshot_purges_all_junk(self, clip_dir):
        """空快照（首篇/异常路径）→ 所有微信公众平台剪藏都在清理射程内"""
        junk = clip_dir / "微信公众平台.md"
        junk.write_text(JUNK_VERIFY_MD, encoding="utf-8")
        assert saver._purge_new_verify_clippings(set()) == 1
        assert not junk.exists()


class TestVerifyPageStuckAbort:
    """save_one_article：两轮自动确认后复读快照仍验证页 → 放弃剪藏返回 failed（Fix A）。"""

    @pytest.fixture
    def isolated_vault(self, tmp_path, monkeypatch):
        vault = tmp_path / "Vault"
        clip_dir = vault / "Clippings"
        clip_dir.mkdir(parents=True)
        monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
        monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
        return vault, clip_dir

    @pytest.fixture
    def article(self):
        return {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T",
                "title": "正常文章", "kb": "AI"}

    @pytest.fixture
    def browser_config(self):
        return {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

    def test_stuck_verify_page_aborts_before_clip(
            self, isolated_vault, article, browser_config):
        """复读快照仍验证页 → ('failed', None)，不触发剪藏，关闭标签页"""
        with patch("ima_obsidian_saver.extract_publish_date", return_value="260927"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.wait_page_ready", return_value=0), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=True), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   side_effect=[{"title": "x", "text": "y"},
                                {"title": "微信公众平台", "text": "环境异常"}]), \
             patch("ima_obsidian_saver.activate_browser") as mock_activate, \
             patch("ima_obsidian_saver.trigger_quick_clip") as mock_clip, \
             patch("ima_obsidian_saver.find_and_rename_in_vault") as mock_rename, \
             patch("ima_obsidian_saver.close_tab") as mock_close, \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        assert result == ("failed", None)
        assert saver._LAST_FAILURE_SIGNATURE == "verify_page_stuck"
        mock_clip.assert_not_called()      # 核心断言：不把验证页剪藏成垃圾
        mock_activate.assert_not_called()
        mock_rename.assert_not_called()
        mock_close.assert_called_once()

    def test_cleared_page_still_saves_normally(
            self, isolated_vault, article, browser_config):
        """阴性对照：确认后滑块页已清（复读为真文章）→ 正常走剪藏流程不被误杀"""
        vault, clip_dir = isolated_vault
        with patch("ima_obsidian_saver.extract_publish_date", return_value="260927"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.wait_page_ready", return_value=0), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=True), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   side_effect=[{"title": "x", "text": "y"},
                                {"title": "正常文章", "text": "这是正文"}]), \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault",
                   return_value=(True, "260927")), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        assert result == ("saved", "260927")

    def test_not_found_branch_purges_new_junk(
            self, isolated_vault, article, browser_config, monkeypatch):
        """接线：认领失败时本轮新落盘的微信公众平台剪藏被物理清理（Fix B）"""
        vault, clip_dir = isolated_vault
        old_junk = clip_dir / "微信公众平台 17.md"
        old_junk.write_text(JUNK_VERIFY_MD, encoding="utf-8")
        monkeypatch.setattr(saver, "WAIT_CLIP_TOTAL", 0)  # 轮询循环直接退出

        def _fake_open_url(*_a, **_k):
            # 模拟 quick_clip 打在验证页上：快照之后才落盘的新垃圾
            (clip_dir / "微信公众平台 18.md").write_text(JUNK_VERIFY_MD, encoding="utf-8")

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260927"), \
             patch("ima_obsidian_saver.open_url", side_effect=_fake_open_url), \
             patch("ima_obsidian_saver.wait_page_ready", return_value=0), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=False), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "正常文章", "text": "这是正文"}), \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault",
                   return_value=(False, None)), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        assert result == ("failed", None)
        assert not (clip_dir / "微信公众平台 18.md").exists()  # 新垃圾被清
        assert old_junk.exists()                               # 存量不碰
