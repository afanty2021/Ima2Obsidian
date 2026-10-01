"""cua-driver 0.31 token-only 迁移（上游 #3873）的双模调用面。

上游 0.31 起三件事变了：click/scroll 只认 element_token（element_index 被
dispatch 硬拒绝「unknown argument」）；单次 call 不带 session 标签返回即
退役快照（读写共用标签 token 才跨调用存活）；像素 scroll 要求同 session
先读过窗。本项目对策（ima_common 收口）：
① 所有 call 经 cua_call 注入进程级 session 标签；
② 每次 get_window_state 解析后登记该窗口快照的 index→token 映射；
③ 元素动作 token 优先、index 回退——驱动单独回滚到 0.8 时代码不用回滚。
"""
import json
from unittest.mock import patch

import ima_common
import ima_obsidian_saver as sv
from ima_common import (
    CUA_SESSION, cua_call, cua_click, cua_element_action,
    element_token_for, remember_window_elements,
)


def _last_params(mock):
    """取 mock 最近一次 run_cua 调用的 JSON 参数（args = ["call", tool, json]）。"""
    args = mock.call_args[0][0]
    return json.loads(args[2])


# ==================== cua_call：session 注入 ====================

class TestCuaCallSession:
    def test_session_injected(self):
        with patch("ima_common.run_cua", return_value="") as mock:
            cua_call("get_window_state", {"pid": 1, "window_id": 2})
        args = mock.call_args[0][0]
        assert args[:2] == ["call", "get_window_state"]
        assert json.loads(args[2])["session"] == CUA_SESSION

    def test_explicit_session_not_overridden(self):
        """调用方自管 session 时注入让路（setdefault 语义）。"""
        with patch("ima_common.run_cua", return_value="") as mock:
            cua_call("click", {"pid": 1, "session": "custom-label"})
        assert json.loads(mock.call_args[0][0][2])["session"] == "custom-label"

    def test_timeout_forwarded(self):
        with patch("ima_common.run_cua", return_value="") as mock:
            cua_call("get_window_state", {"pid": 1}, timeout=7)
        assert mock.call_args.kwargs.get("timeout") == 7


# ==================== index→token 快照缓存 ====================

class TestTokenCache:
    def test_remember_and_lookup(self):
        state = {"elements": [
            {"element_index": 7, "element_token": "tok-7"},
            {"element_index": 8},  # 0.8 的 state 无 token 字段
        ]}
        remember_window_elements(100, 200, state)
        assert element_token_for(100, 200, 7) == "tok-7"
        assert element_token_for(100, 200, 8) is None
        assert element_token_for(100, 999, 7) is None  # 别的窗口
        assert element_token_for(100, 200, 42) is None  # 无该元素

    def test_new_read_replaces_mapping(self):
        """新快照取代旧快照——旧 token 全部失效，映射须整体覆盖非合并。"""
        remember_window_elements(1, 2, {"elements": [
            {"element_index": 5, "element_token": "old-5"},
            {"element_index": 6, "element_token": "old-6"},
        ]})
        remember_window_elements(1, 2, {"elements": [
            {"element_index": 5, "element_token": "new-5"},
        ]})
        assert element_token_for(1, 2, 5) == "new-5"
        assert element_token_for(1, 2, 6) is None  # 旧映射残留会拿 stale token

    def test_none_state_clears(self):
        remember_window_elements(1, 2, {"elements": [
            {"element_index": 5, "element_token": "t"}]})
        remember_window_elements(1, 2, None)  # 读取失败路径
        assert element_token_for(1, 2, 5) is None


# ==================== 元素动作双模：token 优先 / index 回退 ====================

class TestElementActionDualMode:
    def test_token_path_param_shape(self):
        """token 路径只发 pid + element_token（0.31 语义：token 自带窗口定位）。"""
        with patch("ima_common.run_cua", return_value="") as mock:
            cua_element_action("click", 10, 20,
                               element_index=9, element_token="tk")
        p = _last_params(mock)
        assert p == {"pid": 10, "element_token": "tk", "session": CUA_SESSION}
        assert mock.call_count == 1

    def test_token_failure_falls_back_to_index(self):
        """token 调用失败（旧驱动不识 token / 偶发 stale）→ 回退 index 形状。"""
        responses = iter([RuntimeError("unknown argument element_token"), ""])

        def fake_run_cua(*a, **k):
            r = next(responses)
            if isinstance(r, Exception):
                raise r
            return r

        with patch("ima_common.run_cua", side_effect=fake_run_cua) as mock:
            out = cua_element_action("click", 10, 20,
                                     element_index=9, element_token="tk")
        assert out == ""
        assert mock.call_count == 2
        p = _last_params(mock)
        assert p["pid"] == 10 and p["window_id"] == 20 and p["element_index"] == 9

    def test_no_token_goes_straight_to_index(self):
        """0.8 的 state 无 token → 不浪费一次注定失败的调用，直接 index。"""
        with patch("ima_common.run_cua", return_value="") as mock:
            cua_element_action("scroll", 10, 20, element_index=3,
                               extra={"direction": "down", "amount": 3})
        assert mock.call_count == 1
        p = _last_params(mock)
        assert p["element_index"] == 3 and p["direction"] == "down"

    def test_both_missing_raises(self):
        with patch("ima_common.run_cua") as mock:
            try:
                cua_element_action("click", 10, 20)
            except RuntimeError as e:
                assert "element_token" in str(e)
            else:
                raise AssertionError("应抛 RuntimeError")
        mock.assert_not_called()

    def test_token_failure_without_index_reraises(self):
        with patch("ima_common.run_cua",
                   side_effect=RuntimeError("stale_element_token")):
            try:
                cua_element_action("click", 10, 20, element_token="tk")
            except RuntimeError:
                pass
            else:
                raise AssertionError("无 index 可回退时应重抛")


class TestCuaClick:
    def test_click_uses_cached_token(self):
        remember_window_elements(10, 20, {"elements": [
            {"element_index": 9, "element_token": "tok-9"}]})
        with patch("ima_common.run_cua", return_value="") as mock:
            cua_click(10, 20, 9)
        p = _last_params(mock)
        assert p.get("element_token") == "tok-9"
        assert "element_index" not in p


# ==================== 提取器：读窗登记 + 点击换 token ====================

class TestExtractorIntegration:
    def test_get_window_state_registers_tokens(self):
        import ima_ax_extractor as ext
        state = {"tree_markdown": "- [7] AXButton \"打开\"",
                 "elements": [{"element_index": 7, "element_token": "t7",
                               "role": "AXButton", "label": "打开"}]}
        with patch("ima_common.run_cua",
                   return_value=json.dumps(state, ensure_ascii=False)):
            got = ext.get_window_state(10, 20)
        assert got["tree_markdown"].startswith("- [7]")
        assert element_token_for(10, 20, 7) == "t7"

    def test_click_element_prefers_token(self):
        import ima_ax_extractor as ext
        remember_window_elements(10, 20, {"elements": [
            {"element_index": 9, "element_token": "tok-9"}]})
        with patch("ima_common.run_cua", return_value="") as mock:
            assert ext.click_element(10, 20, 9) is True
        p = _last_params(mock)
        assert p.get("element_token") == "tok-9"

    def test_click_element_failure_returns_false(self):
        import ima_ax_extractor as ext
        with patch("ima_common.run_cua", side_effect=RuntimeError("boom")):
            assert ext.click_element(10, 20, 9) is False


# ==================== saver：弹窗按钮双模点击 ====================

class TestSaverDualModeClick:
    def _popup(self):
        return {"pid": 1, "window_id": 2}

    def test_token_preferred(self, monkeypatch):
        calls = []

        def fake_cua(tool, params, timeout=15):
            calls.append((tool, params))
            if tool == "get_window_state":
                return {"elements": [
                    {"label": "Add to Obsidian", "role": "AXButton",
                     "element_index": 7, "element_token": "tok-7"}]}
            return {"ok": True}

        monkeypatch.setattr(sv, "WAIT_AX_BUTTONS", 1.0)
        monkeypatch.setattr(sv.time, "sleep", lambda s: None)
        monkeypatch.setattr(sv, "_cua_call", fake_cua)
        assert sv._ax_press_add_button(self._popup()) is True
        tool, params = calls[-1]
        assert tool == "click"
        assert params == {"pid": 1, "element_token": "tok-7"}  # 无 index/window_id

    def test_no_token_falls_back_to_index(self, monkeypatch):
        calls = []

        def fake_cua(tool, params, timeout=15):
            calls.append((tool, params))
            if tool == "get_window_state":
                return {"elements": [
                    {"label": "add to obsidian", "role": "AXButton",
                     "element_index": 7}]}  # 0.8 形状：无 token
            return {"ok": True}

        monkeypatch.setattr(sv, "WAIT_AX_BUTTONS", 1.0)
        monkeypatch.setattr(sv.time, "sleep", lambda s: None)
        monkeypatch.setattr(sv, "_cua_call", fake_cua)
        assert sv._ax_press_add_button(self._popup()) is True
        tool, params = calls[-1]
        assert params == {"pid": 1, "window_id": 2, "element_index": 7}

    def test_token_click_fails_then_index_succeeds(self, monkeypatch):
        """token 路失败（_cua_call 返 None）→ index 兜底成功——驱动回滚场景。"""
        calls = []

        def fake_cua(tool, params, timeout=15):
            calls.append((tool, params))
            if tool == "get_window_state":
                return {"elements": [
                    {"label": "Add to Obsidian", "role": "AXButton",
                     "element_index": 7, "element_token": "tok-7"}]}
            if params.get("element_token"):
                return None  # 旧驱动不识 token → 失败
            return {"ok": True}  # index 路成功

        monkeypatch.setattr(sv, "WAIT_AX_BUTTONS", 1.0)
        monkeypatch.setattr(sv.time, "sleep", lambda s: None)
        monkeypatch.setattr(sv, "_cua_call", fake_cua)
        assert sv._ax_press_add_button(self._popup()) is True
        assert calls[-1][1].get("element_index") == 7

    def test_cua_call_injects_session_for_saver(self):
        """saver 的 _cua_call 经 cua_call 走——session 注入不被绕过。"""
        with patch("ima_common.run_cua", return_value="") as mock:
            sv._cua_call("get_window_state", {"pid": 1, "window_id": 2})
        assert json.loads(mock.call_args[0][0][2])["session"] == CUA_SESSION


# ==================== 纯树模式：去屏幕录制 TCC 依赖 ====================

class TestTreeOnlyReads:
    """0.31 起截图走屏幕录制授权（换 App 身份会失效）；本项目从不消费截图，
    全部读窗显式关截图，把 TCC 依赖收窄到辅助功能一项。"""

    def test_extractor_state_read_is_tree_only(self):
        import ima_ax_extractor as ext
        with patch("ima_common.run_cua", return_value="{}") as mock:
            ext.get_window_state(1, 2)
        assert json.loads(mock.call_args[0][0][2])["include_screenshot"] is False

    def test_common_article_tab_probe_is_tree_only(self):
        with patch("ima_common.run_cua", return_value="{}") as mock:
            ima_common._is_article_tab_window({"pid": 1, "window_id": 2})
        assert json.loads(mock.call_args[0][0][2])["include_screenshot"] is False

    def test_update_tree_only_constant(self):
        import ima_incremental_update as upd
        assert upd.TREE_ONLY == {"include_screenshot": False}

    def test_daemon_start_bypasses_permissions_gate(self, monkeypatch):
        """start_daemon 注入门旁路环境——无人值守不得挂在 TCC 交互门上等点击。"""
        import ima_incremental_update as upd
        captured = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured.update(cmd=cmd, env=kwargs.get("env"))

        monkeypatch.setattr(upd.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(upd.time, "sleep", lambda s: None)
        monkeypatch.setattr(upd, "is_daemon_running", lambda: True)
        monkeypatch.setattr(ima_common, "run_cua", lambda *a, **k: "{}")
        assert upd.start_daemon() is True
        assert captured["env"]["CUA_DRIVER_RS_PERMISSIONS_GATE"] == "0"
        assert captured["cmd"] == [upd.CUA_DRIVER, "serve"]
