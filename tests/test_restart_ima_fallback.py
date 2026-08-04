"""navigate_to_kb 所有 attempts 失败后强制 restart_ima 兜底（launchd GUI session 隔离自愈）。

根因（systematic-debugging 2026-07-29 11:50 跑）：
- launchd 进程不在用户 GUI session 中，cua-driver bring_to_front（NSRunningApplication.activate）
  不生效，窗口未拉到前台 → AX 探测读到 0 元素 → 5 个知识库导航全失败。
- 旧代码所有 attempts 失败后直接 return False，无自愈。

修复：所有 attempts 失败后强制 restart_ima + 递归一次重试。
- 防无限递归：递归调用传 allow_restart=False（code review #5：解耦 max_attempts 双重含义）
- restart_ima 异常 fail-soft 仍 return False（保留原契约）

本测试覆盖 4 项 code review 修复：
- #1: 宽泛 try/except 把递归异常误归因为 restart 失败 → restart 单独 try + 递归在外
- #3: 多 KB 复合重启 → 模块级 _RESTARTED_IN_THIS_RUN 标志，单次 run 最多 restart 1 次
- #4: restart_ima 返回值被忽略 → if not restart_ima(): return False
- #5: max_attempts 双重含义 → 独立 allow_restart 参数解耦
"""
import json
from unittest.mock import patch

import pytest

import ima_incremental_update


@pytest.fixture(autouse=True)
def _reset_restarted_flag():
    """每个用例前 reset 模块级 _RESTARTED_IN_THIS_RUN（修复 #3 配套）"""
    ima_incremental_update._RESTARTED_IN_THIS_RUN = False
    yield
    ima_incremental_update._RESTARTED_IN_THIS_RUN = False


def _win(is_on_screen=True, y=33):
    """构造一个在屏幕内的窗口（避免触发前置 is_on_screen/y<-50 兜底）"""
    return {"pid": 1, "window_id": 1, "is_on_screen": is_on_screen,
            "bounds": {"x": 0, "y": y, "width": 1512, "height": 885}}


def _empty_md_json():
    """cua-driver 0 元素响应（AX Tree 不完整）"""
    return '{"tree_markdown":""}'


def _good_md_json():
    """cua-driver 完整 AX Tree 响应：包含目标 KB 'AI' 入口（element 2）

    AXStaticText 数 >= 5（通过完整性校验），且第 3 行完全匹配 'AI'。
    """
    md = (
        '[0] AXWindow "AI - ima.copilot"\n'
        '[1] AXScrollArea\n'
        '[2] AXStaticText = "AI"\n'
        '[3] AXStaticText = "侧边栏其他内容 1"\n'
        '[4] AXStaticText = "侧边栏其他内容 2"\n'
        '[5] AXStaticText = "侧边栏其他内容 3"\n'
        '[6] AXStaticText = "侧边栏其他内容 4"\n'
        '[7] AXStaticText = "侧边栏其他内容 5"\n'
    )
    return json.dumps({"tree_markdown": md})


def _rendered_md_without_target_kb_json():
    """cua-driver 完整 AX Tree 响应：渲染正常但无目标 KB 'AI' 入口（element_index 缺失）

    AXStaticText 数 = 6（≥5 通过完整性校验），但所有 AXStaticText 都不含 'AI'。
    模拟场景：KB 名错 / IMA UI 改版 / 用户未创建 'AI' 知识库。
    用于验证「窗口渲染正常时 restart_ima 不被触发」——修复 #2 核心断言。
    """
    md = (
        '[0] AXWindow "某知识库 - ima.copilot"\n'
        '[1] AXScrollArea\n'
        '[2] AXStaticText = "知识库列表"\n'
        '[3] AXStaticText = "通用知识库"\n'
        '[4] AXStaticText = "技术笔记"\n'
        '[5] AXStaticText = "产品文档"\n'
        '[6] AXStaticText = "学习资料"\n'
        '[7] AXStaticText = "项目记录"\n'
    )
    return json.dumps({"tree_markdown": md})


def test_all_attempts_fail_triggers_restart_ima_fallback():
    """5 次 attempts 全失败（AX Tree 0 元素）→ 触发 restart_ima 兜底自愈

    复现 launchd GUI session 隔离场景：cua-driver 一直返回 0 元素 tree_markdown，
    现有逻辑只能走完 max_attempts 然后 return False。修复后应强制 restart_ima 一次。
    """
    with patch("ima_incremental_update.get_ima_main_window",
               return_value=_win()), \
         patch("ima_incremental_update.restart_ima", return_value=True) as mock_restart, \
         patch("ima_incremental_update.run_cua", return_value=_empty_md_json()), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        result = ima_incremental_update.navigate_to_kb("AI", max_attempts=5)
    # 兜底触发：restart_ima 被调用恰好 1 次
    mock_restart.assert_called_once()
    # 递归层 allow_restart=False，AX Tree 仍 0 元素 → 最终 return False
    assert result is False


def test_fallback_recursion_does_not_re_trigger_restart():
    """兜底递归调用 navigate_to_kb(allow_restart=False) 防止无限循环

    关键守卫：allow_restart=False 不再触发兜底。递归层即使再次全失败也不会再触发
    restart_ima（避免 launchd 持续后台时无限重启）。

    code review #5：旧版用 max_attempts=1 作为递归守卫，与「循环次数」语义耦合；
    新版独立 allow_restart 参数解耦——递归层仍走完整 max_attempts 次循环提高成功率，
    但不允许再次 restart。
    """
    with patch("ima_incremental_update.get_ima_main_window",
               return_value=_win()), \
         patch("ima_incremental_update.restart_ima", return_value=True) as mock_restart, \
         patch("ima_incremental_update.run_cua", return_value=_empty_md_json()), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        ima_incremental_update.navigate_to_kb("AI", max_attempts=5)
    # 严格断言：只调 1 次（外层兜底），递归层 allow_restart=False 不再触发
    mock_restart.assert_called_once()


def test_fallback_recursion_succeeds_returns_true():
    """兜底重试时 AX Tree 恢复正常 + 找到 KB → 返回 True（自愈成功）

    场景：restart_ima 后窗口重新渲染，AX Tree 完整、侧边栏能找到 KB 入口。
    验证兜底路径能正常 return True，不被异常路径污染。
    """
    # 外层 5 次 attempts × 2 次读（首次 + retry）= 10 次 0 元素响应；
    # 递归层 allow_restart=False，max_attempts=5：首次读 good_md（≥5 元素通过 + 命中 KB）→ 1 次 click
    cua_responses = [_empty_md_json()] * 10 + [_good_md_json(), '{"clicked": true}']

    with patch("ima_incremental_update.get_ima_main_window",
               return_value=_win()), \
         patch("ima_incremental_update.restart_ima", return_value=True) as mock_restart, \
         patch("ima_incremental_update.run_cua", side_effect=cua_responses), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"), \
         patch("ima_incremental_update.get_ax_window_title",
               return_value="AI - ima.copilot"), \
         patch("ima_incremental_update.is_on_kb_list", return_value=True):
        result = ima_incremental_update.navigate_to_kb("AI", max_attempts=5)
    assert result is True
    mock_restart.assert_called_once()


def test_fallback_restart_ima_exception_returns_false():
    """restart_ima 抛异常时被捕获，fail-soft 仍 return False（保留原契约）

    code review #1：restart_ima 单独 try/except——异常不再吞掉后续递归调用。
    """
    with patch("ima_incremental_update.get_ima_main_window",
               return_value=_win()), \
         patch("ima_incremental_update.restart_ima",
               side_effect=RuntimeError("quit timeout")), \
         patch("ima_incremental_update.run_cua", return_value=_empty_md_json()), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        result = ima_incremental_update.navigate_to_kb("AI", max_attempts=5)
    assert result is False


def test_allow_restart_false_does_not_trigger_fallback():
    """allow_restart=False（递归调用场景）失败后不再触发 restart_ima（守卫正确）

    防无限递归的关键：allow_restart=False 时即使全失败也直接 return False，
    不进入兜底分支。这保证递归层不会再递归。code review #5：替代旧版 max_attempts=1 守卫。
    """
    with patch("ima_incremental_update.get_ima_main_window",
               return_value=_win()), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_incremental_update.run_cua", return_value=_empty_md_json()), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        result = ima_incremental_update.navigate_to_kb(
            "AI", max_attempts=5, allow_restart=False)
    assert result is False
    # 守卫生效：allow_restart=False 时不能再调 restart_ima
    mock_restart.assert_not_called()


# ==================== 新增测试（code review 4 项修复） ====================


def test_fallback_recursion_exception_not_attributed_to_restart():
    """#1 验证：递归调用内的异常不被误归因为 'restart_ima 兜底失败'

    旧版宽泛 try/except 同时包了 restart_ima() 和递归 navigate_to_kb()——递归内
    run_cua/json.loads 抛异常会被误归因为「restart_ima 兜底失败」（实际 restart 已成功）。
    新版 restart_ima 单独 try，递归在 try 外——异常直接传播，不被误归因。

    场景：restart_ima 成功返回 True（已自愈），但递归层第 1 次 run_cua 抛 RuntimeError
    （模拟 cua-driver 网络错误/json 解析失败）。
    """
    # 外层 5 attempts × 2 次 = 10 次 0 元素响应；第 11 次调用（递归层首次 get_window_state）抛异常
    cua_responses = [_empty_md_json()] * 10 + [RuntimeError("递归层 run_cua 网络异常")]

    raised = None
    try:
        with patch("ima_incremental_update.get_ima_main_window",
                   return_value=_win()), \
             patch("ima_incremental_update.restart_ima", return_value=True) as mock_restart, \
             patch("ima_incremental_update.run_cua", side_effect=cua_responses), \
             patch("ima_incremental_update.subprocess.run"), \
             patch("ima_incremental_update.time.sleep"):
            ima_incremental_update.navigate_to_kb("AI", max_attempts=5)
    except RuntimeError as e:
        raised = e

    # 断言 1（核心）：异常从 navigate_to_kb 传播出来（不被 except 误捕为 restart 失败）
    assert raised is not None, "递归层异常应直接传播，不被 except 误捕"
    assert "递归层" in str(raised)
    # 断言 2：restart_ima 已成功调用（返回 True，未抛异常）——证明异常来自递归而非 restart
    mock_restart.assert_called_once()


def test_fallback_returns_false_when_restart_returns_false():
    """#4 验证：restart_ima 返回 False（IMA 30s 内未启动）时 navigate_to_kb 不递归，直接 return False

    旧版忽略 restart_ima() 返回值——launch 失败仍递归，叠加无意义 ~60s 等待。
    新版检查返回值：launch 失败直接 return False。
    """
    with patch("ima_incremental_update.get_ima_main_window",
               return_value=_win()), \
         patch("ima_incremental_update.restart_ima", return_value=False) as mock_restart, \
         patch("ima_incremental_update.run_cua", return_value=_empty_md_json()) as mock_cua, \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep") as mock_sleep:
        result = ima_incremental_update.navigate_to_kb("AI", max_attempts=5)
    # launch 失败 → 直接 return False，不递归
    assert result is False
    mock_restart.assert_called_once()
    # 不递归的关键证据：run_cua 没有再被调用（外层已消费 10 次，递归层不再进入）
    # 旧版会再调至少 1 次 get_window_state —— 这里严格断言总调用次数 == 外层 10 次
    assert mock_cua.call_count == 10


def test_fallback_only_once_per_run():
    """#3 验证：单次 run 内最多 restart 一次——第二次 navigate_to_kb 不再 restart

    场景：main() 循环处理多个 KB，第一个 KB 触发 restart 后 _RESTARTED_IN_THIS_RUN=True，
    后续 KB 即使 5 attempts 全失败也不再 restart（避免 9.5min 复合重启浪费）。
    """
    # 模拟 main 循环：连续两次 navigate_to_kb（每次 5 attempts × 2 次 = 10 次 run_cua）
    with patch("ima_incremental_update.get_ima_main_window", return_value=_win()), \
         patch("ima_incremental_update.restart_ima", return_value=True) as mock_restart, \
         patch("ima_incremental_update.run_cua", return_value=_empty_md_json()), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        # 第一次调用：5 attempts 失败 → 触发 restart → 递归 allow_restart=False 也失败 → False
        result1 = ima_incremental_update.navigate_to_kb("AI", max_attempts=5)
        # 第二次调用：_RESTARTED_IN_THIS_RUN 已 True，不再 restart
        result2 = ima_incremental_update.navigate_to_kb("KB2", max_attempts=5)

    assert result1 is False
    assert result2 is False
    # 关键断言：跨两次 navigate_to_kb，restart_ima 只被调用 1 次（第一次触发，第二次被标志阻止）
    mock_restart.assert_called_once()


# ==================== 修复 #2：兜底条件精确化（窗口未渲染才 restart） ====================


def test_fallback_skipped_when_window_rendered_normally():
    """#2 核心验证：窗口渲染正常（AXStaticText ≥ 5）但找不到 KB → 不触发 restart_ima

    复现「KB 名错 / IMA UI 改版 / cua-driver bug」场景：cua-driver 返回完整 AX Tree
    （≥5 个元素，含知识库列表等），但找不到目标 KB 'AI' 入口。旧版无差别 quit 会丢失
    用户未保存状态；新版按 last_ax_text_count ≥ min_elements 跳过 restart。

    测试矩阵：last_ax_text_count=6 ≥ 5，allow_restart=True，_RESTARTED_IN_THIS_RUN=False
    预期：restart_ima 不调用，返回 False，日志含「窗口渲染正常」
    """
    # log() 只在 tty 下 print，pytest 下用 patch 拦截消息
    log_messages = []
    fake_log = lambda msg, print_too=True: log_messages.append(msg)

    with patch("ima_incremental_update.get_ima_main_window", return_value=_win()), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_incremental_update.run_cua",
               return_value=_rendered_md_without_target_kb_json()) as mock_cua, \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"), \
         patch("ima_incremental_update.log", side_effect=fake_log):
        result = ima_incremental_update.navigate_to_kb("AI", max_attempts=5)

    # 核心断言 1：窗口渲染正常 → restart_ima 不被调用（避免丢失用户未保存状态）
    mock_restart.assert_not_called()
    # 核心断言 2：返回 False（导航失败，但不走 restart 兜底）
    assert result is False
    # 核心断言 3：日志含「窗口渲染正常」区分消息（便于运维定位根因）
    combined = "\n".join(log_messages)
    assert "窗口渲染正常" in combined, f"日志应含「窗口渲染正常」，实际: {combined}"
    # 辅助断言：attempts 循环确实跑了 5 次（每次读 md + scroll）= 10 次 run_cua
    # 验证 mock 数据未被误用——若未跑完 attempts 则 last_ax_text_count 不可信
    assert mock_cua.call_count == 10, f"5 次 attempts × (读 md + scroll) 应=10 次，实际: {mock_cua.call_count}"


def test_fallback_triggered_when_window_not_rendered():
    """#2 对照验证：窗口未渲染（AXStaticText < 5）→ 触发 restart_ima

    与 test_fallback_skipped_when_window_rendered_normally 形成对照：相同 allow_restart
    和 _RESTARTED_IN_THIS_RUN，差异仅在 last_ax_text_count（< 5 vs ≥ 5）。验证阈值判断
    的正确性——只有窗口真的未渲染（疑似 launchd GUI session 隔离）才 restart。

    测试矩阵：last_ax_text_count=0 < 5，allow_restart=True，_RESTARTED_IN_THIS_RUN=False
    预期：restart_ima 调用 1 次，递归 allow_restart=False 后仍失败 → 返回 False
    """
    log_messages = []
    fake_log = lambda msg, print_too=True: log_messages.append(msg)

    with patch("ima_incremental_update.get_ima_main_window",
               return_value=_win()), \
         patch("ima_incremental_update.restart_ima", return_value=True) as mock_restart, \
         patch("ima_incremental_update.run_cua", return_value=_empty_md_json()), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"), \
         patch("ima_incremental_update.log", side_effect=fake_log):
        result = ima_incremental_update.navigate_to_kb("AI", max_attempts=5)

    # 核心断言 1：窗口未渲染 → restart_ima 触发自愈（与上一用例形成对照）
    mock_restart.assert_called_once()
    # 核心断言 2：递归层 allow_restart=False + 仍 0 元素 → 最终 False
    assert result is False
    # 核心断言 3：日志含「窗口未渲染」+ 触发原因（疑似 launchd GUI session 隔离）
    combined = "\n".join(log_messages)
    assert "窗口未渲染" in combined, f"日志应含「窗口未渲染」，实际: {combined}"
    assert "launchd GUI session 隔离" in combined, f"日志应含根因说明，实际: {combined}"
