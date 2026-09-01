"""17:10 兜底槽前置判断（--scheduled）的单元与入口测试

2026-09-01 加的前置判断：16:10 主槽全成功且无待保存残留时，17:10 兜底槽
不再空跑。判定依据是状态文件（write_run_state 落盘）+ 全库残留计数。
约定：拿不准一律照跑（返回 False）——最坏等于现状的一次幂等空跑。

2026-09-01 评审 Important 修复后新增覆盖：
- 提取器「本次失败」参与解析并入知识库失败（部分失败 → 触发补扫）
- 状态文件记 full（--kb 限定的手动运行不构成跳过依据）
- count_unsaved_articles DB 异常返回 None → 门控 fail-open
"""
import json
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import ima_incremental_update as inc
from ima_incremental_update import (
    _parse_extractor_stats,
    count_unsaved_articles,
    scheduled_gate_applies,
    scheduled_gate_decision,
    write_run_state,
)

TODAY = datetime(2026, 9, 1, 17, 10, 0)


def _fake_datetime(hour, minute=10):
    fixed = datetime(2026, 9, 1, hour, minute)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls):
            return fixed

    return FakeDatetime


@pytest.fixture
def fake_now(monkeypatch):
    """把模块内 datetime.now() 固定到 2026-09-01 17:10（兜底槽触发时刻）"""
    monkeypatch.setattr(inc, "datetime", _fake_datetime(17))
    return TODAY


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    p = tmp_path / "last_incremental_run.json"
    monkeypatch.setattr(inc, "RUN_STATE_FILE", p)
    return p


@pytest.fixture
def empty_db(temp_db):
    """有 schema 但无文章的库：残留计数有效返回 0（而非 DB 异常的 None）"""
    from ima_common import init_database
    init_database()
    return temp_db


def _write_state(path, date="2026-09-01", kb_failed=0, save_failed=0, full=True):
    path.write_text(
        json.dumps({"date": date, "kb_failed": kb_failed,
                    "save_failed": save_failed, "full": full,
                    "ts": f"{date}T16:31:04"}),
        encoding="utf-8",
    )


class TestScheduledGateApplies:
    def test_only_after_17(self):
        assert scheduled_gate_applies(datetime(2026, 9, 1, 16, 10)) is False
        assert scheduled_gate_applies(datetime(2026, 9, 1, 17, 0)) is True
        assert scheduled_gate_applies(datetime(2026, 9, 1, 23, 59)) is True

    def test_early_morning_not_applied(self):
        # launchd 补跑跨天触发的凌晨场景：不算兜底槽，照常全量跑
        assert scheduled_gate_applies(datetime(2026, 9, 2, 0, 30)) is False


class TestParseExtractorStats:
    """「本次失败」必须被解析——逐篇提取失败不入库，残留计数看不见它"""

    def test_full_success_sample(self):
        out = "\n".join([
            "  ✅ 已导航到 AI 知识库（列表页验证通过）",
            "    本次新增: 4 篇",
            "    本次跳过: 2 篇",
            "    本次失败: 0 篇",
            "    数据库总计: 3215 篇 (6 个知识库)",
        ])
        assert _parse_extractor_stats(out) == {"new": 4, "skipped": 2, "failed": 0}

    def test_partial_failure_sample(self):
        out = "\n".join([
            "    本次新增: 3 篇",
            "    本次跳过: 1 篇",
            "    本次失败: 2 篇",
        ])
        assert _parse_extractor_stats(out) == {"new": 3, "skipped": 1, "failed": 2}

    def test_malformed_lines_default_zero(self):
        out = "本次新增: abc 篇\n本次失败:\n"
        assert _parse_extractor_stats(out) == {"new": 0, "skipped": 0, "failed": 0}

    def test_repeated_tags_last_wins(self):
        out = "本次新增: 1 篇\n本次新增: 5 篇\n"
        assert _parse_extractor_stats(out)["new"] == 5

    def test_saver_lines_not_confused(self):
        # saver 输出（「处理完成 / 本次失败」前后文）不含「本次新增」时不会误报新增
        out = "  [1/4]   提取日期...\n  本次失败: 1 篇\n"
        assert _parse_extractor_stats(out) == {"new": 0, "skipped": 0, "failed": 1}


class TestCountUnsavedAllKbScope:
    def test_none_means_all_kbs(self, seeded_db):
        # 种子：AI 未保存 1 + Invest 未保存 1（failed/非微信不算）
        assert count_unsaved_articles() == 2
        assert count_unsaved_articles("AI") == 1
        assert count_unsaved_articles("Invest") == 1

    def test_empty_schema_db_zero(self, empty_db):
        # 有 schema 的空库：残留计数有效为 0（区别于 DB 异常的 None）
        assert count_unsaved_articles() == 0

    def test_db_error_returns_none_not_zero(self, temp_db, monkeypatch):
        # DB 读不出来 ≠ 确认为 0：必须返回 None 让门控 fail-open
        import sqlite3
        from unittest.mock import patch

        with patch("sqlite3.connect",
                   side_effect=sqlite3.OperationalError("database is locked")):
            assert count_unsaved_articles() is None


class TestRunStateRoundtrip:
    def test_write_then_read(self, fake_now, state_file):
        write_run_state(2, 1, full=True)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["date"] == "2026-09-01"
        assert state["kb_failed"] == 2
        assert state["save_failed"] == 1
        assert state["full"] is True
        assert inc.read_last_run_state() == state

    def test_partial_scan_recorded_as_not_full(self, fake_now, state_file):
        write_run_state(0, 0, full=False)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["full"] is False

    def test_read_missing_returns_none(self, state_file):
        assert inc.read_last_run_state() is None

    def test_read_corrupt_returns_none(self, state_file):
        state_file.write_text("{not json", encoding="utf-8")
        assert inc.read_last_run_state() is None


class TestScheduledGateDecision:
    """决策矩阵：只有「无残留 + 今天最近一次全库真实运行 kb_failed==0」才跳过"""

    def test_skip_when_clean_today(self, fake_now, state_file, empty_db):
        _write_state(state_file, date="2026-09-01", kb_failed=0)
        should_skip, reason = scheduled_gate_decision()
        assert should_skip is True
        assert "全成功" in reason

    def test_run_when_residual(self, fake_now, state_file, seeded_db):
        # 即使今天全成功，有残留也要跑（保存重试）
        _write_state(state_file, date="2026-09-01", kb_failed=0)
        should_skip, reason = scheduled_gate_decision()
        assert should_skip is False
        assert "残留" in reason

    def test_run_when_kb_failed_today(self, fake_now, state_file, empty_db):
        _write_state(state_file, date="2026-09-01", kb_failed=5)
        should_skip, reason = scheduled_gate_decision()
        assert should_skip is False
        assert "知识库失败" in reason

    def test_run_when_last_scan_not_full(self, fake_now, state_file, empty_db):
        # Important #2：手动 --kb 限定的运行只扫了部分 KB，其「全成功」不构成跳过依据
        _write_state(state_file, date="2026-09-01", kb_failed=0, full=False)
        should_skip, reason = scheduled_gate_decision()
        assert should_skip is False
        assert "全库扫描" in reason

    def test_run_when_no_run_today(self, fake_now, state_file, empty_db):
        # 昨天全成功 ≠ 今天扫过（16:10 没跑/中断的场景），保守照跑
        _write_state(state_file, date="2026-08-31", kb_failed=0)
        should_skip, reason = scheduled_gate_decision()
        assert should_skip is False
        assert "还没有真实运行" in reason

    def test_run_when_no_state_file(self, fake_now, state_file, empty_db):
        should_skip, _ = scheduled_gate_decision()
        assert should_skip is False

    def test_run_when_residual_read_fails(self, fake_now, state_file, empty_db,
                                          monkeypatch):
        # Important #3：DB 异常返回 None → fail-open，绝不当成「无残留」跳过
        monkeypatch.setattr(inc, "count_unsaved_articles", lambda kb=None: None)
        _write_state(state_file, date="2026-09-01", kb_failed=0)
        should_skip, reason = scheduled_gate_decision()
        assert should_skip is False
        assert "DB 异常" in reason

    @pytest.mark.parametrize("bad_state", [
        "{not json",
        json.dumps({"date": "2026-09-01", "save_failed": 0, "full": True}),   # 缺 kb_failed
        json.dumps({"date": "2026-09-01", "kb_failed": "0", "full": True}),   # 类型错误
        json.dumps({"date": "2026-09-01", "kb_failed": True, "full": True}),  # bool 冒充 int
        json.dumps({"date": "2026-09-01", "kb_failed": -1, "full": True}),    # 负数
    ])
    def test_run_when_state_abnormal(self, fake_now, state_file, empty_db, bad_state):
        state_file.write_text(bad_state, encoding="utf-8")
        should_skip, reason = scheduled_gate_decision()
        assert should_skip is False


class TestMainScheduledGate:
    """入口级：gate 短路时不碰任何 IMA 副作用；落穿时正常跑完并写状态文件"""

    def _patch_common(self, monkeypatch, tmp_path):
        monkeypatch.setattr(inc, "LOCK_FILE", tmp_path / "l.lock")
        monkeypatch.setattr(inc, "LOG_FILE", tmp_path / "l.log")
        # 落穿用例会真跑 KB 间隔等待，mock 掉避免拖慢套件
        monkeypatch.setattr(inc.time, "sleep", lambda s: None)

    @pytest.fixture
    def happy_extractor(self, monkeypatch):
        """mock 掉全部 IMA 副作用：daemon/漂移快照/caffeinate/KB 更新"""
        monkeypatch.setattr(inc, "ensure_daemon", lambda: True)
        monkeypatch.setattr(inc, "save_snapshot_and_report_drift",
                            lambda: ({}, {"chrome_profile_name": "James",
                                          "clipper_version": "1.7.1",
                                          "ime_source": "com.apple.keylayout.ABC"}))
        monkeypatch.setattr(inc.subprocess, "Popen", MagicMock())
        return monkeypatch.setattr(
            inc, "update_knowledge_base",
            lambda kb, dry_run: {"new": 0, "skipped": 0, "failed": 0})

    def test_main_skips_clean_at_17(self, empty_db, tmp_path, monkeypatch,
                                    fake_now, state_file):
        _write_state(state_file, date="2026-09-01", kb_failed=0)
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["ima_incremental_update.py", "--scheduled"])
        sentinel = MagicMock(
            side_effect=AssertionError("gate 应短路，不应进入 KB 处理"))
        monkeypatch.setattr(inc, "update_knowledge_base", sentinel)

        inc.main()  # 正常返回 = 退出码 0
        sentinel.assert_not_called()

    def test_main_falls_through_when_kb_failed_and_writes_full_state(
            self, empty_db, tmp_path, monkeypatch, fake_now, state_file,
            happy_extractor):
        _write_state(state_file, date="2026-09-01", kb_failed=5)
        self._patch_common(monkeypatch, tmp_path)
        # 不带 --kb：全库扫描 → 状态 full=True
        monkeypatch.setattr(sys, "argv",
                            ["ima_incremental_update.py", "--scheduled"])

        inc.main()

        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["kb_failed"] == 0
        assert state["date"] == "2026-09-01"
        assert state["full"] is True

    def test_main_before_17_runs_full_even_when_clean(
            self, empty_db, tmp_path, monkeypatch, state_file, happy_extractor):
        # 评审 Minor：17 点前 --scheduled 不做前置判断（16:10 主槽永远全量跑）
        monkeypatch.setattr(inc, "datetime", _fake_datetime(16))
        _write_state(state_file, date="2026-09-01", kb_failed=0)
        self._patch_common(monkeypatch, tmp_path)
        recorder = MagicMock(return_value={"new": 0, "skipped": 0, "failed": 0})
        monkeypatch.setattr(inc, "update_knowledge_base", recorder)

        monkeypatch.setattr(sys, "argv",
                            ["ima_incremental_update.py", "--scheduled"])

        inc.main()
        assert recorder.call_count == len(inc.KNOWLEDGE_BASES)

    def test_main_dry_run_does_not_write_state(
            self, temp_db, tmp_path, monkeypatch, fake_now, state_file):
        # dry-run 不代表真实扫描，绝不能写状态文件（否则污染次日门控）
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["ima_incremental_update.py", "--dry-run", "--kb", "AI"])
        monkeypatch.setattr(inc, "update_knowledge_base",
                            lambda kb, dry_run: {"new": 0, "skipped": 0, "failed": 0})

        inc.main()

        assert not state_file.exists()

    def test_main_partial_scan_records_not_full(
            self, empty_db, tmp_path, monkeypatch, fake_now, state_file,
            happy_extractor):
        # --kb 限定的真实运行落盘 full=False
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["ima_incremental_update.py", "--kb", "AI"])

        inc.main()

        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["full"] is False

    def test_main_extraction_failure_counts_as_kb_failed(
            self, empty_db, tmp_path, monkeypatch, fake_now, state_file):
        # Important #1 链路：提取器部分失败 → stats.failed>0 → 状态 kb_failed>0
        # → 17:10 门控必须照跑
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["ima_incremental_update.py", "--scheduled", "--kb", "AI"])
        monkeypatch.setattr(inc, "ensure_daemon", lambda: True)
        monkeypatch.setattr(inc, "save_snapshot_and_report_drift",
                            lambda: ({}, {"chrome_profile_name": "James",
                                          "clipper_version": "1.7.1",
                                          "ime_source": "com.apple.keylayout.ABC"}))
        monkeypatch.setattr(inc.subprocess, "Popen", MagicMock())
        monkeypatch.setattr(inc, "update_knowledge_base",
                            lambda kb, dry_run: {"new": 3, "skipped": 0, "failed": 2})
        # new=3 会触发保存阶段，mock 掉真实 saver
        monkeypatch.setattr(inc, "save_to_obsidian",
                            lambda kb, dry_run=False, run_reclaim=True:
                            {"saved": 3, "failed": 0, "started": True})

        with pytest.raises(SystemExit) as exc_info:
            inc.main()

        assert exc_info.value.code == 1  # 有失败 → 非零退出告警
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["kb_failed"] == 2  # 而不是被吞掉的 0
