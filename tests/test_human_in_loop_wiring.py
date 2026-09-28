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
