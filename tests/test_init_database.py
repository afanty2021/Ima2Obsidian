"""F8: init_database 不应吞掉非 'duplicate column' 的 OperationalError"""
import sqlite3
from unittest.mock import patch

import pytest

from ima_common import init_database


def test_init_creates_schema_first_time(temp_db):
    """全新 DB：init_database 必须成功创建所有列"""
    init_database()  # 不抛异常即通过
    conn = sqlite3.connect(temp_db)
    c = conn.cursor()
    c.execute("PRAGMA table_info(articles)")
    cols = {row[1] for row in c.fetchall()}
    conn.close()
    expected = {"id", "url", "title", "knowledge_base", "extracted_at",
                "y_position", "status", "obsidian_saved", "obsidian_saved_at",
                "published_date"}
    assert expected.issubset(cols), f"缺失列: {expected - cols}"


def test_init_idempotent_on_migrated_db(temp_db):
    """已迁移 DB：再次 init 不抛异常（duplicate column 被精确捕获）"""
    init_database()
    init_database()  # 第二次必须无副作用
    init_database()  # 第三次确认幂等


def test_init_does_not_swallow_database_locked(temp_db, monkeypatch):
    """'database is locked' 等 OperationalError 不应被静默吞掉"""
    from unittest.mock import MagicMock

    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    def fake_execute(sql, *args, **kwargs):
        if "ALTER TABLE" in sql.upper() and "ADD COLUMN" in sql.upper():
            raise sqlite3.OperationalError("database is locked")
        return MagicMock()

    fake_cursor.execute.side_effect = fake_execute
    monkeypatch.setattr("ima_common.sqlite3.connect", lambda *a, **kw: fake_conn)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        init_database()


def test_init_swallows_only_duplicate_column(temp_db, capsys):
    """'duplicate column name' 错误必须被静默吞掉（兼容已有 DB）"""
    init_database()
    # 第二次运行时所有列都已存在，每个 ALTER 抛 'duplicate column name'
    # init_database 应静默通过，不打印错误
    init_database()
    captured = capsys.readouterr()
    # 没有输出错误（这里只是确认不抛异常 + 没有意外输出）
    assert "Traceback" not in captured.err


def test_init_closes_connection_on_alter_raise(temp_db, monkeypatch):
    """F8 raise 路径必须关闭连接：不能因 raise 跳过 close 而泄漏 fd

    历史问题：F8 修复让 'database is locked' 等 OperationalError 传播，
    但 init_database 主体仍是裸 connect + 末尾 close，raise 跳过 close → fd 泄漏。
    用 TrackingConnection 验证 raise 路径上 close 被调用。
    """
    from tests.test_db_connections import TrackingConnection

    real_connect = sqlite3.connect
    instances = []

    def faulty_connect(*args, **kwargs):
        real = real_connect(*args, **kwargs)
        wrapper = TrackingConnection(real)
        # 让 ALTER TABLE ADD COLUMN 抛 'database is locked'
        wrapper.set_cursor_exception(sqlite3.OperationalError("database is locked"))
        instances.append(wrapper)
        return wrapper

    # 先建好 schema（让后续 ALTER 路径必然走），再用 faulty connect 替换
    init_database()
    instances.clear()

    monkeypatch.setattr(sqlite3, "connect", faulty_connect)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        init_database()

    # raise 路径也必须 close
    assert len(instances) >= 1, "应当打开过连接"
    for conn in instances:
        assert conn.close_called, "init_database raise 路径未关闭连接（fd 泄漏）"
