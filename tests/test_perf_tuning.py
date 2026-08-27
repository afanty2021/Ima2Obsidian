"""保存链路性能调优（2026-08-27）的回归测试。

五项优化：
1. 正式跑跳过 extract_publish_date 的 requests 预取（微信精简页必失配，纯白付 HTTP）
2. 固定 6s 页面加载 → wait_page_ready 的 readyState 自适应轮询
3. 快照三次 AppleScript 往返合并为一次（publish_time 并入 read_page_snapshot）
4. 落盘确认 4s 固定起步窗 → 早轮询 + _file_write_settled 半成品防护
5. wait_for_ax_ready 预算 30s → 12s（降级契约不变）

参考今天 16:10 实测：happy-path 篇均 ~21s，以上目标砍到 ~12s。
"""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ima_obsidian_saver as saver


def _today():
    return datetime.now().strftime("%y%m%d")


class TestSkipDatePrefetch:
    """优化1：正式跑不得发起 requests 预取；真实日期由快照 publish_time 覆盖。"""

    def _run_happy_path(self, tmp_path, monkeypatch, snapshot,
                        md_body="*2026年5月4日 10:00*\n正文"):
        """完整跑通 save_one_article happy-path（文件已"落盘"在 Clippings），返回 (status, date)。

        默认 md_body 带内容日期（内容日期优先级最高）；断言 publish_time 通路的用例
        应传不带日期标记的 md_body，否则内容日期总会赢。"""
        vault = tmp_path / "Vault"
        clippings = vault / "Clippings"
        clippings.mkdir(parents=True)
        target_dir = vault / "AI"
        target_dir.mkdir()
        monkeypatch.setattr(saver, "VAULT_DIR", vault)
        monkeypatch.setattr(saver, "CLIPPINGS_DIR", clippings)
        monkeypatch.setattr(saver, "WAIT_CLIP_TOTAL", 5)

        # 模拟 Web Clipper 已落盘的文章（标题与待存文章互为子串；两端 len>10 才进精确匹配分支）
        title = "这是一篇集成测试用的长标题文章 ABC"
        md = clippings / f"{title}.md"
        md.write_text(md_body, encoding="utf-8")

        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?x=1", "title": title}
        cfg = {"app": "Google Chrome", "shortcut_mods": ["cmd", "shift"]}
        base = [
            patch("ima_obsidian_saver.requests.get",
                  side_effect=AssertionError("正式跑不应发起 requests 预取")),
            patch("ima_obsidian_saver.wait_page_ready", return_value=1.0),
            patch("ima_obsidian_saver.open_url"),
            patch("ima_obsidian_saver.read_page_snapshot", return_value=snapshot),
            patch("ima_obsidian_saver.handle_verify_page", return_value=False),
            patch("ima_obsidian_saver.activate_browser"),
            patch("ima_obsidian_saver.get_frontmost_app", return_value="Chrome"),
            patch("ima_obsidian_saver.trigger_clipper_with_receipt", return_value=True),
            patch("ima_obsidian_saver.time.sleep"),
            patch("ima_obsidian_saver.close_tab"),
        ]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in base:
                stack.enter_context(p)
            return saver.save_one_article(article, cfg, mode="clipper", target_folder="AI")

    def test_live_run_never_calls_requests(self, tmp_path, monkeypatch):
        """requests.get 被断言为立即失败——若走到预取路径，测试会炸而不是静默变慢"""
        snapshot = {"title": "t", "text": "正文", "publish_time": "2026年7月15日 09:56"}
        status, date_str = self._run_happy_path(tmp_path, monkeypatch, snapshot)
        assert status == "saved"

    def test_snapshot_publish_time_overrides_fallback_date(self, tmp_path, monkeypatch):
        """命名兜底日期被 publish_time 真实日期覆盖（优化3 的日期通路）"""
        snapshot = {"title": "t", "text": "正文", "publish_time": "2026年7月15日 09:56"}
        _, date_str = self._run_happy_path(tmp_path, monkeypatch, snapshot,
                                           md_body="正文无日期标记")  # 排除内容日期竞争
        assert date_str == "260715"

    def test_content_date_still_overrides_when_publish_time_missing(self, tmp_path, monkeypatch):
        """publish_time 缺席时，文件内容 *YYYY年M月D日* 仍是二级来源（旧链路保留）"""
        snapshot = {"title": "t", "text": "正文"}  # 兼容 shim 会补 publish_time=''
        _, date_str = self._run_happy_path(tmp_path, monkeypatch, snapshot)
        assert date_str == "260504"

    def test_dry_run_still_uses_prefetch(self):
        """dry-run 是给人看的预览，仍走 extract_publish_date（不在本次提速范围）"""
        article = {"id": 1, "url": "https://x", "title": "t"}
        cfg = {"app": "Google Chrome", "shortcut_mods": []}
        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101") as m:
            status, _ = saver.save_one_article(article, cfg, dry_run=True)
        assert status == "saved"
        m.assert_called_once()


class TestWaitPageReady:
    """优化2：readyState 自适应轮询。"""

    def test_returns_early_on_complete(self):
        sleeps = []
        calls = iter(["interactive", "complete"])
        with patch("ima_obsidian_saver.execute_chrome_js",
                   side_effect=lambda *a: next(calls)), \
             patch("ima_obsidian_saver.time.sleep", side_effect=sleeps.append):
            waited = saver.wait_page_ready("Google Chrome", max_wait=6.0)
        assert waited < 6.0                       # 提前就绪，没睡满上限
        assert any(abs(s - saver.WAIT_PAGE_SETTLE) < 1e-9 for s in sleeps)

    def test_degrades_after_consecutive_misses(self):
        """连续拿不到结果（权限丢失等）：4 次后不再空转，直接把余量睡掉"""
        sleeps = []
        count = {"n": 0}

        def fake_js(*a):
            count["n"] += 1
            return None

        with patch("ima_obsidian_saver.execute_chrome_js", side_effect=fake_js), \
             patch("ima_obsidian_saver.time.sleep", side_effect=lambda s: sleeps.append(s)):
            waited = saver.wait_page_ready("Google Chrome", max_wait=6.0)
        assert count["n"] == 4                    # 尝试 4 次即止
        assert len(sleeps) >= 1                   # 余量一次性睡掉（sleep 本身已被 mock）

    def test_times_out_on_eternal_loading(self):
        """永远是 loading：受 MAX_PAGE_POLLS 硬上限约束，不无限打 osascript"""
        # 注：sleep 被 mock 后轮询近零耗时，次数上限会比时间上限先到（生产中
        # 单次 osascript ~100ms，60 次 ≈ 时间预算自然到期）
        calls = {"n": 0}

        def fake_js(*a):
            calls["n"] += 1
            return "loading"

        with patch("ima_obsidian_saver.execute_chrome_js", side_effect=fake_js), \
             patch("ima_obsidian_saver.time.sleep"):
            saver.wait_page_ready("Google Chrome", max_wait=6.0)
        assert calls["n"] == saver.MAX_PAGE_POLLS

    def test_non_chrome_sleeps_full_budget(self):
        """Safari 等 AppleScript execute JS 不可用的浏览器：退化为固定等待"""
        def boom(*a):
            raise AssertionError("非 Chrome 不应尝试 execute_chrome_js")
        with patch("ima_obsidian_saver.execute_chrome_js", side_effect=boom), \
             patch("ima_obsidian_saver.time.sleep") as ms:
            waited = saver.wait_page_ready("Safari", max_wait=2.0)
        assert waited == 2.0
        ms.assert_called_once_with(2.0)


class TestExtractDateFromSnapshot:
    def test_parses_full_format(self):
        assert saver.extract_date_from_snapshot(
            {"publish_time": "2026年7月15日 09:56"}) == "260715"

    def test_single_digit_month_day(self):
        assert saver.extract_date_from_snapshot(
            {"publish_time": "2026年7月5日"}) == "260705"

    def test_none_on_missing_key_or_none_snapshot(self):
        assert saver.extract_date_from_snapshot({}) is None
        assert saver.extract_date_from_snapshot(None) is None

    def test_none_on_invalid_values(self):
        assert saver.extract_date_from_snapshot({"publish_time": "微信公众平台"}) is None
        assert saver.extract_date_from_snapshot({"publish_time": "2026年13月40日"}) is None


class TestHrefMatchesUrl:
    """URL 守卫匹配规则：sn= 是微信文章唯一标识，路径相同不能作为新旧页判据。"""

    def test_sn_param_is_strong_match(self):
        url = "https://mp.weixin.qq.com/s?__biz=A&mid=1&idx=1&sn=abc123"
        assert saver._href_matches_url(
            "https://mp.weixin.qq.com/s?__biz=A&mid=1&idx=1&sn=abc123", url) is True

    def test_same_path_different_sn_rejected(self):
        """上一篇文章也停在 /s 路径——只认 sn 才能区分新旧页（本回归的根因）"""
        url = "https://mp.weixin.qq.com/s?__biz=A&mid=1&idx=1&sn=NEW"
        stale = "https://mp.weixin.qq.com/s?__biz=A&mid=99&idx=1&sn=OLD"
        assert saver._href_matches_url(stale, url) is False

    def test_no_sn_falls_back_to_full_prefix(self):
        url = "https://example.com/articles/foo?id=7"
        assert saver._href_matches_url("https://example.com/articles/foo?id=7", url) is True
        assert saver._href_matches_url("https://other.com/x", url) is False

    def test_empty_href_never_matches(self):
        assert saver._href_matches_url("", "https://x.com/a?sn=1") is False

    def test_real_short_link_lands_on_short_form(self):
        """真实 Chrome 实测回归钉：/s/<token> 短链加载后 href 保持短链不变。

        2026-08-27 用 osascript 在本机 Chrome 打开生产库真实短链观测：
        `complete|https://mp.weixin.qq.com/s/-36m-d7VURKgoEusIwvggQ`（无 HTTP 重定向；
        页面 og:url 即短链，history.replaceState 仅回放 location.href）。据此
        弱前缀匹配可即时命中（reviews 曾误判此处存在永久失配）。

        注意：本用例是匹配器契约锚点（纯函数、不触网），只能防弱前缀分支被
        改坏或跨形态误判；它检测不到微信侧改地址——若那发生，本测试仍绿，
        生产会退化为每篇睡满 WAIT_PAGE_LOAD_MAX + 切页告警，需靠日志发现，
        届时引入重定向预解析。真正的微信行为哨兵需网络标记测试，另行建设。
        """
        url = "https://mp.weixin.qq.com/s/-36m-d7VURKgoEusIwvggQ"
        assert saver._href_matches_url(url, url) is True            # 落点即短链
        # 切换前的旧页（无论长短形态）都不得放行
        assert saver._href_matches_url(
            "https://mp.weixin.qq.com/s?__biz=A&mid=1&idx=1&sn=deadbeef00", url) is False
        assert saver._href_matches_url(
            "https://mp.weixin.qq.com/s/differenttoken9999", url) is False


class TestWaitPageReadyUrlGuard:
    """评审 Important 修复：就绪判定必须确认活动标签页已切到本篇。"""

    def test_blocks_on_complete_but_stale_tab(self):
        """旧标签页也是 complete——不匹配 URL 就不能放行"""
        url = "https://mp.weixin.qq.com/s?sn=fresh"
        seq = iter([
            f"complete|https://mp.weixin.qq.com/s?sn=stale",   # 还停在上篇
            f"interactive|{url}",                              # 已切换未加载完
            f"complete|{url}",                                 # 真正就绪
        ])
        sleeps = []
        with patch("ima_obsidian_saver.execute_chrome_js",
                   side_effect=lambda *a: next(seq)), \
             patch("ima_obsidian_saver.time.sleep", side_effect=sleeps.append):
            waited = saver.wait_page_ready("Google Chrome", max_wait=6.0,
                                           require_url=url)
        assert waited < 6.0
        assert any(abs(s - saver.WAIT_PAGE_SETTLE) < 1e-9 for s in sleeps)

    def test_blank_tab_delayed_switch(self):
        """冷启动新标签页短暂为空标题：complete 不带本篇 href 同样拦下"""
        url = "https://mp.weixin.qq.com/s?sn=x"
        seq = iter(["complete|", f"complete|about:blank|", f"complete|{url}"])
        with patch("ima_obsidian_saver.execute_chrome_js",
                   side_effect=lambda *a: next(seq)), \
             patch("ima_obsidian_saver.time.sleep"):
            waited = saver.wait_page_ready("Google Chrome", max_wait=6.0,
                                           require_url=url)
        assert waited < 6.0

    def test_never_matching_degrades_at_poll_cap(self):
        """永不匹配：受 MAX_PAGE_POLLS 硬上限约束退出（同 eternal-loading 的 mock 语义）"""
        url = "https://mp.weixin.qq.com/s?sn=x"
        calls = {"n": 0}

        def fake_js(*a):
            calls["n"] += 1
            return "complete|https://elsewhere/?sn=y"

        with patch("ima_obsidian_saver.execute_chrome_js", side_effect=fake_js), \
             patch("ima_obsidian_saver.time.sleep"):
            saver.wait_page_ready("Google Chrome", max_wait=6.0, require_url=url)
        assert calls["n"] == saver.MAX_PAGE_POLLS

    def test_garbage_returns_capped_by_poll_budget(self):
        """有返回但永不满足（垃圾字符串）：达 MAX_PAGE_POLLS 即止，不再无限打 osascript"""
        calls = {"n": 0}

        def fake_js(*a):
            calls["n"] += 1
            return "weird"

        with patch("ima_obsidian_saver.execute_chrome_js", side_effect=fake_js), \
             patch("ima_obsidian_saver.time.sleep"):
            waited = saver.wait_page_ready("Google Chrome", max_wait=60.0)
        assert calls["n"] == saver.MAX_PAGE_POLLS


class TestPublishTimeRetry:
    """发布日期两级来源 + 落空显式告警（评审 Important：冷启动静默按今日命名）。"""

    def _run_impl(self, tmp_path, monkeypatch, snapshot):
        """跑通 happy-path，返回 (status, date, slept, sleep_mock)。

        extract_publish_date_js 不在此 patch——是否触发 JS 兜底是各用例的被测行为。
        """
        vault = tmp_path / "Vault"
        clippings = vault / "Clippings"
        target = vault / "AI"
        target.mkdir(parents=True)
        clippings.mkdir()
        monkeypatch.setattr(saver, "VAULT_DIR", vault)
        monkeypatch.setattr(saver, "CLIPPINGS_DIR", clippings)
        monkeypatch.setattr(saver, "WAIT_CLIP_TOTAL", 5)

        title = "这是一篇集成测试用的长标题文章 XYZ"
        (clippings / f"{title}.md").write_text("无日期标记正文", encoding="utf-8")

        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?sn=q", "title": title}
        cfg = {"app": "Google Chrome", "shortcut_mods": ["cmd", "shift"]}
        base = [
            patch("ima_obsidian_saver.requests.get",
                  side_effect=AssertionError("正式跑不应发起 requests 预取")),
            patch("ima_obsidian_saver.wait_page_ready", return_value=1.0),
            patch("ima_obsidian_saver.open_url"),
            patch("ima_obsidian_saver.read_page_snapshot", return_value=snapshot),
            patch("ima_obsidian_saver.handle_verify_page", return_value=False),
            patch("ima_obsidian_saver.activate_browser"),
            patch("ima_obsidian_saver.get_frontmost_app", return_value="Chrome"),
            patch("ima_obsidian_saver.trigger_clipper_with_receipt", return_value=True),
            patch("ima_obsidian_saver.close_tab"),
        ]
        from contextlib import ExitStack
        slept = []
        with ExitStack() as stack:
            for p in base:
                stack.enter_context(p)
            sleep_mock = stack.enter_context(patch(
                "ima_obsidian_saver.time.sleep",
                side_effect=lambda s: slept.append(s)))
            status, date_str = saver.save_one_article(article, cfg, mode="clipper",
                                                      target_folder="AI")
        return status, date_str, slept, sleep_mock

    def test_present_publish_time_skips_fallback(self, tmp_path, monkeypatch):
        """快照已含日期：JS 兜底通道不应被触发（多余往返）"""
        snapshot = {"title": "t", "text": "b", "publish_time": "2026年7月15日 09:00"}
        with patch("ima_obsidian_saver.extract_publish_date_js",
                   side_effect=AssertionError("publish_time 已命中，不应走 JS 兜底")):
            status, date_str, _, _ = self._run_impl(tmp_path, monkeypatch, snapshot)
        assert (status, date_str) == ("saved", "260715")

    def test_empty_snap_falls_back_to_js_retry(self, tmp_path, monkeypatch):
        """快照过早 publish_time 空 → 短等后 JS 兜底重读一次命中"""
        snapshot = {"title": "t", "text": "b"}  # shim 补 publish_time=''
        with patch("ima_obsidian_saver.extract_publish_date_js",
                   return_value="260811") as mjs:
            status, date_str, slept, _ = self._run_impl(tmp_path, monkeypatch, snapshot)
        assert (status, date_str) == ("saved", "260811")
        assert any(abs(s - saver.WAIT_PUBLISH_TIME_RETRY) < 1e-9 for s in slept)
        assert mjs.call_count == 1

    def test_both_sources_missing_warns_and_uses_today(self, tmp_path, monkeypatch, capsys):
        snapshot = {"title": "t", "text": "b"}
        with patch("ima_obsidian_saver.extract_publish_date_js", return_value=None):
            status, date_str, _, _ = self._run_impl(tmp_path, monkeypatch, snapshot)
        assert status == "saved"
        assert date_str == datetime.now().strftime("%y%m%d")
        assert "真实发布日期未取到" in capsys.readouterr().out


class TestFileWriteSettled:
    def test_stable_file_accepted(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("x")
        with patch("ima_obsidian_saver.time.sleep"):   # 双采样间无需真等（文件未变即稳定）
            assert saver._file_write_settled(f) is True

    def test_unstable_size_rejected(self, tmp_path, monkeypatch):
        """两次采样 size 变化（渐进写入中）→ 视为半成品拒绝认领"""
        f = tmp_path / "a.md"
        samples = iter([SimpleNamespace(st_size=10, st_mtime=1.0),
                        SimpleNamespace(st_size=20, st_mtime=1.0)])
        monkeypatch.setattr(Path, "stat",
                            lambda self, *a, **k: next(samples), raising=False)
        with patch("ima_obsidian_saver.time.sleep"):
            assert saver._file_write_settled(f) is False

    def test_vanishing_file_rejected(self, tmp_path, monkeypatch):
        """第二次采样前文件消失（被其他进程移动）→ 拒绝"""
        f = tmp_path / "a.md"
        states = iter([SimpleNamespace(st_size=10, st_mtime=1.0), OSError(2, "gone")])

        def flaky_stat(self, *a, **k):
            v = next(states)
            if isinstance(v, OSError):
                raise v
            return v

        monkeypatch.setattr(Path, "stat", flaky_stat, raising=False)
        with patch("ima_obsidian_saver.time.sleep"):
            assert saver._file_write_settled(f) is False

    def test_first_stat_failure_rejected(self, tmp_path, monkeypatch):
        f = tmp_path / "never.md"
        monkeypatch.setattr(Path, "stat",
                            lambda self, *a, **k: (_ for _ in ()).throw(OSError(2, "no")),
                            raising=False)
        assert saver._file_write_settled(f) is False


class TestFindAndRenameRequireStable:
    """require_stable=True 时半成品候选本轮不认领，交外层轮询重试。"""

    def _make_clippings(self, tmp_path, monkeypatch):
        vault = tmp_path / "Vault"
        clippings = vault / "Clippings"
        clippings.mkdir(parents=True)
        monkeypatch.setattr(saver, "VAULT_DIR", vault)
        monkeypatch.setattr(saver, "CLIPPINGS_DIR", clippings)
        return clippings

    def test_unsettled_exact_match_skips_claim(self, tmp_path, monkeypatch):
        clippings = self._make_clippings(tmp_path, monkeypatch)
        md = clippings / "这是一篇标题足够长的测试文章.md"
        md.write_text("正文")
        with patch.object(saver, "_file_write_settled", return_value=False) as m:
            renamed, date = saver.find_and_rename_in_vault(
                "这是一篇标题足够长的测试文章", _today(), set(), require_stable=True)
        assert (renamed, date) == (False, None)
        m.assert_called_once()                    # 精确匹配分支也走了稳定性检查
        assert md.exists()                        # 文件未被改名/移动

    def test_unsettled_new_file_branch_skips_claim(self, tmp_path, monkeypatch):
        """第二步「唯一新文件」兜底同样受守卫保护"""
        clippings = self._make_clippings(tmp_path, monkeypatch)
        md = clippings / "Web Clipper 默认名完全不同的产物.md"
        md.write_text("正文")
        existing = {(md, 12345)}                  # 标题对不上但会在新文件分支命中
        existing = {e for e in existing}
        # 让 rglob 能看到它且 not-in-existing：existing 用假路径条目
        real_existing = {("/nonexistent/a.md", 0)}
        with patch.object(saver, "_file_write_settled", return_value=False):
            renamed, _ = saver.find_and_rename_in_vault(
                "一篇和文件名毫无关系但我们知道它唯一的文章",
                _today(), real_existing, require_stable=True)
        assert renamed is False
        assert list(clippings.glob("*.md")) == [md]

    def test_default_off_keeps_legacy_behavior(self, tmp_path, monkeypatch):
        """不传 require_stable（reclaim 等调用方）：稳定性检查根本不触发"""
        clippings = self._make_clippings(tmp_path, monkeypatch)
        md = clippings / "又是一篇标题足够长的回归样例文章.md"
        md.write_text("正文")
        def boom(*a, **k):
            raise AssertionError("默认关闭时不该做双采样")
        with patch.object(saver, "_file_write_settled", side_effect=boom):
            renamed, _ = saver.find_and_rename_in_vault(
                "又是一篇标题足够长的回归样例文章", _today(), set())
        assert renamed is True

    def test_settled_exact_match_renames_normally(self, tmp_path, monkeypatch):
        clippings = self._make_clippings(tmp_path, monkeypatch)
        (vault_ai := tmp_path / "Vault" / "AI").mkdir()
        md = clippings / "再一篇标题够长的稳定落盘文章样例.md"
        md.write_text("*2026年3月3日 08:00*\n正文")
        target = str(vault_ai)
        with patch.object(saver, "_file_write_settled", return_value=True):
            renamed, date = saver.find_and_rename_in_vault(
                "再一篇标题够长的稳定落盘文章样例", _today(), set(),
                target_folder="AI", require_stable=True)
        assert renamed is True and date == "260303"
        saved = list(Path(target).glob("*.md"))
        assert len(saved) == 1 and saved[0].name.startswith("260303")


class TestHandleVerifyPageInitialSnap:
    """handle_verify_page 新增 initial_snap 复用参数。"""

    def test_non_verify_initial_snap_skips_probe(self):
        """非验证页 + 复用快照：read_page_snapshot 一次都不该调（省往返的主路径）"""
        def boom(*a):
            raise AssertionError("复用快照时不应再探测")
        with patch("ima_obsidian_saver.read_page_snapshot", side_effect=boom), \
             patch("ima_obsidian_saver.click_confirm") as mc:
            ret = saver.handle_verify_page("Google Chrome",
                                           initial_snap={"title": "文章", "text": "正文"})
        assert ret is False
        mc.assert_not_called()

    def test_verify_snap_clicks_then_probes_fresh_once(self):
        """复用快照判为验证页 → 点确认后必须重新探测（不能拿旧快照二次判定）"""
        fresh = [{"title": "文章", "text": "正文"}]
        with patch("ima_obsidian_saver.read_page_snapshot",
                   side_effect=fresh) as mr, \
             patch("ima_obsidian_saver.click_confirm", return_value=True), \
             patch("ima_obsidian_saver.time.sleep"):
            ret = saver.handle_verify_page(
                "Google Chrome",
                initial_snap={"title": "微信公众平台", "text": "当前环境异常"})
        assert ret is True
        assert mr.call_count == 1                 # 只补探测一次

    def test_none_behaves_like_legacy(self):
        """initial_snap 缺省/None 与旧签名完全一致（自行探测）"""
        with patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "文章", "text": "正文"}) as mr:
            ret = saver.handle_verify_page("Google Chrome")
        assert ret is False
        assert mr.call_count == 1                 # 旧版首轮探测恰好一次


class TestAxReadyBudget:
    """优化5：wait_for_ax_ready 降到 12s 且降级契约不变。

    模块 log() 非 TTY 下只写 LOG_FILE——重定向到 tmp 防污染生产日志，断言读文件。
    """

    def _patch_env(self, tmp_path, monkeypatch, static_texts):
        import ima_incremental_update as inc
        monkeypatch.setattr(inc, "LOG_FILE", tmp_path / "inc_test.log")
        monkeypatch.setattr(
            "ima_incremental_update.get_ima_main_window",
            lambda: {"pid": 100, "window_id": 1, "is_on_screen": True})
        tree = json.dumps({"tree_markdown": "AXStaticText " * static_texts})

        def fake_cua(args, timeout=None):
            return tree

        monkeypatch.setattr("ima_incremental_update.run_cua", fake_cua)
        slept = []
        monkeypatch.setattr("ima_incremental_update.time.sleep",
                            lambda s: slept.append(s))
        return inc, slept

    def _log_text(self, tmp_path):
        return (tmp_path / "inc_test.log").read_text(encoding="utf-8")

    def test_default_timeout_is_twelve(self, monkeypatch):
        import inspect
        import ima_incremental_update as inc
        sig = inspect.signature(inc.wait_for_ax_ready)
        assert sig.parameters["timeout"].default == 12

    def test_below_threshold_times_out_fast(self, tmp_path, monkeypatch):
        inc, _ = self._patch_env(tmp_path, monkeypatch, static_texts=1)  # 永远只有 1 个
        ok = inc.wait_for_ax_ready(min_elements=5, timeout=2)
        assert ok is False
        assert "AX 树就绪等待超时" in self._log_text(tmp_path)          # 降级日志仍在

    def test_threshold_met_returns_true_immediately(self, tmp_path, monkeypatch):
        inc, _ = self._patch_env(tmp_path, monkeypatch, static_texts=8)
        ok = inc.wait_for_ax_ready(min_elements=5, timeout=12)
        assert ok is True
        assert "✅ AX 树就绪（8 个元素）" in self._log_text(tmp_path)

    def test_offscreen_window_triggers_bring_to_front(self, tmp_path, monkeypatch):
        """跨 Space 场景：is_on_screen=False → bring_to_front 被调且不中断等待"""
        inc, slept = self._patch_env(tmp_path, monkeypatch, static_texts=8)
        front_calls = []
        monkeypatch.setattr(
            "ima_incremental_update.run_cua",
            lambda args, timeout=None: (
                front_calls.append(args) or "null"
                if args[:2] == ["call", "bring_to_front"] else
                json.dumps({"tree_markdown": "AXStaticText " * 8})))
        monkeypatch.setattr(
            "ima_incremental_update.get_ima_main_window",
            lambda: {"pid": 100, "window_id": 1, "is_on_screen": False})
        ok = inc.wait_for_ax_ready(min_elements=5, timeout=3)
        assert ok is True
        assert any(a[:2] == ["call", "bring_to_front"] for a in front_calls)
