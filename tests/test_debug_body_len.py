"""save_one_article 渐进验证 debug print 节流 + IMA_DEBUG_BODY_LEN env 关闭语义。

背景：launchd/cron 长期跑（1000 篇）累积约 50KB 日志全是 `[debug] len(body)=N`
噪声（spec §5）。spec 实证语义是「采集几个不同长度样本」，相同长度重复打印
无信息增益。

策略：
  1. module-level `_DEBUG_BODY_LEN_SEEN: Set[int]` 节流，相同长度只打印一次
  2. env `IMA_DEBUG_BODY_LEN` truthy 解析：识别 '0'/'false'/'no'/'off'/'' 五种
     falsy 值（PoLS：运维习惯性尝试 =false/=no/=off 也应关闭）
"""
from unittest.mock import patch

import pytest

import ima_obsidian_saver as saver


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """复用 test_deleted_page.py 的 vault 隔离模式 + 重置节流状态。

    节流状态是 module-level，会在测试间残留——每个测试 setup 必须显式清空，
    否则同长度跨测试串扰（test A 加了 len=10 后 test B 同长度不打印 = 假通过）。
    """
    vault = tmp_path / "Vault"
    vault.mkdir()
    clip_dir = vault / "Clippings"
    clip_dir.mkdir()
    monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
    monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
    saver._DEBUG_BODY_LEN_SEEN.clear()  # 关键：防止 module-level 状态串扰
    return vault, clip_dir


@pytest.fixture
def env_debug_on(monkeypatch):
    """强制开 debug print（即使 conftest 或环境默认关闭）"""
    monkeypatch.setenv("IMA_DEBUG_BODY_LEN", "1")


def _make_article(text: str):
    return (
        {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "T", "kb": "AI"},
        {"app": "Chrome", "shortcut_mods": ["option", "shift"]},
        {"title": "正常", "text": text},
    )


def _patch_chain(snap):
    """save_one_article 正常路径的全 mock 链（绕开浏览器交互）"""
    return [
        patch("ima_obsidian_saver.extract_publish_date", return_value="260101"),
        patch("ima_obsidian_saver.open_url"),
        patch("ima_obsidian_saver.handle_verify_page", return_value=False),
        patch("ima_obsidian_saver.read_page_snapshot", return_value=snap),
        patch("ima_obsidian_saver.activate_browser"),
        patch("ima_obsidian_saver.trigger_quick_clip"),
        patch("ima_obsidian_saver.close_tab"),
        patch("ima_obsidian_saver.find_and_rename_in_vault", return_value=(True, "260101")),
        patch("ima_obsidian_saver.time.sleep"),
    ]


class TestDebugBodyLenThrottle:
    """_DEBUG_BODY_LEN_SEEN 节流：相同长度只打印一次。"""

    def test_same_body_len_prints_only_once(self, isolated_vault, env_debug_on, capsys):
        """同 len(body) 调两次 save_one_article，[debug] 行只出现一次。"""
        article, browser_config, snap = _make_article("这是正文内容")  # len=6
        expected_len = len(snap["text"])

        ctx = _patch_chain(snap)
        for c in ctx:
            c.__enter__()
        try:
            saver.save_one_article(article, browser_config)
            saver.save_one_article(article, browser_config)  # 第二次：重复长度
        finally:
            for c in ctx:
                c.__exit__(None, None, None)

        captured = capsys.readouterr().out
        debug_lines = [l for l in captured.split("\n") if "[debug] len(body)" in l]
        assert len(debug_lines) == 1, f"同长度二次调用不应重复打印，实际: {debug_lines}"
        assert f"len(body)={expected_len}" in debug_lines[0]
        assert expected_len in saver._DEBUG_BODY_LEN_SEEN

    def test_different_body_len_each_prints_once(self, isolated_vault, env_debug_on, capsys):
        """不同 len(body) 各打印一次（节流不能误杀新长度）。"""
        # 三个不同长度：6, 9, 1
        snaps = [
            {"title": "T", "text": "这是正文内容"},        # len=6
            {"title": "T", "text": "这是另一篇正文内容"},  # len=9
            {"title": "T", "text": "短"},                  # len=1
        ]
        seen_lengths = []
        for snap in snaps:
            article, browser_config, _ = _make_article(snap["text"])
            ctx = _patch_chain(snap)
            for c in ctx:
                c.__enter__()
            try:
                saver.save_one_article(article, browser_config)
            finally:
                for c in ctx:
                    c.__exit__(None, None, None)
            seen_lengths.append(len(snap["text"]))

        captured = capsys.readouterr().out
        debug_lines = [l for l in captured.split("\n") if "[debug] len(body)" in l]
        assert len(debug_lines) == 3, f"三个不同长度应各打印一次，实际: {debug_lines}"
        for n in seen_lengths:
            assert f"len(body)={n}" in captured
            assert n in saver._DEBUG_BODY_LEN_SEEN


class TestDebugBodyLenEnvSwitch:
    """IMA_DEBUG_BODY_LEN env 关闭语义：truthy 解析 5 种 falsy 值。

    PoLS：运维习惯性尝试 =false/=no/=off 也应关闭，不只识别 '0'。
    """

    @pytest.mark.parametrize("falsy_val", ["0", "false", "no", "off", ""])
    def test_falsy_env_values_disable_print(self, isolated_vault, monkeypatch, capsys, falsy_val):
        """5 种 falsy 值都应关闭 [debug] 输出。"""
        monkeypatch.setenv("IMA_DEBUG_BODY_LEN", falsy_val)
        article, browser_config, snap = _make_article("这是正文内容")

        ctx = _patch_chain(snap)
        for c in ctx:
            c.__enter__()
        try:
            saver.save_one_article(article, browser_config)
        finally:
            for c in ctx:
                c.__exit__(None, None, None)

        captured = capsys.readouterr().out
        assert "[debug] len(body)" not in captured, (
            f"IMA_DEBUG_BODY_LEN={falsy_val!r} 应关闭 debug print，但仍打印了"
        )

    @pytest.mark.parametrize("truthy_val", ["1", "yes", "true", "on", "random"])
    def test_truthy_env_values_keep_print(self, isolated_vault, monkeypatch, capsys, truthy_val):
        """非 falsy 值（含 unset 默认）应保持 debug print 开启。"""
        monkeypatch.setenv("IMA_DEBUG_BODY_LEN", truthy_val)
        article, browser_config, snap = _make_article("这是正文内容")

        ctx = _patch_chain(snap)
        for c in ctx:
            c.__enter__()
        try:
            saver.save_one_article(article, browser_config)
        finally:
            for c in ctx:
                c.__exit__(None, None, None)

        captured = capsys.readouterr().out
        assert "[debug] len(body)" in captured, (
            f"IMA_DEBUG_BODY_LEN={truthy_val!r} 应保持 debug print 开启，但未打印"
        )

    def test_unset_env_defaults_to_on(self, isolated_vault, monkeypatch, capsys):
        """env 未设置时默认开启（默认值 '1'）。"""
        monkeypatch.delenv("IMA_DEBUG_BODY_LEN", raising=False)
        article, browser_config, snap = _make_article("这是正文内容")

        ctx = _patch_chain(snap)
        for c in ctx:
            c.__enter__()
        try:
            saver.save_one_article(article, browser_config)
        finally:
            for c in ctx:
                c.__exit__(None, None, None)

        captured = capsys.readouterr().out
        assert "[debug] len(body)" in captured, "env 未设置时应默认开启 debug print"

    @pytest.mark.parametrize("upper_val", ["FALSE", "NO", "OFF", "False", "No"])
    def test_case_insensitive_falsy(self, isolated_vault, monkeypatch, capsys, upper_val):
        """falsy 解析大小写不敏感（.lower() 归一化）。"""
        monkeypatch.setenv("IMA_DEBUG_BODY_LEN", upper_val)
        article, browser_config, snap = _make_article("这是正文内容")

        ctx = _patch_chain(snap)
        for c in ctx:
            c.__enter__()
        try:
            saver.save_one_article(article, browser_config)
        finally:
            for c in ctx:
                c.__exit__(None, None, None)

        captured = capsys.readouterr().out
        assert "[debug] len(body)" not in captured, (
            f"IMA_DEBUG_BODY_LEN={upper_val!r} 大小写应被 .lower() 归一化为 falsy"
        )
