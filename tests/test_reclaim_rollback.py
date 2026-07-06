"""#4: reclaim_clippings 必须用 closing 包裹连接，且 rename/UPDATE 失败时回滚"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ima_common import init_database
from reclaim_clippings import main as reclaim_main


def _setup_isolated_vault(tmp_path, db_path):
    """建一个隔离 vault + DB，方便触发各种 reclaim 场景"""
    vault = tmp_path / "Vault"
    vault.mkdir()
    (vault / "AI").mkdir()
    (vault / "Invest").mkdir()
    clip_dir = vault / "Clippings"
    clip_dir.mkdir()

    # 初始化 schema 并插一行未保存文章
    init_database()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO articles (url, title, knowledge_base, status, obsidian_saved) "
        "VALUES (?,?,?,?,?)",
        ("https://mp.weixin.qq.com/s?__biz=T&mid=T&idx=1&sn=T", "测试文章A", "AI", "success", 0),
    )
    conn.commit()
    conn.close()
    return vault, clip_dir


def test_reclaim_closes_connection_in_apply_mode(temp_db, tmp_path, monkeypatch):
    """reclaim --apply 路径必须 close 连接"""
    vault, clip_dir = _setup_isolated_vault(tmp_path, temp_db)
    (clip_dir / "测试文章A.md").write_text("正文\n*2026年1月2日 10:00*\n", encoding="utf-8")

    monkeypatch.setattr("reclaim_clippings.VAULT_DIR", vault)
    monkeypatch.setattr("reclaim_clippings.CLIPPINGS_DIR", clip_dir)
    monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
    monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
    monkeypatch.setattr("sys.argv", ["reclaim_clippings.py", "--apply"])

    # 用 TrackingConnection 监控 close
    from tests.test_db_connections import TrackingConnection
    real_connect = sqlite3.connect
    instances = []

    def tracking_connect(*args, **kwargs):
        real = real_connect(*args, **kwargs)
        wrapper = TrackingConnection(real)
        instances.append(wrapper)
        return wrapper

    with patch.object(sqlite3, "connect", tracking_connect):
        reclaim_main()

    assert len(instances) >= 1, "应当打开过连接"
    for conn in instances:
        assert conn.close_called, "reclaim 未在 --apply 路径关闭连接（fd 泄漏）"


def test_reclaim_closes_connection_when_update_raises(temp_db, tmp_path, monkeypatch):
    """reclaim --apply 在 UPDATE 抛异常时仍必须 close 连接"""
    vault, clip_dir = _setup_isolated_vault(tmp_path, temp_db)
    (clip_dir / "测试文章A.md").write_text("正文\n*2026年1月2日 10:00*\n", encoding="utf-8")

    monkeypatch.setattr("reclaim_clippings.VAULT_DIR", vault)
    monkeypatch.setattr("reclaim_clippings.CLIPPINGS_DIR", clip_dir)
    monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
    monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
    monkeypatch.setattr("sys.argv", ["reclaim_clippings.py", "--apply"])

    from tests.test_db_connections import TrackingConnection
    real_connect = sqlite3.connect
    instances = []

    def faulty_connect(*args, **kwargs):
        real = real_connect(*args, **kwargs)
        wrapper = TrackingConnection(real)
        # 仅 UPDATE 语句抛异常，SELECT 通过
        wrapper.set_failure_on_sql("UPDATE", sqlite3.OperationalError("database is locked"))
        instances.append(wrapper)
        return wrapper

    with patch.object(sqlite3, "connect", faulty_connect):
        try:
            reclaim_main()
        except Exception:
            pass

    for conn in instances:
        assert conn.close_called, "reclaim 在 UPDATE 异常时未 close 连接"


def test_reclaim_restores_file_when_update_fails(temp_db, tmp_path, monkeypatch):
    """rename 成功但 UPDATE 失败时，文件必须回滚到 Clippings（不能丢）

    不变式：UPDATE 失败时
      - 文件必须回到原位（Clippings 目录），不能滞留 KB 文件夹
      - DB 必须未被标记 obsidian_saved=1（避免被误以为已保存而永久漏存）
    """
    vault, clip_dir = _setup_isolated_vault(tmp_path, temp_db)
    original_file = clip_dir / "测试文章A.md"
    original_file.write_text("正文\n*2026年1月2日 10:00*\n", encoding="utf-8")

    monkeypatch.setattr("reclaim_clippings.VAULT_DIR", vault)
    monkeypatch.setattr("reclaim_clippings.CLIPPINGS_DIR", clip_dir)
    monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
    monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
    monkeypatch.setattr("sys.argv", ["reclaim_clippings.py", "--apply"])

    from tests.test_db_connections import TrackingConnection
    real_connect = sqlite3.connect
    instances = []

    def faulty_connect(*args, **kwargs):
        real = real_connect(*args, **kwargs)
        wrapper = TrackingConnection(real)
        # UPDATE 时抛异常，模拟 DB 锁定
        wrapper.set_failure_on_sql("UPDATE", sqlite3.OperationalError("database is locked"))
        instances.append(wrapper)
        return wrapper

    with patch.object(sqlite3, "connect", faulty_connect):
        try:
            reclaim_main()
        except Exception:
            pass

    # 关键不变式：文件必须回到 Clippings（UPDATE 失败时回滚 rename）
    assert original_file.exists(), "UPDATE 失败时文件未被回滚到 Clippings（文件丢失！）"

    # DB 必须未被标记（commit 未发生）
    conn = sqlite3.connect(temp_db)
    c = conn.cursor()
    c.execute("SELECT obsidian_saved FROM articles WHERE title='测试文章A'")
    row = c.fetchone()
    conn.close()
    assert row[0] == 0, "UPDATE 失败时不应标记 obsidian_saved=1"


def test_reclaim_restores_all_files_when_commit_fails(temp_db, tmp_path, monkeypatch):
    """commit 失败时（多文件已 rename + UPDATE 入事务），全部 rename 必须回滚

    场景：rename A → 成功，UPDATE A → 入事务；rename B → 成功，UPDATE B → 入事务；
    commit() → 抛 'database is locked'。SQLite 自动回滚 UPDATE，但 rename 不会自动回滚。
    必须手动把 A 和 B 都移回 Clippings，否则文件滞留 KB 但 DB 仍 unsaved（永久漏存）。
    """
    vault, clip_dir = _setup_isolated_vault(tmp_path, temp_db)
    # 多插一行未保存文章
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO articles (url, title, knowledge_base, status, obsidian_saved) "
        "VALUES (?,?,?,?,?)",
        ("https://mp.weixin.qq.com/s?__biz=T2&mid=T2&idx=1&sn=T2", "测试文章B", "Invest", "success", 0),
    )
    conn.commit()
    conn.close()
    (vault / "Invest").mkdir(exist_ok=True)

    file_a = clip_dir / "测试文章A.md"
    file_a.write_text("正文A\n*2026年1月2日 10:00*\n", encoding="utf-8")
    file_b = clip_dir / "测试文章B.md"
    file_b.write_text("正文B\n*2026年2月3日 10:00*\n", encoding="utf-8")

    monkeypatch.setattr("reclaim_clippings.VAULT_DIR", vault)
    monkeypatch.setattr("reclaim_clippings.CLIPPINGS_DIR", clip_dir)
    monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
    monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
    monkeypatch.setattr("sys.argv", ["reclaim_clippings.py", "--apply"])

    # 用 TrackingConnection 让 conn.commit() 抛异常
    from tests.test_db_connections import TrackingConnection
    real_connect = sqlite3.connect
    instances = []

    def faulty_connect(*args, **kwargs):
        real = real_connect(*args, **kwargs)
        wrapper = TrackingConnection(real)
        wrapper.set_commit_exception(sqlite3.OperationalError("database is locked"))
        instances.append(wrapper)
        return wrapper

    with patch.object(sqlite3, "connect", faulty_connect):
        try:
            reclaim_main()
        except Exception:
            pass

    # 关键不变式：所有 rename 的文件必须回到 Clippings
    assert file_a.exists(), "file_a 未回滚到 Clippings（commit 失败时丢失！）"
    assert file_b.exists(), "file_b 未回滚到 Clippings（commit 失败时丢失！）"

    # KB 文件夹不应有这些文章
    assert not list((vault / "AI").glob("*测试文章A*")), "file_a 滞留 AI 文件夹"
    assert not list((vault / "Invest").glob("*测试文章B*")), "file_b 滞留 Invest 文件夹"

    # DB 未标记（commit 失败）
    conn = sqlite3.connect(temp_db)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM articles WHERE obsidian_saved=1")
    assert c.fetchone()[0] == 0, "commit 失败时不应有任何行被标记"
    conn.close()


def test_reclaim_dead_letter_when_rollback_fails(temp_db, tmp_path, monkeypatch, capsys):
    """rollback rename 也失败时（极端：磁盘满 / 权限丢失），必须 dead-letter 提示

    不变式：无法回滚时必须打印明确错误（含源路径与目标路径），让运维能手动恢复。
    不能静默吞掉——否则文件位置不可知，且 DB 未标记，下次 reclaim 仍会跳过。
    """
    vault, clip_dir = _setup_isolated_vault(tmp_path, temp_db)
    original_file = clip_dir / "测试文章A.md"
    original_file.write_text("正文\n*2026年1月2日 10:00*\n", encoding="utf-8")

    monkeypatch.setattr("reclaim_clippings.VAULT_DIR", vault)
    monkeypatch.setattr("reclaim_clippings.CLIPPINGS_DIR", clip_dir)
    monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
    monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
    monkeypatch.setattr("sys.argv", ["reclaim_clippings.py", "--apply"])

    # 让 UPDATE 抛异常 → 触发回滚；同时让 Path.rename 抛 → 回滚失败
    from tests.test_db_connections import TrackingConnection

    real_connect = sqlite3.connect
    instances = []

    def faulty_connect(*args, **kwargs):
        real = real_connect(*args, **kwargs)
        wrapper = TrackingConnection(real)
        wrapper.set_failure_on_sql("UPDATE", sqlite3.OperationalError("database is locked"))
        instances.append(wrapper)
        return wrapper

    rename_call_count = [0]
    real_rename = Path.rename

    def faulty_rename(self, target):
        rename_call_count[0] += 1
        # 第一次 rename（Clippings → KB）成功；第二次 rename（KB → Clippings 回滚）失败
        if rename_call_count[0] == 2:
            raise OSError("disk full during rollback")
        return real_rename(self, target)

    with patch.object(sqlite3, "connect", faulty_connect), \
         patch.object(Path, "rename", faulty_rename):
        try:
            reclaim_main()
        except Exception:
            pass

    captured = capsys.readouterr().out
    # 严格断言：必须含 '回滚失败' 的 dead-letter 标识，且含 '文件位置不可知'
    # （UPDATE 失败时的 '回滚文件到Clippings' 不算 dead-letter）
    assert "回滚失败" in captured, f"dead-letter 必须明确打印 '回滚失败'，实际: {captured!r}"
    assert "文件位置不可知" in captured, \
        f"dead-letter 必须含 '文件位置不可知'（区别于 UPDATE 失败提示），实际: {captured!r}"
    # 汇总必须列出 dead-letter 文件数
    assert "回滚失败（位置不可知" in captured or "回滚失败" in captured, \
        f"汇总必须列 dead-letter，实际: {captured!r}"


def test_reclaim_keyboard_interrupt_rolls_back_all(temp_db, tmp_path, monkeypatch):
    """KeyboardInterrupt（Ctrl+C）穿透循环时，已 rename 的文件必须全量回滚

    回归 #4：旧实现只 catch OSError(rename) 和 sqlite3.Error(UPDATE)，
    KeyboardInterrupt 在 rename 成功后穿透 → renamed_pairs 不被遍历回滚 →
    .md 滞留 KB 文件夹；下轮 reclaim 只扫 Clippings 找不到 → 永久漏存。
    """
    vault, clip_dir = _setup_isolated_vault(tmp_path, temp_db)
    # 多插一行让循环至少跑两次
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO articles (url, title, knowledge_base, status, obsidian_saved) "
        "VALUES (?,?,?,?,?)",
        ("https://mp.weixin.qq.com/s?__biz=T2&mid=T2&idx=1&sn=T2", "测试文章B", "Invest", "success", 0),
    )
    conn.commit()
    conn.close()
    (vault / "Invest").mkdir(exist_ok=True)

    file_a = clip_dir / "测试文章A.md"
    file_a.write_text("正文A\n*2026年1月2日 10:00*\n", encoding="utf-8")
    file_b = clip_dir / "测试文章B.md"
    file_b.write_text("正文B\n*2026年2月3日 10:00*\n", encoding="utf-8")

    monkeypatch.setattr("reclaim_clippings.VAULT_DIR", vault)
    monkeypatch.setattr("reclaim_clippings.CLIPPINGS_DIR", clip_dir)
    monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
    monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
    monkeypatch.setattr("sys.argv", ["reclaim_clippings.py", "--apply"])

    # 在第二条 rename 时中断（第一条已成功）
    rename_call_count = [0]
    real_rename = Path.rename

    def faulty_rename(self, target):
        rename_call_count[0] += 1
        if rename_call_count[0] == 2:  # 第二条 rename 时中断
            raise KeyboardInterrupt("Ctrl+C")
        return real_rename(self, target)

    with patch.object(Path, "rename", faulty_rename):
        try:
            reclaim_main()
        except KeyboardInterrupt:
            pass  # 顶层会重抛，但回滚已发生

    # 关键不变式：第一条 rename 成功的文件必须被回滚到 Clippings
    assert file_a.exists(), "KeyboardInterrupt 时 file_a 必须回滚到 Clippings（不能丢）"
