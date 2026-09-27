"""保存节流（2026-09-27 滑块墙事故后续治理）。

背景：一次补 3 天积压时 Chrome 连开 ~29 个文章页触发微信滑块风控墙（32 次验证页、
16 存 3、剪藏垃圾 12 个）。三层节流：
① saver 每轮上限 MAX_SAVE_PER_RUN（--limit 只能收窄不能放大）；
② saver 验证墙熔断（verify_page_stuck 达阈值即停 + 写跨运行冷却到 saver_state）；
③ update 保存预算跨库共享递减 + 每库保存前查冷却（墙按账号计，换库开页同样喂风控）。

冷却时长 45 分钟的设计意图：16:10 撞墙熔断后，17:10 兜底槽（约 55 分钟后）仍能
探测一次；再撞再熔断再冷却，每天探测代价有界（每次 ~2 篇尝试即止损）。
"""
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import ima_common
import ima_incremental_update as upd
import ima_obsidian_saver as sv
from ima_common import (
    SAVE_WALL_COOLDOWN_KEY, init_database,
    get_save_wall_cooldown_remaining, set_save_wall_cooldown,
)


# ==================== ima_common：冷却状态机 ====================

class TestSaveWallCooldown:
    def test_set_then_remaining_positive_and_bounded(self, temp_db):
        init_database()
        assert set_save_wall_cooldown(45) is True
        remaining = get_save_wall_cooldown_remaining()
        assert 0 < remaining <= 45 * 60

    def test_expiry_returns_zero(self, temp_db):
        """注入 now：45 分钟冷却在 +46 分钟已过期、+44 分钟仍在"""
        init_database()
        t0 = datetime(2026, 9, 27, 13, 0, 0)
        assert set_save_wall_cooldown(45, now=t0) is True
        assert get_save_wall_cooldown_remaining(now=t0 + timedelta(minutes=44)) > 0
        assert get_save_wall_cooldown_remaining(now=t0 + timedelta(minutes=46)) == 0

    def test_no_state_returns_zero(self, temp_db):
        init_database()
        assert get_save_wall_cooldown_remaining() == 0

    def test_garbage_value_returns_zero(self, temp_db):
        """状态值损坏 → fail-open 按无冷却（冷却只是节流，不该阻断保存）"""
        init_database()
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO saver_state (key, value) VALUES (?, ?)",
                (SAVE_WALL_COOLDOWN_KEY, "not-a-timestamp"),
            )
            conn.commit()
        assert get_save_wall_cooldown_remaining() == 0

    def test_overwrite_replaces_expiry(self, temp_db):
        init_database()
        t0 = datetime(2026, 9, 27, 13, 0, 0)
        set_save_wall_cooldown(45, now=t0)
        set_save_wall_cooldown(1, now=t0)
        remaining = get_save_wall_cooldown_remaining(now=t0)
        assert 0 < remaining <= 60


# ==================== saver main：节流与熔断 ====================

def _fake_article(i):
    return {"id": i, "url": f"https://mp.weixin.qq.com/s?__biz=T&i={i}",
            "title": f"文章{i}", "kb": "AI"}


@pytest.fixture
def saver_env(temp_db, tmp_path, monkeypatch):
    """saver main() 实跑（真实模式）所需的最小封闭环境。"""
    init_database()
    vault = tmp_path / "Vault"
    vault.mkdir()
    monkeypatch.setattr(sv, "VAULT_DIR", vault)
    monkeypatch.setattr(sv, "CLIPPINGS_DIR", vault / "Clippings")
    monkeypatch.setattr(sv, "ensure_appnap_disabled", lambda: True)
    monkeypatch.setattr(sv, "ensure_chrome_js_enabled", lambda *_a: True)
    monkeypatch.setattr(sv, "WAIT_BETWEEN", 0)
    monkeypatch.setattr("sys.argv", [
        "ima_obsidian_saver.py", "--skip-preflight", "--skip-reclaim",
    ])
    return monkeypatch


class TestSaverCooldownSkip:
    def test_cooldown_active_skips_and_exits_zero(self, saver_env, capsys):
        """冷却中：exit 0、不取文章（零开页）、输出说明冷却"""
        set_save_wall_cooldown(45)
        with patch.object(sv, "get_unsaved_articles") as mock_fetch, \
             patch.object(sv, "get_stats",
                          return_value={"total": 1, "saved": 0, "unsaved": 1, "deleted": 0}):
            with pytest.raises(SystemExit) as ei:
                sv.main()
        assert ei.value.code == 0
        mock_fetch.assert_not_called()
        assert "冷却" in capsys.readouterr().out

    def test_no_cooldown_proceeds(self, saver_env):
        """无冷却：正常取文章进入保存循环"""
        with patch.object(sv, "get_unsaved_articles", return_value=[_fake_article(1)]), \
             patch.object(sv, "get_stats", return_value={"total": 1, "saved": 0, "unsaved": 1, "deleted": 0}), \
             patch.object(sv, "save_one_article",
                          return_value=("saved", "260927")) as mock_save, \
             patch.object(sv, "mark_saved"):
            sv.main()
        assert mock_save.call_count == 1


class TestSaverPerRunCap:
    def test_backlog_capped_to_max_per_run(self, saver_env, capsys):
        """积压 20 篇 + --limit 1300：只处理 MAX_SAVE_PER_RUN 篇，输出节流说明"""
        articles = [_fake_article(i) for i in range(20)]

        def fake_fetch(limit, kb=None):
            assert limit == sv.MAX_SAVE_PER_RUN, "--limit 应被上限收窄"
            return articles[:limit]

        with patch.object(sv, "get_unsaved_articles", side_effect=fake_fetch), \
             patch.object(sv, "get_stats", return_value={"total": 20, "saved": 0, "unsaved": 20, "deleted": 0}), \
             patch.object(sv, "save_one_article",
                          return_value=("saved", "260927")) as mock_save, \
             patch.object(sv, "mark_saved"):
            sv.main()
        assert mock_save.call_count == sv.MAX_SAVE_PER_RUN
        assert "节流上限" in capsys.readouterr().out

    def test_lower_limit_narrows_further(self, saver_env, monkeypatch):
        """--limit 5 < 上限 12：按 5 处理（--limit 只能收窄）"""
        monkeypatch.setattr("sys.argv", [
            "ima_obsidian_saver.py", "--skip-preflight", "--skip-reclaim",
            "--limit", "5",
        ])
        with patch.object(sv, "get_unsaved_articles",
                          side_effect=lambda limit, kb=None: [_fake_article(i) for i in range(limit)]), \
             patch.object(sv, "get_stats", return_value={"total": 20, "saved": 0, "unsaved": 20, "deleted": 0}), \
             patch.object(sv, "save_one_article",
                          return_value=("saved", "260927")) as mock_save, \
             patch.object(sv, "mark_saved"):
            sv.main()
        assert mock_save.call_count == 5


class TestSaverWallAbort:
    def test_wall_hits_abort_and_write_cooldown(self, saver_env, capsys):
        """verify_page_stuck 连续 2 次即熔断：不再处理第 3 篇、写冷却、非零退出"""
        articles = [_fake_article(i) for i in range(5)]

        def fake_save_one(*_a, **_k):
            sv._LAST_FAILURE_SIGNATURE = "verify_page_stuck"
            return ("failed", None)

        with patch.object(sv, "get_unsaved_articles", return_value=articles), \
             patch.object(sv, "get_stats", return_value={"total": 5, "saved": 0, "unsaved": 5, "deleted": 0}), \
             patch.object(sv, "save_one_article", side_effect=fake_save_one) as mock_save:
            with pytest.raises(SystemExit) as ei:
                sv.main()
        assert ei.value.code == 1  # 全失败退出码
        assert mock_save.call_count == sv.VERIFY_WALL_ABORT_THRESHOLD  # 第 3 篇不再开页
        assert get_save_wall_cooldown_remaining() > 0  # 冷却已落库
        assert "风控墙活跃" in capsys.readouterr().out

    def test_other_signature_uses_generic_breaker_no_cooldown(self, saver_env):
        """非墙失败走通用熔断（3 次），不写冷却——冷却专属验证墙"""
        articles = [_fake_article(i) for i in range(5)]

        def fake_save_one(*_a, **_k):
            sv._LAST_FAILURE_SIGNATURE = "file_not_found"
            return ("failed", None)

        with patch.object(sv, "get_unsaved_articles", return_value=articles), \
             patch.object(sv, "get_stats", return_value={"total": 5, "saved": 0, "unsaved": 5, "deleted": 0}), \
             patch.object(sv, "save_one_article", side_effect=fake_save_one) as mock_save:
            with pytest.raises(SystemExit):
                sv.main()
        assert mock_save.call_count == sv.CONSECUTIVE_FAIL_ABORT
        assert get_save_wall_cooldown_remaining() == 0

    def test_wall_hit_then_success_no_accumulate(self, saver_env):
        """墙命中 1 次后成功 1 次：计数不累计到熔断（与通用熔断同口径）"""
        articles = [_fake_article(i) for i in range(4)]

        def fake2(*_a, **_k):
            idx = fake2.i = getattr(fake2, "i", 0) + 1
            sv._LAST_FAILURE_SIGNATURE = "verify_page_stuck" if idx in (1, 3) else "file_not_found"
            return ("failed", None) if idx in (1, 3) else ("saved", "260927")

        with patch.object(sv, "get_unsaved_articles", return_value=articles), \
             patch.object(sv, "get_stats", return_value={"total": 4, "saved": 2, "unsaved": 4, "deleted": 0}), \
             patch.object(sv, "save_one_article", side_effect=fake2) as mock_save, \
             patch.object(sv, "mark_saved"):
            with pytest.raises(SystemExit) as ei:
                sv.main()  # 部分失败契约 exit 2
        assert ei.value.code == 2
        assert mock_save.call_count == 4  # 交错不熔断，4 篇全尝试
        assert get_save_wall_cooldown_remaining() == 0


# ==================== update：保存预算与冷却跳过 ====================

@pytest.fixture
def upd_env(temp_db, tmp_path, monkeypatch):
    """update main() 跑 KB 循环所需的最小封闭环境（模仿 test_incremental_guard）。"""
    monkeypatch.setattr(upd, "LOCK_FILE", tmp_path / "l.lock")
    monkeypatch.setattr(upd, "LOG_FILE", tmp_path / "l.log")
    monkeypatch.setattr(upd, "WAIT_BETWEEN_KB", 0)
    init_database()
    monkeypatch.setattr(upd, "ensure_daemon", lambda: True)
    monkeypatch.setattr(upd, "save_snapshot_and_report_drift",
                        lambda *a, **kw: ([], {}))
    monkeypatch.setattr(
        upd, "update_knowledge_base",
        lambda _kb, _dry_run: {"new": 1, "skipped": 0, "failed": 0},
    )
    monkeypatch.setattr(
        "ima_incremental_update.subprocess.Popen",
        lambda *args, **kwargs: type("Caffeinate", (), {"terminate": lambda self: None})(),
    )
    return monkeypatch


class TestUpdateSaveBudget:
    def test_budget_decrements_across_kbs(self, upd_env):
        """预算跨库递减：12 → 12-(1+0)=11 → 10（按实际尝试页数扣）"""
        upd_env.setattr("sys.argv", ["ima_incremental_update.py", "--kb", "AI", "Invest", "英语教与学"])
        budgets = []

        def fake_save(kb_name, dry_run=False, run_reclaim=True, save_budget=None):
            budgets.append(save_budget)
            return {"saved": 1, "failed": 0, "started": True}

        upd_env.setattr(upd, "save_to_obsidian", fake_save)
        upd.main()
        assert budgets == [upd.SAVE_BUDGET_PER_UPDATE,
                           upd.SAVE_BUDGET_PER_UPDATE - 1,
                           upd.SAVE_BUDGET_PER_UPDATE - 2]

    def test_budget_exhaustion_skips_remaining_kbs(self, upd_env):
        """预算用尽后剩余库跳过保存（提取照常），输出说明"""
        upd_env.setattr("sys.argv", ["ima_incremental_update.py", "--kb", "AI", "Invest"])

        def fake_save(kb_name, dry_run=False, run_reclaim=True, save_budget=None):
            return {"saved": upd.SAVE_BUDGET_PER_UPDATE, "failed": 0, "started": True}

        upd_env.setattr(upd, "save_to_obsidian", fake_save)
        upd.main()
        assert "保存预算已用尽" in upd.LOG_FILE.read_text(encoding="utf-8")

    def test_cooldown_active_skips_all_saves(self, upd_env):
        """冷却中：所有库的保存阶段跳过，saver 不被调用"""
        upd_env.setattr("sys.argv", ["ima_incremental_update.py", "--kb", "AI", "Invest"])
        set_save_wall_cooldown(45)
        with patch.object(upd, "save_to_obsidian") as mock_save:
            upd.main()
        mock_save.assert_not_called()
        assert "风控冷却中" in upd.LOG_FILE.read_text(encoding="utf-8")

    def test_midrun_wall_writes_cooldown_next_kb_skipped(self, upd_env):
        """库 1 保存中途撞墙熔断（fake 内写冷却）→ 库 2 保存被跳过——墙跨库传播"""
        upd_env.setattr("sys.argv", ["ima_incremental_update.py", "--kb", "AI", "Invest"])
        calls = []

        def fake_save(kb_name, dry_run=False, run_reclaim=True, save_budget=None):
            calls.append(kb_name)
            if kb_name == "AI":
                set_save_wall_cooldown(45)  # 模拟 saver 子进程熔断写冷却
            return {"saved": 0, "failed": 1, "started": True}

        upd_env.setattr(upd, "save_to_obsidian", fake_save)
        try:
            upd.main()  # failed=1 → exit 1
        except SystemExit as e:
            assert e.code == 1
        assert calls == ["AI"]  # Invest 的保存被冷却跳过


class TestSaveToObsidianPassesLimit:
    def test_budget_passed_as_limit(self, temp_db, tmp_path, monkeypatch):
        """save_to_obsidian 把剩余预算作为 --limit 传给 saver 子进程"""
        monkeypatch.setattr(upd, "ensure_obsidian_ready", lambda: True)

        class FakeStream:
            def readline(self):
                return ""

            def close(self):
                pass

        class FakeProcess:
            stdout = FakeStream()
            stderr = FakeStream()
            returncode = 0

            def wait(self, timeout=None):
                return 0

        commands = []

        def fake_popen(cmd, **_kwargs):
            commands.append(cmd)
            return FakeProcess()

        monkeypatch.setattr("ima_incremental_update.subprocess.Popen", fake_popen)
        upd.save_to_obsidian("AI", run_reclaim=True, save_budget=7)
        cmd = commands[0]
        i = cmd.index("--limit")
        assert cmd[i + 1] == "7"
