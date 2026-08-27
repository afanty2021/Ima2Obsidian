"""环境预检与漂移预警：Profile/扩展/快捷键/输入法前提的显式断言。

背景（2026-08-12 故障）：换 Chrome 登录账号后自动化标签页落到新 Profile，
该 Profile 没装 Web Clipper，保存器 0 落盘且 rc=0 无声，静默 15 天。
本组测试保证预检/快照逻辑本身不回归。
"""
import json

import pytest

import ima_common
from ima_common import (
    WEB_CLIPPER_EXT_ID,
    diff_snapshots,
    ime_blocks_option_shortcuts,
    read_chrome_profile_info,
    read_web_clipper_status,
    save_snapshot_and_report_drift,
)


# ==================== fixtures ====================

def _make_chrome_dir(tmp_path, last_used="Profile 1", ext_settings=None):
    """构造隔离的 Chrome 用户目录：Local State + Profile 1/Secure Preferences。

    ext_settings：写到 extensions.settings[WEB_CLIPPER_EXT_ID] 的 dict；
    None 表示不写该扩展（未安装场景）。
    """
    (tmp_path / "Local State").write_text(json.dumps({
        "profile": {
            "last_used": last_used,
            "last_active_profiles": [last_used],
            "info_cache": {
                "Default": {"name": "您的 Chrome", "user_name": "a@gmail.com"},
                "Profile 1": {"name": "James", "user_name": "b@gmail.com"},
            },
        },
    }, ensure_ascii=False))
    prof_dir = tmp_path / last_used
    prof_dir.mkdir(parents=True, exist_ok=True)
    settings = ext_settings if ext_settings is not None else {}
    (prof_dir / "Secure Preferences").write_text(json.dumps({
        "extensions": {"settings": settings},
    }, ensure_ascii=False))
    return tmp_path


def _clipper_ext(version="1.7.1", state=None, disable_reasons=None, commands=None):
    """一份「已安装」的 Web Clipper 扩展记录；state/reasons 缺省时不写键
    （Chrome 只在禁用时写 state=0，显式 null 与缺省同义）。"""
    ext = {
        "manifest": {"name": "Obsidian Web Clipper", "version": version},
        "commands": commands if commands is not None else {
            "_execute_action": {"suggested_key": {"mac": "Command+Shift+O"}},
            "quick_clip": {"suggested_key": {"default": "Alt+Shift+O"}},
        },
    }
    if state is not None:
        ext["state"] = state
    if disable_reasons is not None:
        ext["disable_reasons"] = disable_reasons
    return ext


# ==================== read_chrome_profile_info ====================

class TestReadChromeProfileInfo:

    def test_reads_last_used_profile(self, tmp_path):
        d = _make_chrome_dir(tmp_path, last_used="Profile 1")
        info = read_chrome_profile_info(d)
        assert info == {"dir": "Profile 1", "name": "James", "user": "b@gmail.com"}

    def test_default_profile_when_no_last_used(self, tmp_path):
        d = _make_chrome_dir(tmp_path, last_used="")
        info = read_chrome_profile_info(d)
        assert info["dir"] == "Default"
        assert info["name"] == "您的 Chrome"

    def test_missing_local_state_returns_empty(self, tmp_path):
        assert read_chrome_profile_info(tmp_path) == {}


# ==================== read_web_clipper_status ====================

class TestReadWebClipperStatus:

    def test_installed_enabled_with_commands(self, tmp_path):
        _make_chrome_dir(tmp_path, ext_settings={
            WEB_CLIPPER_EXT_ID: _clipper_ext(version="1.7.1"),
        })
        st = read_web_clipper_status("Profile 1", tmp_path)
        assert st["installed"] is True
        assert st["enabled"] is True
        assert st["version"] == "1.7.1"
        assert st["commands"]["_execute_action"] == "Command+Shift+O"
        assert st["commands"]["quick_clip"] == "Alt+Shift+O"

    def test_disabled_by_state_zero(self, tmp_path):
        _make_chrome_dir(tmp_path, ext_settings={
            WEB_CLIPPER_EXT_ID: _clipper_ext(state=0),
        })
        st = read_web_clipper_status("Profile 1", tmp_path)
        assert st["installed"] is True
        assert st["enabled"] is False

    def test_disabled_by_disable_reasons(self, tmp_path):
        _make_chrome_dir(tmp_path, ext_settings={
            WEB_CLIPPER_EXT_ID: _clipper_ext(disable_reasons=[1]),
        })
        st = read_web_clipper_status("Profile 1", tmp_path)
        assert st["enabled"] is False

    def test_not_installed_in_this_profile(self, tmp_path):
        # 扩展装在 Default，但读的是 Profile 1 —— 2026-08 故障的抽象复现
        d = _make_chrome_dir(tmp_path, ext_settings={})
        default_dir = d / "Default"
        default_dir.mkdir(parents=True, exist_ok=True)
        (default_dir / "Secure Preferences").write_text(json.dumps({
            "extensions": {"settings": {WEB_CLIPPER_EXT_ID: _clipper_ext()}},
        }, ensure_ascii=False))
        st = read_web_clipper_status("Profile 1", d)
        assert st["installed"] is False

    def test_string_form_suggested_key_parsed(self, tmp_path):
        # 用户自定义绑定时 Chrome 会把 suggested_key 直接写成字符串
        _make_chrome_dir(tmp_path, ext_settings={
            WEB_CLIPPER_EXT_ID: _clipper_ext(commands={
                "_execute_action": {"suggested_key": "Command+Shift+O"},
            }),
        })
        st = read_web_clipper_status("Profile 1", tmp_path)
        assert st["commands"]["_execute_action"] == "Command+Shift+O"

    def test_unreadable_prefs_returns_not_installed(self, tmp_path):
        _make_chrome_dir(tmp_path)
        st = read_web_clipper_status("Profile 1", tmp_path / "nonexistent")
        assert st == {"installed": False, "enabled": False,
                      "version": None, "commands": {}}


# ==================== IME ====================

class TestImeBlocksOptionShortcuts:

    @pytest.mark.parametrize("source,expected", [
        ("com.apple.keylayout.PinyinKeyboard", True),   # 2026-08-27 实测拦截 ⌥⇧O
        ("com.apple.inputmethod.SCIM.ITABC", True),
        ("com.apple.keylayout.Cangjie", True),
        ("com.apple.keylayout.ABC", False),
        ("com.apple.keylayout.US", False),
        (None, False),
        ("", False),
    ])
    def test_sources(self, source, expected):
        assert ime_blocks_option_shortcuts(source) is expected


# ==================== 快照与漂移 ====================

class TestSnapshots:

    def _patch_ime(self, monkeypatch, value):
        monkeypatch.setattr(ima_common, "get_ime_source", lambda: value)

    def test_first_run_no_drift(self, tmp_path, monkeypatch):
        self._patch_ime(monkeypatch, "com.apple.keylayout.ABC")
        _make_chrome_dir(tmp_path, ext_settings={WEB_CLIPPER_EXT_ID: _clipper_ext()})
        drifts, snap = save_snapshot_and_report_drift(
            snapshot_path=tmp_path / "snap.json", chrome_dir=tmp_path)
        assert drifts == []
        assert snap["chrome_profile_name"] == "James"

    def test_profile_drift_detected(self, tmp_path, monkeypatch):
        self._patch_ime(monkeypatch, "com.apple.keylayout.ABC")
        snap_file = tmp_path / "snap.json"
        _make_chrome_dir(tmp_path, ext_settings={WEB_CLIPPER_EXT_ID: _clipper_ext()})
        save_snapshot_and_report_drift(snapshot_path=snap_file, chrome_dir=tmp_path)
        # 模拟换账号：Local State 切回 Default
        (tmp_path / "Local State").write_text(json.dumps({
            "profile": {"last_used": "Default", "info_cache": {
                "Default": {"name": "您的 Chrome", "user_name": "a@gmail.com"}}},
        }, ensure_ascii=False))
        drifts, _ = save_snapshot_and_report_drift(
            snapshot_path=snap_file, chrome_dir=tmp_path)
        assert any("chrome_profile_dir" in d for d in drifts)
        assert any("'Profile 1' → 'Default'" in d for d in drifts)

    def test_ime_drift_detected(self, tmp_path, monkeypatch):
        snap_file = tmp_path / "snap.json"
        _make_chrome_dir(tmp_path, ext_settings={WEB_CLIPPER_EXT_ID: _clipper_ext()})
        self._patch_ime(monkeypatch, "com.apple.keylayout.ABC")
        save_snapshot_and_report_drift(snapshot_path=snap_file, chrome_dir=tmp_path)
        self._patch_ime(monkeypatch, "com.apple.keylayout.PinyinKeyboard")
        drifts, _ = save_snapshot_and_report_drift(
            snapshot_path=snap_file, chrome_dir=tmp_path)
        assert any("ime_source" in d for d in drifts)

    def test_time_change_alone_is_not_drift(self):
        snap = {"time": "2026-08-01T00:00:00", "chrome_profile_dir": "Profile 1",
                "chrome_profile_name": "James", "chrome_profile_user": "b@gmail.com",
                "clipper_installed": True, "clipper_enabled": True,
                "clipper_version": "1.7.1", "ime_source": "X"}
        new = dict(snap, time="2026-08-27T09:00:00")
        assert diff_snapshots(snap, new) == []
        assert diff_snapshots({}, new) == []


# ==================== saver：预检 / 熔断 / 失败签名 ====================

class TestPreflightClipperEnv:
    """preflight_clipper_env 用隔离 chrome_dir 验证 fail-closed 判定。"""

    def _patch_ime(self, monkeypatch, value):
        monkeypatch.setattr("ima_obsidian_saver.get_ime_source", lambda: value)

    def _patch_daemon(self, monkeypatch, ok=True):
        # daemon 探测走真实 pgrep，测试内 mock 保持封闭
        monkeypatch.setattr("ima_obsidian_saver._ensure_daemon_for_receipt",
                            lambda: ok)

    def test_all_good_clipper_mode(self, tmp_path, monkeypatch, capsys):
        from ima_obsidian_saver import preflight_clipper_env
        _make_chrome_dir(tmp_path, ext_settings={WEB_CLIPPER_EXT_ID: _clipper_ext()})
        self._patch_ime(monkeypatch, "com.apple.keylayout.PinyinKeyboard")
        self._patch_daemon(monkeypatch)
        # clipper 模式 + 中文输入法：⌘⇧O 不受影响，应通过
        assert preflight_clipper_env("Google Chrome", "clipper", chrome_dir=tmp_path) is True
        out = capsys.readouterr().out
        assert "预检" in out

    def test_daemon_down_fails_closed(self, tmp_path, monkeypatch, capsys):
        from ima_obsidian_saver import preflight_clipper_env
        _make_chrome_dir(tmp_path, ext_settings={WEB_CLIPPER_EXT_ID: _clipper_ext()})
        self._patch_ime(monkeypatch, "com.apple.keylayout.ABC")
        self._patch_daemon(monkeypatch, ok=False)
        assert preflight_clipper_env("Google Chrome", "clipper", chrome_dir=tmp_path) is False
        assert "daemon" in capsys.readouterr().out

    def test_quick_mode_skips_daemon_check(self, tmp_path, monkeypatch):
        # quick 模式不走弹窗回执，daemon 状态不应影响判定（不调用探测）
        from ima_obsidian_saver import preflight_clipper_env
        calls = []
        monkeypatch.setattr("ima_obsidian_saver._ensure_daemon_for_receipt",
                            lambda: calls.append(1) or False)
        _make_chrome_dir(tmp_path, ext_settings={WEB_CLIPPER_EXT_ID: _clipper_ext()})
        self._patch_ime(monkeypatch, "com.apple.keylayout.ABC")
        assert preflight_clipper_env("Google Chrome", "quick", chrome_dir=tmp_path) is True
        assert calls == []

    def test_not_installed_fails_closed(self, tmp_path, monkeypatch, capsys):
        from ima_obsidian_saver import preflight_clipper_env
        _make_chrome_dir(tmp_path, ext_settings={})  # 激活 Profile 无扩展
        self._patch_ime(monkeypatch, "com.apple.keylayout.ABC")
        self._patch_daemon(monkeypatch)
        assert preflight_clipper_env("Google Chrome", "clipper", chrome_dir=tmp_path) is False
        assert "未安装" in capsys.readouterr().out

    def test_quick_mode_with_cjk_ime_fails_closed(self, tmp_path, monkeypatch, capsys):
        from ima_obsidian_saver import preflight_clipper_env
        _make_chrome_dir(tmp_path, ext_settings={WEB_CLIPPER_EXT_ID: _clipper_ext()})
        self._patch_ime(monkeypatch, "com.apple.keylayout.PinyinKeyboard")
        assert preflight_clipper_env("Google Chrome", "quick", chrome_dir=tmp_path) is False
        assert "clipper" in capsys.readouterr().out  # 提示改用 --mode clipper

    def test_missing_shortcut_binding_fails(self, tmp_path, monkeypatch, capsys):
        from ima_obsidian_saver import preflight_clipper_env
        _make_chrome_dir(tmp_path, ext_settings={
            WEB_CLIPPER_EXT_ID: _clipper_ext(commands={}),  # 无任何绑定
        })
        self._patch_ime(monkeypatch, "com.apple.keylayout.ABC")
        self._patch_daemon(monkeypatch)
        assert preflight_clipper_env("Google Chrome", "clipper", chrome_dir=tmp_path) is False
        assert "快捷键" in capsys.readouterr().out

    def test_rebound_key_mismatch_fails(self, tmp_path, monkeypatch, capsys):
        # 键位被改绑（如改成 Ctrl+K）：绑定非空但与 saver 发送的键位不符，
        # 预检必须拦下（否则每篇 popup_missing，靠熔断止损当天批次已烧掉）
        from ima_obsidian_saver import preflight_clipper_env
        _make_chrome_dir(tmp_path, ext_settings={
            WEB_CLIPPER_EXT_ID: _clipper_ext(commands={
                "_execute_action": {"suggested_key": "Ctrl+K"},
            }),
        })
        self._patch_ime(monkeypatch, "com.apple.keylayout.ABC")
        self._patch_daemon(monkeypatch)
        assert preflight_clipper_env("Google Chrome", "clipper", chrome_dir=tmp_path) is False
        out = capsys.readouterr().out
        assert "Ctrl+K" in out and "Command+Shift+O" in out

    def test_unreadable_profile_warns_but_passes(self, tmp_path, monkeypatch, capsys):
        from ima_obsidian_saver import preflight_clipper_env
        empty = tmp_path / "nothing"
        self._patch_ime(monkeypatch, "com.apple.keylayout.ABC")
        self._patch_daemon(monkeypatch)
        assert preflight_clipper_env("Google Chrome", "clipper", chrome_dir=empty) is True
        assert "跳过扩展预检" in capsys.readouterr().out


class TestConsecutiveFailureBreaker:

    def test_aborts_after_threshold_same_signature(self):
        from ima_obsidian_saver import ConsecutiveFailureBreaker
        b = ConsecutiveFailureBreaker(threshold=3)
        assert b.record_failure("popup_missing") is False
        assert b.record_failure("popup_missing") is False
        assert b.record_failure("popup_missing") is True   # 第 3 次熔断

    def test_different_signature_resets_count(self):
        from ima_obsidian_saver import ConsecutiveFailureBreaker
        b = ConsecutiveFailureBreaker(threshold=3)
        b.record_failure("popup_missing")
        b.record_failure("popup_missing")
        assert b.record_failure("file_not_found") is False  # 换签名重新计
        assert b.record_failure("file_not_found") is False
        assert b.record_failure("file_not_found") is True

    def test_success_resets(self):
        from ima_obsidian_saver import ConsecutiveFailureBreaker
        b = ConsecutiveFailureBreaker(threshold=2)
        assert b.record_failure("popup_missing") is False
        b.record_success()
        assert b.record_failure("popup_missing") is False  # 被成功重置，不熔断


class TestFailureSignature:
    """save_one_article 失败路径必须写 _LAST_FAILURE_SIGNATURE（主循环熔断依据）。

    浏览器/网络交互链全部 mock（conftest「subprocess 默认 mock」约定），
    否则会真实请求 example.com、真实打开 Chrome 标签页（曾贡献 ~22.5s 耗时）。
    """

    def _base_mocks(self):
        from datetime import datetime
        from unittest.mock import patch
        return [
            patch("ima_obsidian_saver.extract_publish_date",
                  return_value=datetime.now().strftime("%y%m%d")),
            patch("ima_obsidian_saver.extract_publish_date_js", return_value=None),
            patch("ima_obsidian_saver.open_url"),
            patch("ima_obsidian_saver.handle_verify_page", return_value=False),
            patch("ima_obsidian_saver.read_page_snapshot",
                  return_value={"title": "t", "text": "正文"}),
            patch("ima_obsidian_saver.activate_browser"),
            patch("ima_obsidian_saver.get_frontmost_app", return_value="Chrome"),
            patch("ima_obsidian_saver.time.sleep"),
            patch("ima_obsidian_saver.close_tab"),
        ]

    def _setup_vault(self, tmp_path, monkeypatch, sv):
        vault = tmp_path / "Vault"
        (vault / "Clippings").mkdir(parents=True)
        monkeypatch.setattr(sv, "VAULT_DIR", vault)
        monkeypatch.setattr(sv, "CLIPPINGS_DIR", vault / "Clippings")
        monkeypatch.setattr(sv, "WAIT_CLIP_TOTAL", 0)  # 轮询立即到期，防回归空等

    def test_file_not_found_signature(self, tmp_path, monkeypatch):
        import ima_obsidian_saver as sv
        from contextlib import ExitStack
        from unittest.mock import patch
        self._setup_vault(tmp_path, monkeypatch, sv)
        article = {"id": 1, "url": "https://example.com/x", "title": "t", "kb": "AI"}
        cfg = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}
        with ExitStack() as stack:
            for p in self._base_mocks():
                stack.enter_context(p)
            with patch("ima_obsidian_saver.trigger_quick_clip"):
                status, _ = sv.save_one_article(article, cfg)
        assert status == "failed"
        assert sv._LAST_FAILURE_SIGNATURE == "file_not_found"

    def test_popup_missing_signature_fast_fail(self, tmp_path, monkeypatch):
        import ima_obsidian_saver as sv
        from contextlib import ExitStack
        from unittest.mock import patch
        self._setup_vault(tmp_path, monkeypatch, sv)
        article = {"id": 1, "url": "https://example.com/x", "title": "t", "kb": "AI"}
        cfg = {"app": "Google Chrome", "shortcut_mods": ["option", "shift"]}
        with ExitStack() as stack:
            for p in self._base_mocks():
                stack.enter_context(p)
            with patch("ima_obsidian_saver.trigger_clipper_with_receipt",
                       return_value=False):
                status, _ = sv.save_one_article(article, cfg, mode="clipper")
        assert status == "failed"
        assert sv._LAST_FAILURE_SIGNATURE == "popup_missing"


class TestAtomicSnapshotWrite:
    """快照必须原子落盘：中断不能留下半截文件（否则漂移基线被静默重置）。"""

    def test_no_tmp_leftover_and_valid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ima_common, "get_ime_source",
                            lambda: "com.apple.keylayout.ABC")
        _make_chrome_dir(tmp_path, ext_settings={WEB_CLIPPER_EXT_ID: _clipper_ext()})
        snap_file = tmp_path / "snap.json"
        save_snapshot_and_report_drift(snapshot_path=snap_file, chrome_dir=tmp_path)
        assert not (tmp_path / "snap.tmp").exists()  # with_suffix(".tmp")
        data = json.loads(snap_file.read_text())     # 落盘内容完整可解析
        assert data["chrome_profile_name"] == "James"


class TestChromeWindowHelpers:
    """list_windows 解析的防御性：脏条目（缺 window_id/pid）不得让回执路径崩溃。"""

    def _patch_windows(self, monkeypatch, windows):
        monkeypatch.setattr(
            "ima_obsidian_saver.run_cua",
            lambda *a, **kw: json.dumps({"windows": windows}),
        )

    def test_dirty_entries_skipped(self, monkeypatch):
        import ima_obsidian_saver as sv
        self._patch_windows(monkeypatch, [
            {"app_name": "Google Chrome", "window_id": 1, "pid": 10,
             "bounds": {"width": 1280, "height": 706}},
            {"app_name": "Google Chrome", "pid": 11},                # 缺 window_id
            {"app_name": "Google Chrome", "window_id": 3},           # 缺 pid
            {"app_name": "Finder", "window_id": 4, "pid": 12},       # 非 Chrome
        ])
        wins = sv._chrome_windows()
        assert [w["window_id"] for w in wins] == [1]
        # 弹窗搜索不因脏条目崩溃，且能找到基线外的新窄窗口
        popup = sv._find_clipper_popup({1})
        assert popup is None

    def test_popup_found_by_width_heuristic(self, monkeypatch):
        import ima_obsidian_saver as sv
        self._patch_windows(monkeypatch, [
            {"app_name": "Google Chrome", "window_id": 1, "pid": 10,
             "bounds": {"width": 1280, "height": 706}},
            {"app_name": "Google Chrome", "window_id": 2, "pid": 10,
             "bounds": {"width": 364, "height": 554}},   # 剪藏器弹窗（实测尺寸）
        ])
        popup = sv._find_clipper_popup({1})
        assert popup["window_id"] == 2

    def test_ax_add_button_case_insensitive_match(self, monkeypatch):
        # 标签按 ADD_BUTTON_LABELS 小写子串匹配——大小写变化不影响
        import ima_obsidian_saver as sv
        calls = []

        def fake_cua(tool, params, timeout=15):
            calls.append(tool)
            if tool == "get_window_state":
                return {"elements": [
                    {"label": "Settings", "role": "AXLink", "element_index": 3},
                    {"label": "ADD TO OBSIDIAN", "role": "AXButton", "element_index": 7},
                ]}
            return {}  # click

        monkeypatch.setattr(sv, "_cua_call", fake_cua)
        assert sv._ax_press_add_button({"pid": 66952, "window_id": 1388}) is True
        assert calls == ["get_window_state", "click"]
        assert calls and calls[1] == "click"

    def test_ax_add_button_missing_ids_returns_false(self):
        import ima_obsidian_saver as sv
        assert sv._ax_press_add_button({"pid": None, "window_id": None}) is False
