"""F2/F7: 所有 DB 访问点在异常路径上必须关闭连接"""
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from ima_common import init_database
from ima_ax_extractor import save_article
from ima_obsidian_saver import mark_saved, get_stats, get_unsaved_articles
from ima_incremental_update import count_unsaved_articles


class TrackingCursor:
    """包装 sqlite3.Cursor，可注入 execute 异常（支持按 SQL 内容条件触发）"""

    def __init__(self, real_cursor):
        self._real = real_cursor
        self.execute_exception = None  # 无条件触发
        self.conditional_exceptions = []  # [(sql_substr, exc), ...] 按 SQL 内容触发

    def execute(self, sql, *args, **kwargs):
        for substr, exc in self.conditional_exceptions:
            if substr.lower() in sql.lower():
                raise exc
        if self.execute_exception:
            raise self.execute_exception
        return self._real.execute(sql, *args, **kwargs)

    def fetchall(self):
        return self._real.fetchall()

    def fetchone(self):
        return self._real.fetchone()

    def __getattr__(self, name):
        return getattr(self._real, name)


class TrackingConnection:
    """包装 sqlite3.Connection，记录 close() 是否被调用"""

    def __init__(self, real_conn):
        self._real = real_conn
        self.close_called = False
        self._cursor_exception = None
        self._conditional = []  # [(sql_substr, exc), ...]
        self._commit_exception = None  # 注入 commit() 异常

    def set_cursor_exception(self, exc):
        """让后续 cursor().execute() 抛指定异常，用于测试异常路径"""
        self._cursor_exception = exc

    def set_failure_on_sql(self, sql_substr, exc):
        """按 SQL 内容触发：execute UPDATE/INSERT/DELETE 等特定语句时抛异常"""
        self._conditional.append((sql_substr, exc))

    def set_commit_exception(self, exc):
        """让下次 commit() 抛指定异常（模拟 'database is locked' 等）"""
        self._commit_exception = exc

    def cursor(self):
        c = TrackingCursor(self._real.cursor())
        if self._cursor_exception:
            c.execute_exception = self._cursor_exception
        c.conditional_exceptions = list(self._conditional)
        return c

    def commit(self):
        if self._commit_exception:
            exc = self._commit_exception
            self._commit_exception = None  # 触发后清除，避免 close 时再抛
            raise exc
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def execute(self, *args, **kwargs):
        return self._real.execute(*args, **kwargs)

    def close(self):
        self.close_called = True
        return self._real.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def tracked_db(temp_db):
    """注入一个追踪 close() 的 connect wrapper，返回 (db_path, tracker_list)"""
    init_database()  # 先用真 connect 建 schema
    instances = []

    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        real = real_connect(*args, **kwargs)
        wrapper = TrackingConnection(real)
        instances.append(wrapper)
        return wrapper

    # patch 所有相关模块的 sqlite3.connect 引用
    patches = []
    for mod_name in ("ima_common", "ima_ax_extractor", "ima_obsidian_saver",
                     "ima_incremental_update"):
        import importlib
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "sqlite3"):
            patches.append(patch.object(mod.sqlite3, "connect", tracking_connect))

    for p in patches:
        p.start()

    yield temp_db, instances

    for p in patches:
        p.stop()


@pytest.fixture
def tracking_connect_factory(tracked_db):
    """返回一个工厂：每次调用得到 (connect_fn, instances_list)，
    可在测试中通过 set_cursor_exception 注入异常到即将创建的连接"""
    real_connect = sqlite3.connect
    instances = tracked_db[1]

    def make_connect_with_exception(exception_to_inject=None):
        def connect(*args, **kwargs):
            real = real_connect(*args, **kwargs)
            wrapper = TrackingConnection(real)
            if exception_to_inject:
                wrapper.set_cursor_exception(exception_to_inject)
            instances.append(wrapper)
            return wrapper
        return connect
    return make_connect_with_exception


def test_save_article_closes_connection_on_exception(tracked_db):
    """save_article 在 INSERT 异常时仍必须 close 连接"""
    db_path, instances = tracked_db
    instances.clear()

    real_connect = sqlite3.connect

    def faulty_connect(*args, **kwargs):
        real = real_connect(*args, **kwargs)
        wrapper = TrackingConnection(real)
        wrapper.set_cursor_exception(sqlite3.IntegrityError("mock constraint failure"))
        instances.append(wrapper)
        return wrapper

    # sqlite3 是单例模块；patch sqlite3.connect 影响所有调用者
    with patch.object(sqlite3, "connect", faulty_connect):
        save_article("https://example.com/x", "title", "AI")  # 内部捕获，返回 False

    assert len(instances) >= 1, "应当打开过连接"
    for conn in instances:
        assert conn.close_called, "save_article 异常路径未关闭连接（fd 泄漏）"


def test_count_unsaved_articles_closes_connection_on_exception(tracked_db):
    """count_unsaved_articles 在 SELECT 异常时必须 close 连接"""
    db_path, instances = tracked_db
    instances.clear()

    real_connect = sqlite3.connect

    def faulty_connect(*args, **kwargs):
        real = real_connect(*args, **kwargs)
        wrapper = TrackingConnection(real)
        wrapper.set_cursor_exception(sqlite3.OperationalError("mock select failure"))
        instances.append(wrapper)
        return wrapper

    with patch.object(sqlite3, "connect", faulty_connect):
        count = count_unsaved_articles("AI")

    assert count == 0  # 异常时返回 0（向后兼容）
    for conn in instances:
        assert conn.close_called, "count_unsaved_articles 异常路径未关闭连接（fd 泄漏）"


def test_mark_saved_closes_connection_on_success(tracked_db):
    """mark_saved 正常路径也必须 close 连接"""
    db_path, instances = tracked_db

    # 先插一行
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO articles (url, title, knowledge_base, status) VALUES (?,?,?,?)",
        ("https://example.com/test_close", "t", "AI", "success"),
    )
    conn.commit()
    conn.close()

    # 清空 instances 列表只追踪 mark_saved 的连接
    instances.clear()
    mark_saved(1)

    assert len(instances) == 1
    assert instances[0].close_called


def test_get_stats_closes_connection(tracked_db):
    db_path, instances = tracked_db
    instances.clear()
    get_stats()
    assert len(instances) == 1
    assert instances[0].close_called


def test_get_unsaved_articles_closes_connection(tracked_db):
    db_path, instances = tracked_db
    instances.clear()
    get_unsaved_articles(limit=10)
    assert len(instances) == 1
    assert instances[0].close_called
