"""晚到按钮补点（_reprobe_add_button）回归。

「认知投降」篇 2026-09-29、10-01 连续两轮同方式失败：冷启动首弹 + 重页面，
clipper 弹窗在、AX 按钮晚于 WAIT_AX_BUTTONS(12s) 预算未就绪，回车兜底救不
回、晚到的按钮没人点（vault 无文件、真失败非迟到落盘）。修复 = 预算放宽到
30s + 落盘轮询失败后补点一次并重给一轮完整轮询。
"""
import inspect

import ima_obsidian_saver as saver


class TestLateButtonRescue:
    def test_wait_ax_buttons_gives_cold_start_heavy_page_30s(self):
        # 12s 实测不封顶首弹慢就绪（连续两轮同篇未就绪）；就绪弹窗首探即中，
        # 预算只在按钮真缺席时消耗，放宽无热路径成本
        assert saver.WAIT_AX_BUTTONS == 30.0

    def test_reprobe_returns_false_without_popup(self, monkeypatch):
        monkeypatch.setattr(saver, "_LAST_CLIPPER_POPUP", None)
        called = []
        monkeypatch.setattr(saver, "_ax_press_add_button",
                            lambda p: called.append(p) or True)
        assert saver._reprobe_add_button() is False
        assert called == []  # 无在案弹窗不得乱点（quick/非 Chrome/触发失败路径）

    def test_reprobe_delegates_to_ax_press_with_recorded_popup(self, monkeypatch):
        popup = {"pid": 7, "window_id": 9}
        monkeypatch.setattr(saver, "_LAST_CLIPPER_POPUP", popup)
        got = {}

        def fake_press(p):
            got["p"] = p
            return True

        monkeypatch.setattr(saver, "_ax_press_add_button", fake_press)
        assert saver._reprobe_add_button() is True
        assert got["p"] is popup  # 必须用在案的同一个弹窗句柄

    def test_trigger_resets_then_records_last_popup(self):
        # 触发前重置（防陈旧句柄跨篇复用）+ 弹窗出现即在案
        src = inspect.getsource(saver.trigger_clipper_with_receipt)
        assert "_LAST_CLIPPER_POPUP = None" in src
        assert "_LAST_CLIPPER_POPUP = popup" in src
        assert src.index("_LAST_CLIPPER_POPUP = None") < \
            src.index("_LAST_CLIPPER_POPUP = popup")

    def test_save_one_article_rescues_before_declaring_failure(self):
        # 补点必须插在「未找到文件」宣告之前——晚点就等于没补
        src = inspect.getsource(saver.save_one_article)
        assert "_reprobe_add_button()" in src
        assert src.index("_reprobe_add_button()") < src.index('"file_not_found"')

    def test_rescue_reuses_full_poll_budget(self):
        # 补点后是完整一轮 WAIT_CLIP_TOTAL 轮询，不是一次性探测
        src = inspect.getsource(saver.save_one_article)
        rescue = src[src.index("_reprobe_add_button()"):]
        assert "WAIT_CLIP_TOTAL" in rescue
        assert "find_and_rename_in_vault" in rescue
