"""
save_article URL 白名单测试——排除非微信文章（如 IMA copilot 公告的 github.com）。

背景：2026-05-08 提取器误抓 IMA 界面"copilot 功能上线"公告卡片，打开后地址栏是
github.com，存进 DB 污染文章列表。修复：save_article 加 mp.weixin.qq.com hostname 白名单。

所有测试用 seeded_db fixture（隔离临时 DB，不污染生产 ima_articles.db，review PR#12 #3）。
"""
from ima_ax_extractor import save_article, url_exists


def test_save_article_rejects_non_wechat_url(seeded_db):
    """非微信 URL 被白名单拒绝（return False + 不存 DB）"""
    test_url = "https://github.com/filter-test-repo-unique-20260804"
    result = save_article(test_url, "filter test", "AI")
    assert result is False, "非微信 URL 应被拒绝"
    assert not url_exists(test_url), "非微信 URL 不应进入 DB"


def test_save_article_rejects_other_non_content_domains(seeded_db):
    """其他非内容域（如 twitter/youtube）也被拒绝"""
    for url in [
        "https://twitter.com/some-status",
        "https://www.youtube.com/watch?v=test",
    ]:
        result = save_article(url, "test", "AI")
        assert result is False, "{} 应被拒绝".format(url)


def test_save_article_rejects_substring_bypass(seeded_db):
    """子串绕过防护：/path?redirect=mp.weixin.qq.com 不应通过（review PR#12 #5）"""
    bypass_url = "https://evil.com/path?redirect=mp.weixin.qq.com"
    result = save_article(bypass_url, "bypass test", "AI")
    assert result is False, "hostname 非 mp.weixin.qq.com 应被拒绝（子串绕过防护）"


def test_save_article_accepts_wechat_long_url(seeded_db):
    """微信长格式 URL 正常存"""
    test_url = "https://mp.weixin.qq.com/s?__biz=MzTestFilter==&mid=123&idx=1&sn=abc-filter-test"
    result = save_article(test_url, "filter test wechat", "AI")
    assert result is True, "微信长格式 URL 应被接受"


def test_save_article_accepts_wechat_short_url(seeded_db):
    """微信短格式 URL 正常存"""
    test_url = "https://mp.weixin.qq.com/s/FilterTestUnique20260804"
    result = save_article(test_url, "filter test short", "AI")
    assert result is True, "微信短格式 URL 应被接受"
