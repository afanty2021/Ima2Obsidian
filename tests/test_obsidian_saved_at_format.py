"""F4b: obsidian_saved_at 时间戳格式必须跨写者一致"""
import sqlite3
from datetime import datetime

from ima_common import init_database, now_saved_at
from ima_obsidian_saver import mark_saved


def test_helper_returns_iso_with_t_separator_seconds_precision():
    """helper 必须返回带 T 分隔符、秒精度的 ISO 字符串"""
    s = now_saved_at()
    parsed = datetime.fromisoformat(s)  # 必须可被解析（Python 3.7+）
    assert parsed is not None
    assert "T" in s, f"期望 T 分隔符，实际: {s!r}"
    assert "." not in s, f"不应含微秒，实际: {s!r}"
    # 字典序与时间序一致：YYYY-MM-DDTHH:MM:SS 共 19 字符
    assert len(s) == 19, f"长度应为 19，实际 {len(s)}: {s!r}"


def test_mark_saved_uses_helper_format(seeded_db):
    """saver 写入的 obsidian_saved_at 必须符合 helper 格式"""
    mark_saved(2)

    conn = sqlite3.connect(seeded_db)
    c = conn.cursor()
    c.execute("SELECT obsidian_saved_at FROM articles WHERE id=2")
    ts = c.fetchone()[0]
    conn.close()

    assert "T" in ts and "." not in ts, f"saver 写入格式错误: {ts!r}"
    datetime.fromisoformat(ts)  # 必须可解析


def test_reclaim_uses_helper_format(seeded_db, tmp_path, monkeypatch):
    """reclaim 写入的 obsidian_saved_at 必须符合 helper 格式"""
    # 准备一个伪造的 Clippings 文件，内容含可识别的日期，文件名匹配未保存文章标题
    # 同时把 VAULT_DIR 也指向 tmp_path，避免污染真实 vault
    fake_vault = tmp_path / "Vault"
    fake_vault.mkdir()
    (fake_vault / "AI").mkdir()       # KB 文件夹必须存在，reclaim 才会移动
    (fake_vault / "Invest").mkdir()
    fake_clip_dir = fake_vault / "Clippings"
    fake_clip_dir.mkdir()
    # 用 id=2 标题（在 AI KB），文件名带去重后缀模拟 Web Clipper
    fake_file = fake_clip_dir / "未保存文章A.md"
    fake_file.write_text("正文\n*2026年2月3日 10:00*\n", encoding="utf-8")

    monkeypatch.setattr("reclaim_clippings.CLIPPINGS_DIR", fake_clip_dir)
    monkeypatch.setattr("reclaim_clippings.VAULT_DIR", fake_vault)
    monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", fake_vault)
    monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", fake_clip_dir)

    # 让 reclaim 在本测试 DB 上工作（patch 它通过 ima_obsidian_saver.DB_FILE 间接拿到）
    import reclaim_clippings
    monkeypatch.setattr("sys.argv", ["reclaim_clippings.py", "--apply"])
    reclaim_clippings.main()

    conn = sqlite3.connect(seeded_db)
    c = conn.cursor()
    c.execute("SELECT obsidian_saved_at FROM articles WHERE id=2")
    ts = c.fetchone()[0]
    conn.close()

    assert ts is not None, "reclaim 未标记该文章（可能匹配/移动失败）"
    assert "T" in ts and "." not in ts, f"reclaim 写入格式错误: {ts!r}"
    datetime.fromisoformat(ts)


def test_format_consistent_across_writers(seeded_db, tmp_path, monkeypatch):
    """两种写者产出的时间戳必须同格式、同长度、同分隔符"""
    # 完全隔离的 vault
    fake_vault = tmp_path / "Vault"
    fake_vault.mkdir()
    (fake_vault / "AI").mkdir()
    (fake_vault / "Invest").mkdir()
    fake_clip_dir = fake_vault / "Clippings"
    fake_clip_dir.mkdir()
    monkeypatch.setattr("reclaim_clippings.CLIPPINGS_DIR", fake_clip_dir)
    monkeypatch.setattr("reclaim_clippings.VAULT_DIR", fake_vault)
    monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", fake_vault)
    monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", fake_clip_dir)

    # saver 写 id=2
    mark_saved(2)

    # reclaim 写 id=3（标题"未保存文章B"，KB=Invest）
    (fake_clip_dir / "未保存文章B.md").write_text(
        "正文\n*2026年3月4日 10:00*\n", encoding="utf-8"
    )
    import reclaim_clippings
    monkeypatch.setattr("sys.argv", ["reclaim_clippings.py", "--apply"])
    reclaim_clippings.main()

    conn = sqlite3.connect(seeded_db)
    c = conn.cursor()
    c.execute("SELECT id, obsidian_saved_at FROM articles WHERE id IN (2,3) ORDER BY id")
    rows = c.fetchall()
    conn.close()

    saver_ts = next(ts for aid, ts in rows if aid == 2)
    reclaim_ts = next(ts for aid, ts in rows if aid == 3)
    assert saver_ts is not None, "saver 未写入"
    assert reclaim_ts is not None, "reclaim 未写入"
    assert len(saver_ts) == len(reclaim_ts) == 19
    assert saver_ts[10] == reclaim_ts[10] == "T"
