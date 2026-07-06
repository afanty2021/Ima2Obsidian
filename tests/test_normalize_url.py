"""F4: normalize_url 单元测试 — 验证规范化属性"""
import pytest

from ima_ax_extractor import normalize_url


class TestNormalizeUrlIdempotent:
    """规范化必须幂等：反复应用得到相同结果"""

    @pytest.mark.parametrize("url", [
        "https://mp.weixin.qq.com/s?__biz=B&mid=M&idx=1&sn=S",
        "https://mp.weixin.qq.com/s/AbCd123",
        "https://mp.weixin.qq.com/s?__biz=B&mid=M&idx=1&sn=S&scene=161",
        "https://zhihu.com/answer/123456?utm_source=x",
        "https://example.com/path?a=1&b=2&utm_medium=y",
        "",
        None,
    ])
    def test_idempotent(self, url):
        once = normalize_url(url)
        twice = normalize_url(once)
        assert once == twice, f"非幂等: once={once!r} twice={twice!r}"


class TestWechatShortForm:
    """微信短格式 /s/ARTICLE_ID：去除所有 ? 参数"""

    def test_strip_query(self):
        assert normalize_url("https://mp.weixin.qq.com/s/ABC123?foo=bar&scene=1") \
            == "https://mp.weixin.qq.com/s/ABC123"

    def test_no_query_unchanged(self):
        assert normalize_url("https://mp.weixin.qq.com/s/ABC123") \
            == "https://mp.weixin.qq.com/s/ABC123"

    def test_strip_fragment(self):
        # 微信常带 #rd 锚点，应当去除
        assert normalize_url("https://mp.weixin.qq.com/s/ABC123#rd") \
            == "https://mp.weixin.qq.com/s/ABC123"


class TestWechatLongForm:
    """微信长格式 /s?__biz=...&mid=...&idx=...&sn=...：保留核心参数"""

    def test_keep_core_params(self):
        result = normalize_url(
            "https://mp.weixin.qq.com/s?__biz=B&mid=M&idx=1&sn=S"
            "&chksm=abc&scene=161&sessionid=xyz#rd"
        )
        # 去除 chksm/scene/sessionid 与 #rd，仅保留 __biz/mid/idx/sn
        assert "__biz=B" in result
        assert "mid=M" in result
        assert "idx=1" in result
        assert "sn=S" in result
        assert "chksm" not in result
        assert "scene" not in result
        assert "sessionid" not in result
        assert "#rd" not in result

    def test_param_order_canonical(self):
        """同参数不同顺序应规范化到同一结果（顺序无关性）"""
        url_a = "https://mp.weixin.qq.com/s?__biz=B&mid=M&idx=1&sn=S&scene=1"
        url_b = "https://mp.weixin.qq.com/s?sn=S&idx=1&mid=M&__biz=B&chksm=x"
        url_c = "https://mp.weixin.qq.com/s?mid=M&sn=S&__biz=B&idx=1"
        assert normalize_url(url_a) == normalize_url(url_b) == normalize_url(url_c)

    def test_no_core_params_keeps_original(self):
        """长格式但完全没有核心参数时，不应把不同 URL 折叠成 /s（避免碰撞）"""
        # 旧实现会把它们都折叠到 bare base，造成跨文章误判重复
        url_a = "https://mp.weixin.qq.com/s?scene=1&random=x"
        url_b = "https://mp.weixin.qq.com/s?scene=2&random=y"
        na, nb = normalize_url(url_a), normalize_url(url_b)
        assert na != nb, f"两篇不同文章被折叠到同一 URL: {na!r}"


class TestZhihuAndGeneric:
    """知乎与通用平台规范化"""

    def test_zhihu_strips_all_params(self):
        assert normalize_url("https://zhuanlan.zhihu.com/p/123?utm_source=x") \
            == "https://zhuanlan.zhihu.com/p/123"

    def test_generic_strips_tracking(self):
        result = normalize_url("https://example.com/path?a=1&utm_source=x&b=2")
        assert "a=1" in result
        assert "b=2" in result
        assert "utm_source" not in result

    def test_empty_input(self):
        assert normalize_url("") == ""
        assert normalize_url(None) is None


class TestCrossFormLimitation:
    """
    跨形式不统一性：短 /s/ID 和长 /s?__biz=.. 在不发起网络请求的前提下
    无法判定是否指向同一篇文章。本测试仅确认这一已知限制被显式记录。
    """

    def test_short_and_long_remain_distinct(self):
        short = normalize_url("https://mp.weixin.qq.com/s/ABC123")
        long_ = normalize_url("https://mp.weixin.qq.com/s?__biz=B&mid=M&idx=1&sn=S")
        # 它们是不同的字符串——这是已知限制，不能在 normalize_url 层统一
        assert short != long_
