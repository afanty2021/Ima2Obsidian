"""F3: run_cua_call 必须正确区分'空输出成功'与'失败'"""
from unittest.mock import patch, MagicMock

import pytest

import ima_ax_extractor


def _patch_run_cua(stdout: str = "", returncode: int = 0, raises: Exception = None):
    """构造一个 mock run_cua：可控制 stdout / returncode / 抛异常"""
    if raises:
        return patch("ima_ax_extractor.run_cua", side_effect=raises)
    if returncode != 0:
        return patch("ima_ax_extractor.run_cua", side_effect=RuntimeError(f"exit {returncode}"))
    return patch("ima_ax_extractor.run_cua", return_value=stdout)


class TestRunCuaCall:

    def test_json_output_parsed(self):
        """JSON 输出应被解析为 dict"""
        with _patch_run_cua('{"ok": true}'):
            result = ima_ax_extractor.run_cua_call("get_window_state", {"x": 1})
        assert result == {"ok": True}

    def test_plain_text_output_wrapped(self):
        """非 JSON 文本（如 click 返回）应包装成 {"raw": ...}"""
        with _patch_run_cua("clicked"):
            result = ima_ax_extractor.run_cua_call("click", {"x": 1})
        assert result == {"raw": "clicked"}

    def test_empty_output_treated_as_success(self):
        """空 stdout + exit 0 应视为成功（返回 dict），而非 None

        cua-driver 的 click/scroll 等命令在成功时常返回空 stdout。
        旧实现返回 None，导致 click_element 误判失败、触发不必要的 re-fetch。
        """
        with _patch_run_cua(""):
            result = ima_ax_extractor.run_cua_call("click", {"x": 1})
        assert result is not None, "空输出不应返回 None"
        assert isinstance(result, dict)

    def test_whitespace_only_output_treated_as_success(self):
        """仅含空白的 stdout 也应视为成功"""
        with _patch_run_cua("   \n  "):
            result = ima_ax_extractor.run_cua_call("scroll", {"x": 1})
        assert result is not None

    def test_runtime_error_returns_none(self):
        """run_cua 抛 RuntimeError（非零退出）应返回 None"""
        with _patch_run_cua(returncode=1):
            result = ima_ax_extractor.run_cua_call("click", {"x": 1})
        assert result is None


class TestClickElement:

    def test_click_empty_stdout_is_success(self):
        """click_element 在 cua-driver 返回空 stdout 时不应误判失败"""
        with patch("ima_ax_extractor.run_cua", return_value=""):
            assert ima_ax_extractor.click_element(pid=1, window_id=1, element_index=5) is True

    def test_click_failure_when_run_cua_raises(self):
        with patch("ima_ax_extractor.run_cua", side_effect=RuntimeError("exit 1")):
            assert ima_ax_extractor.click_element(pid=1, window_id=1, element_index=5) is False


class TestScrollDown:

    def test_scroll_does_not_raise_on_empty_output(self):
        """scroll_down 在空 stdout 上不应抛异常"""
        with patch("ima_ax_extractor.run_cua", return_value=""):
            ima_ax_extractor.scroll_down(pid=1, window_id=1, amount=3)  # 不抛即通过
