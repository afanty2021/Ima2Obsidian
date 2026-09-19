"""
共享 pytest fixtures

关键策略：
- 每个测试用独立临时 DB 文件，避免污染真实 ima_articles.db
- monkeypatch DB_FILE 在模块加载后就已绑定，需 patch 模块属性
- subprocess 调用默认 mock，避免触发 cua-driver / osascript
"""
import importlib
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """提供一个干净临时 DB，并 patch 所有相关模块的 DB_FILE"""
    db_path = tmp_path / "test_articles.db"

    # patch 每个用到 DB_FILE 的模块的模块级属性
    for mod_name in ("ima_common", "ima_ax_extractor", "ima_obsidian_saver",
                     "ima_incremental_update", "reclaim_clippings",
                     "migrate_normalize_urls"):
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "DB_FILE"):
            monkeypatch.setattr(mod, "DB_FILE", db_path)

    yield db_path


@pytest.fixture(autouse=True)
def _isolate_run_state_file(tmp_path, monkeypatch):
    """测试不得触碰真实的 last_incremental_run.json（17:10 兜底槽前置判断依据）。

    背景：write_run_state 加进 main 收尾后，跑到完整收尾的 main() 测试
    （如 test_failed_saver_does_not_consume_reclaim_for_next_kb）曾把 mock 结果
    写进真实状态文件，污染当晚的前置判断。
    """
    mod = importlib.import_module("ima_incremental_update")
    monkeypatch.setattr(mod, "RUN_STATE_FILE", tmp_path / "last_incremental_run.json")


@pytest.fixture(autouse=True)
def _isolate_run_log_file(tmp_path, monkeypatch):
    """测试不得写真实的 incremental_update.log（launchd 运行日志）。

    背景：log() 无条件 append 到模块级 LOG_FILE（非 TTY 时不 print），
    未 patch log 的用例（如 test_restart_ima_fallback 的兜底路径）会把
    mock 运行的输出混进真实日志，干扰按日志定位线上问题。各测试内
    显式 patch LOG_FILE 的写法保留不动（指向同一 tmp 语义，无害冗余）。
    """
    mod = importlib.import_module("ima_incremental_update")
    monkeypatch.setattr(mod, "LOG_FILE", tmp_path / "incremental_update.log")


@pytest.fixture(autouse=True)
def _quiet_app_cleanup(monkeypatch):
    """main() 收尾会退出真实 IMA/Obsidian/Chrome（osascript quit）——测试一律 no-op。

    跑到完整收尾的 main() 用例若不隔离，会把用户正开着的浏览器/编辑器关掉。
    需要断言收尾行为的用例从本夹具的 yield 值拿 MagicMock；对 quit 逻辑本身
    的单元测试应在收集期绑定真实函数（from ... import 于模块顶层）。
    """
    mod = importlib.import_module("ima_incremental_update")
    mock = MagicMock()
    monkeypatch.setattr(mod, "cleanup_gui_apps", mock)
    yield mock


@pytest.fixture(autouse=True)
def _stub_ima_restart(monkeypatch):
    """默认隔离对真实 IMA 的重启/拉起（自愈循环、导航兜底会调用）。

    自愈走 _heal_wedge 的惰性导入取模块属性，本兜底使未显式 mock 的用例
    一律走「重启失败 → 中止」的安全路径，绝不真的退出/拉起 IMA。需要真实
    断言的用例在 ima_incremental_update 上自行 monkeypatch 覆盖即可。
    """
    mod = importlib.import_module("ima_incremental_update")
    monkeypatch.setattr(mod, "restart_ima", lambda: False)
    monkeypatch.setattr(mod, "launch_ima", lambda *a, **k: False)


@pytest.fixture
def seeded_db(temp_db):
    """初始化 schema 并插入若干测试文章"""
    from ima_common import init_database
    init_database()

    conn = sqlite3.connect(temp_db)
    c = conn.cursor()
    # 插入 3 篇：1 已保存 + 2 未保存
    test_rows = [
        # (url, title, kb, status, obsidian_saved, obsidian_saved_at, published_date)
        ("https://mp.weixin.qq.com/s?__biz=B&mid=M1&idx=1&sn=S1", "已保存文章", "AI", "success", 1, "2026-01-01T10:00:00", "260101"),
        ("https://mp.weixin.qq.com/s?__biz=B&mid=M2&idx=1&sn=S2", "未保存文章A", "AI", "success", 0, None, None),
        ("https://mp.weixin.qq.com/s?__biz=B&mid=M3&idx=1&sn=S3", "未保存文章B", "Invest", "success", 0, None, None),
        # 非 success / 非 mp.weixin 不应被统计
        ("https://example.com/x", "无效状态", "AI", "failed", 0, None, None),
    ]
    c.executemany(
        "INSERT INTO articles (url, title, knowledge_base, status, obsidian_saved, obsidian_saved_at, published_date) "
        "VALUES (?,?,?,?,?,?,?)",
        test_rows,
    )
    conn.commit()
    conn.close()
    return temp_db


@pytest.fixture(autouse=True)
def _stub_saver_execute_chrome_js(monkeypatch):
    """默认隔离 saver 对本机真实 Chrome 的 osascript 调用（返回 None = 失败退化路径）。

    背景：save_one_article 的旧用例大多只 patch 上层函数，未封 execute_chrome_js——
    以前靠「osascript 快速失败」侥幸成立；wait_page_ready 就绪轮询引入后（562aa84
    评审跟进），真实 Chrome 在跑时每篇会烧满 6s 预算，全量测试从 ~10s 涨到 ~165s。
    测试内显式 patch execute_chrome_js / wait_page_ready 的用例不受影响。
    """
    try:
        import ima_obsidian_saver as sv
    except ImportError:
        return
    monkeypatch.setattr(sv, "execute_chrome_js", lambda *a, **k: None)
