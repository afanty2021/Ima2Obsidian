"""--human-in-loop 人在回路接线契约（9/28）。

通道污染期产物：以反射/源码 token 断言接线完整——签名参数、argparse 旗标、
人工阈值、冷却门与 KB 循环旁路、update 透传。不 mock 运行时流程（流程级
行为由当日实跑、真人在场拖滑块验收）；后续通道恢复再补流程用例。
"""
import inspect

import ima_incremental_update as upd
import ima_obsidian_saver as sv


def test_saver_signature_accepts_human_window():
    sig = inspect.signature(sv.save_one_article)
    assert sig.parameters["human_verify_window"].default == 0.0


def test_saver_argparse_flag_wired():
    src = inspect.getsource(sv)
    assert '"--human-in-loop"' in src
    assert "args.human_in_loop" in src


def test_saver_wait_invoked_only_when_window_positive():
    src = inspect.getsource(sv)
    assert "is_verify_page(snap) and human_verify_window > 0" in src


def test_saver_wall_threshold_widened_in_human_mode():
    src = inspect.getsource(sv)
    assert ("VERIFY_WALL_ABORT_THRESHOLD_HUMAN if args.human_in_loop"
            in src)


def test_saver_cooldown_gate_bypassed_when_human_present():
    src = inspect.getsource(sv)
    assert "not args.dry_run and not args.human_in_loop" in src


def test_updater_threads_flag_and_bypasses_kb_cooldown():
    src = inspect.getsource(upd)
    assert '"--human-in-loop"' in src
    assert "human_in_loop=args.human_in_loop" in src
    assert "and not args.human_in_loop:" in src


def test_notify_uses_osascript_and_escapes_quotes():
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)

    orig = sv.subprocess.run
    sv.subprocess.run = fake_run
    try:
        sv._notify_human('副标题"引号', '正文"引号\\反斜杠')
    finally:
        sv.subprocess.run = orig
    assert len(calls) == 1
    assert calls[0][0] == "osascript"
    script = calls[0][2]
    assert '\\"' in script  # 引号已转义
    assert "\\\\" in script  # 反斜杠已转义


def test_notify_swallows_all_exceptions():
    def boom(*_a, **_k):
        raise RuntimeError("osascript missing")

    orig = sv.subprocess.run
    sv.subprocess.run = boom
    try:
        sv._notify_human("t", "m")  # 不抛即过
    finally:
        sv.subprocess.run = orig


def test_saver_pings_notification_on_slider_and_timeout():
    src = inspect.getsource(sv)
    assert src.count("_notify_human(") >= 3  # def + 出现时 + 超时
    assert "display notification" in src
