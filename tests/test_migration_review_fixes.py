"""迁移复审修复的回归测试（2026-10-01 cua 0.31 迁移 max 评审）。

评审 4 项主发现的钉子：
- Critical #1  navigate_to_kb 对话页分支重读漏注册 → stale token → index 回退
              被 0.31 硬拒 → RuntimeError 炸整轮无人值守运行
- Important #2 _probe_article_window_md 漏 include_screenshot:false（纯树模式
              的唯一漏洞，屏幕录制授权失效时死页探测静默残废）
- Important #3 saver 单独拉 daemon 缺权限门旁路（无人值守挂死风险）
- Important #4 cua_bring_to_front 漏捕 TimeoutExpired（卡死守护进程击穿降级）
"""
import inspect
import json
import os
import re
import subprocess
from types import SimpleNamespace

import pytest

import ima_common
import ima_ax_extractor as extractor
import ima_incremental_update as upd
import ima_obsidian_saver as saver

# conftest 的 _stub_extractor_dead_page_probe 会按用例把
# extractor._probe_article_window_md 桩成 lambda:""——收集期先捕获真实函数，
# 需要测真身的用例调 _REAL_PROBE 而非模块属性
_REAL_PROBE = extractor._probe_article_window_md


class FakeDriver031:
    """进程内仿真 cua-driver 0.31 语义（评审者同款复现口径）。

    - get_window_state：登记新快照 tokens，同 pid 旧快照 token 全部退役
    - click 带 element_token：命中活 token 才成功；否则 stale 拒绝
      （0.31 token 自带窗口定位，token 路径不发 window_id——按 pid 校验）
    - click 带 element_index：无条件硬拒 invalid_arguments（0.31 实机行为）
    """

    def __init__(self):
        self.live_tokens = {}  # pid -> set(token)（最近一次快照的活 token）
        self.reads = 0
        self.clicks = []       # 每次点击实发参数

    @staticmethod
    def _fail(code):
        err = RuntimeError(f"cua-driver failed: exit 1")
        err.stdout = json.dumps({"error": {"code": code}})
        raise err

    def run(self, args, timeout=30):
        tool, params = args[1], json.loads(args[2])
        if tool == "get_window_state":
            self.reads += 1
            if self.reads == 1:
                # 对话页：含导航按钮 + 恰好 5 个 AXStaticText（≥5 跳过重试分支）
                md = ('AXWindow "问问ima"\n' + '[x] AXStaticText = "对话内容"\n' * 5
                      + '[7] AXButton = "知识库"\n')
                tokens = {7: "tok-A-7", 30: "tok-A-30"}
            else:
                # 第 2+ 次读：导航后的 KB 列表页（token 换代，旧 tok-A-* 退役）
                md = '[30] AXStaticText = "AI"\n' + '[x] AXStaticText = "条目"\n' * 5
                tokens = {30: "tok-B-30"}
            self.live_tokens[params["pid"]] = set(tokens.values())
            return json.dumps({"tree_markdown": md, "elements": [
                {"element_index": i, "element_token": t} for i, t in tokens.items()
            ]})
        if tool == "click":
            self.clicks.append(params)
            tok = params.get("element_token")
            if tok is not None:
                if tok in self.live_tokens.get(params["pid"], set()):
                    return "{}"
                self._fail("stale_token")
            self._fail("invalid_arguments")
        return "{}"


@pytest.fixture()
def chat_page_env(monkeypatch):
    """navigate_to_kb 走「对话页 → 点知识库导航 → 重读 → 点 KB 入口」的最小环境。"""
    drv = FakeDriver031()
    monkeypatch.setattr(ima_common, "run_cua", drv.run)
    monkeypatch.setattr(upd, "get_ima_main_window",
                        lambda: {"pid": 100, "window_id": 9})
    monkeypatch.setattr(upd, "subprocess", SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(returncode=0)))
    monkeypatch.setattr(upd, "is_on_kb_list", lambda kb: True)
    monkeypatch.setattr(upd.time, "sleep", lambda s: None)
    return drv


class TestChatPageTokenReRegister:
    def test_kb_entry_click_uses_post_nav_snapshot_token(self, chat_page_env):
        # 漏注册时：KB 入口点击拿旧快照 tok-A-30 → stale → index 回退 →
        # invalid_arguments → RuntimeError 上抛（炸整轮无人值守）
        drv = chat_page_env
        assert upd.navigate_to_kb("AI") is True
        kb_clicks = [p for p in drv.clicks if p.get("element_token") == "tok-B-30"]
        assert kb_clicks, (
            f"KB 入口未用导航后新快照 token（stale/硬拒路径）: {drv.clicks}")
        # 导航按钮点击用的是首快照 token（当时是活的）
        assert any(p.get("element_token") == "tok-A-7" for p in drv.clicks)

    def test_every_read_in_navigate_registers_before_next_click(self):
        """源序不变量：navigate_to_kb 内每次 get_window_state 之后、下一次
        cua_click 之前必须出现 remember_window_elements（防同类回归）。"""
        src = inspect.getsource(upd.navigate_to_kb)
        events = []
        for m in re.finditer(r'cua_call\(\s*"get_window_state"', src):
            events.append((m.start(), "read"))
        for m in re.finditer(r"remember_window_elements\(", src):
            events.append((m.start(), "remember"))
        for m in re.finditer(r"cua_click\(", src):
            events.append((m.start(), "click"))
        assert events, "navigate_to_kb 应含读窗/注册/点击序列"
        events.sort()
        pending_read = False
        for _, kind in events:
            if kind == "read":
                pending_read = True
            elif kind == "remember":
                pending_read = False
            else:
                assert not pending_read, (
                    "存在读窗后未 remember 即点击的路径：0.31 上 stale token → "
                    "index 回退被硬拒 → RuntimeError 炸整轮")


class TestProbePureTree:
    def test_probe_requests_no_screenshot_and_registers(self, monkeypatch):
        captured = {}

        def fake_run(args, timeout=30):
            if args[0] == "list_windows":
                return json.dumps({"windows": [{
                    "pid": 11, "window_id": 4, "app_name": "ima.copilot",
                    "bounds": {"height": 800, "width": 1200},
                }]})
            captured.update(json.loads(args[2]))
            return json.dumps({
                "tree_markdown": '[1] AXTextField = "地址和搜索栏"',
                "elements": [{"element_index": 1, "element_token": "tok-p-1"}],
            })

        monkeypatch.setattr(ima_common, "run_cua", fake_run)
        monkeypatch.setattr(extractor, "activate_ima", lambda: None)
        monkeypatch.setattr(extractor.time, "sleep", lambda s: None)
        md = _REAL_PROBE()
        assert "地址和搜索栏" in md
        # 纯树模式不留屏幕录制依赖（屏幕录制授权失效时探测不得静默残废）
        assert captured.get("include_screenshot") is False
        # 读后注册：注册表与「最后一次读窗」一致
        assert ima_common._ELEMENT_TOKENS[(11, 4)] == {1: "tok-p-1"}


class TestSaverDaemonGateBypass:
    def test_spawn_carries_permissions_gate_bypass(self, monkeypatch):
        spawns = []

        monkeypatch.setattr(saver, "is_daemon_running", lambda: False)
        monkeypatch.setattr(saver, "subprocess", SimpleNamespace(
            Popen=lambda cmd, **kw: spawns.append((cmd, kw)) or SimpleNamespace(pid=1),
            DEVNULL=-3,
        ))
        monkeypatch.setattr(saver.time, "sleep", lambda s: None)

        assert saver._ensure_daemon_for_receipt() is False  # 恒判未运行 → 轮询耗尽
        cmd, kw = spawns[0]
        assert cmd[-2:] == ["serve"] or "serve" in cmd
        env = kw["env"]
        assert env["CUA_DRIVER_RS_PERMISSIONS_GATE"] == "0"
        assert env["PATH"] == os.environ["PATH"]  # 继承而非替换


class TestBringToFrontRobustness:
    def test_timeout_expired_swallowed_returns_false(self, monkeypatch, capsys):
        def boom(args, timeout=30):
            raise subprocess.TimeoutExpired(cmd=["cua-driver"] + args,
                                            timeout=timeout)
        monkeypatch.setattr(ima_common, "run_cua", boom)
        assert ima_common.cua_bring_to_front(1, 2) is False  # 不得击穿调用点降级
        assert "bring_to_front 失败" in capsys.readouterr().out  # 次晨取证线索

    def test_partial_activation_still_passes(self, monkeypatch, capsys):
        def partial(args, timeout=30):
            err = RuntimeError("cua-driver failed: exit 1")
            err.stdout = json.dumps({"process_activated": True})
            raise err
        monkeypatch.setattr(ima_common, "run_cua", partial)
        assert ima_common.cua_bring_to_front(1, 2) is True
        assert "bring_to_front 失败" not in capsys.readouterr().out


class TestImportHygiene:
    def test_no_bare_run_cua_import(self):
        """裸 run_cua 导入是绕过统一测试接缝的陷阱（评审 Minor #8）。"""
        for mod in (upd, extractor):
            src = inspect.getsource(mod)
            m = re.search(r"from ima_common import \(([^)]*)\)", src)
            assert m, f"{mod.__name__} 应有 ima_common 导入块"
            assert not re.search(r"\brun_cua\b", m.group(1)), (
                f"{mod.__name__} 不应裸导入 run_cua（绕过 ima_common.run_cua 接缝）")


class ScrollFakeDriver:
    """scroll_down 行为测试的 0.31/0.8 双形态驱动。

    snapshot_tokens 控制读窗返回的元素 token 形态：dict 时 0.31（带 token），
    None 时 0.8（无 token 字段）。with_scrollarea=False 模拟树里没有
    AXScrollArea（坐标兜底路径）。scroll 调用全部记录参数。
    """

    def __init__(self, snapshot_tokens, scroll_shape="scrollarea"):
        # scroll_shape：scrollarea（有 AXScrollArea，首选靶）/ webarea（ima 实测
        # 形态——树里只有 AXWebArea）/ none（两者皆无 → 坐标兜底）
        self.snapshot_tokens = snapshot_tokens  # {index: token} 或 None（0.8）
        self.scroll_shape = scroll_shape
        self.reads = 0
        self.scrolls = []
        self.scroll_fail = None  # 异常实例：token 路径首次调用抛出后清空

    def run(self, args, timeout=30):
        if args[0] == "list_windows":  # _scroll_local_coords 直连形态
            return json.dumps({"windows": [{
                "window_id": 42, "bounds": {"width": 1000, "height": 800},
            }]})
        tool, params = args[1], json.loads(args[2])
        if tool == "get_window_state":
            self.reads += 1
            md, elements = "", []
            if self.scroll_shape == "scrollarea":
                md += '[5] AXScrollArea "列表"\n'
                elements.append({"element_index": 5, "role": "AXScrollArea"})
            elif self.scroll_shape == "webarea":
                md += '[5] AXWebArea "AI"\n'
                elements.append({"element_index": 5, "role": "AXWebArea"})
            md += '[8] AXStaticText = "卡片"'
            elements.append({"element_index": 8, "role": "AXStaticText"})
            if self.snapshot_tokens is not None:
                for el in elements:
                    el["element_token"] = self.snapshot_tokens.get(el["element_index"])
            return json.dumps({"tree_markdown": md, "elements": elements})
        if tool == "scroll":
            self.scrolls.append(params)
            if self.scroll_fail is not None and params.get("element_token"):
                raise_factory, self.scroll_fail = self.scroll_fail, None
                raise raise_factory
            return "{}"
        return "{}"


@pytest.fixture()
def clean_scroll_cache():
    extractor._SCROLL_AREA.clear()
    extractor._SCROLL_LOCAL_COORDS.clear()
    ima_common._ELEMENT_TOKENS.clear()
    yield
    extractor._SCROLL_AREA.clear()
    extractor._SCROLL_LOCAL_COORDS.clear()
    ima_common._ELEMENT_TOKENS.clear()


class TestScrollTokenPath:
    """0.31 纯树模式下 x/y 滚轮路径拿不到 screenshot context（实测
    screenshot_context_missing 连败），scroll_down 必须走 element_token。"""

    def test_token_scroll_with_burst_cache(self, monkeypatch, clean_scroll_cache):
        drv = ScrollFakeDriver({5: "tok-s-5"})
        monkeypatch.setattr(ima_common, "run_cua", drv.run)
        monkeypatch.setattr(extractor.time, "sleep", lambda s: None)
        for _ in range(3):  # 调用方 10 连发场景缩小为 3
            extractor.scroll_down(100, 42)
        # 首滚发现 ScrollArea（读窗 1 次），突发内缓存命中不再读窗
        assert drv.reads == 1, f"突发连滚应只读窗 1 次，实际 {drv.reads}"
        assert len(drv.scrolls) == 3
        assert all(p.get("element_token") == "tok-s-5" for p in drv.scrolls)
        assert not any("x" in p for p in drv.scrolls)  # 不走坐标路径

    def test_snapshot_refresh_invalidates_cache(self, monkeypatch, clean_scroll_cache):
        drv = ScrollFakeDriver({5: "tok-s-5"})
        monkeypatch.setattr(ima_common, "run_cua", drv.run)
        monkeypatch.setattr(extractor.time, "sleep", lambda s: None)
        extractor.scroll_down(100, 42)
        assert drv.reads == 1
        # 页循环重读前 token 已换代：重读登记新 token → 缓存比对不符必须作废
        drv.snapshot_tokens = {5: "tok-s-9"}
        extractor.get_window_state(100, 42)  # 页循环同款读窗（读即注册）
        extractor.scroll_down(100, 42)
        assert drv.reads == 3  # 首次发现 + 页循环重读 + 缓存失效重找
        assert drv.scrolls[-1].get("element_token") == "tok-s-9"

    def test_08_rollback_uses_index_path(self, monkeypatch, clean_scroll_cache):
        drv = ScrollFakeDriver(None)  # 0.8：state 无 element_token
        monkeypatch.setattr(ima_common, "run_cua", drv.run)
        monkeypatch.setattr(extractor.time, "sleep", lambda s: None)
        extractor.scroll_down(100, 42)
        assert drv.scrolls, "0.8 回滚态也应发出 scroll（index 路径）"
        assert drv.scrolls[0].get("element_index") == 5
        assert "element_token" not in drv.scrolls[0]
        assert drv.scrolls[0].get("window_id") == 42

    def test_webarea_shape_ima_real_form(self, monkeypatch, clean_scroll_cache):
        """ima 实测形态：列表页无 AXScrollArea，靶子回退到 AXWebArea 根。"""
        drv = ScrollFakeDriver({5: "tok-s-5"}, scroll_shape="webarea")
        monkeypatch.setattr(ima_common, "run_cua", drv.run)
        monkeypatch.setattr(extractor.time, "sleep", lambda s: None)
        extractor.scroll_down(100, 42)
        assert drv.scrolls and drv.scrolls[0].get("element_token") == "tok-s-5"
        assert not any("x" in p for p in drv.scrolls)

    def test_no_scrollarea_falls_back_to_coords(self, monkeypatch, clean_scroll_cache):
        drv = ScrollFakeDriver({8: "tok-8"}, scroll_shape="none")
        monkeypatch.setattr(ima_common, "run_cua", drv.run)
        monkeypatch.setattr(extractor.time, "sleep", lambda s: None)
        extractor.scroll_down(100, 42)
        assert drv.scrolls and "x" in drv.scrolls[-1] and "y" in drv.scrolls[-1]

    def test_token_scroll_failure_does_not_raise(self, monkeypatch,
                                                 clean_scroll_cache):
        drv = ScrollFakeDriver({5: "tok-s-5"})
        drv.scroll_fail = RuntimeError("cua-driver failed: exit 1")
        monkeypatch.setattr(ima_common, "run_cua", drv.run)
        monkeypatch.setattr(extractor.time, "sleep", lambda s: None)
        extractor.scroll_down(100, 42)  # 不得抛（提取循环自己重试）


class TestNavigateClickResilience:
    def test_kb_click_timeout_degrades_and_retries(self, monkeypatch):
        """KB 入口点击 TimeoutExpired 必须降级为本次尝试失败（历史死法：
        cua_call 15s 超时穿透炸整轮），attempt 循环重读重试。"""
        drv = FakeDriver031()
        drv.fail_kb_click_once = True
        monkeypatch.setattr(upd, "get_ima_main_window",
                            lambda: {"pid": 100, "window_id": 9})
        monkeypatch.setattr(upd, "subprocess", self._fake_subprocess())
        monkeypatch.setattr(upd, "is_on_kb_list", lambda kb: True)
        monkeypatch.setattr(upd.time, "sleep", lambda s: None)

        # FakeDriver031.run 的 click 里没有超时注入点——直接在其外层包：
        orig_run = drv.run

        def run_with_timeout(args, timeout=30):
            if (args[1] == "click"
                    and json.loads(args[2]).get("element_token") == "tok-B-30"
                    and drv.fail_kb_click_once):
                drv.fail_kb_click_once = False
                raise subprocess.TimeoutExpired(cmd=["cua-driver"] + args,
                                                timeout=timeout)
            return orig_run(args, timeout=timeout)

        monkeypatch.setattr(ima_common, "run_cua", run_with_timeout)
        assert upd.navigate_to_kb("AI") is True  # 第 2 次尝试成功，全程无异常上抛

    @staticmethod
    def _osascript_timeout_run():
        def _run(*a, **k):
            raise subprocess.TimeoutExpired(cmd=["osascript"], timeout=5)
        return _run

    @classmethod
    def _fake_subprocess(cls):
        # 替身必须带 TimeoutExpired：navigate 的 except 子句运行期要从
        # upd.subprocess 取该属性
        return SimpleNamespace(run=cls._osascript_timeout_run(),
                               TimeoutExpired=subprocess.TimeoutExpired)

    def test_osascript_activate_timeout_swallowed(self, monkeypatch):
        """navigate 尝试循环的 osascript activate 5s 超时不得炸（9/16 error log
        两条 traceback 即此死法）。全程抛超时也照样导航成功。"""
        drv = FakeDriver031()
        monkeypatch.setattr(ima_common, "run_cua", drv.run)
        monkeypatch.setattr(upd, "get_ima_main_window",
                            lambda: {"pid": 100, "window_id": 9})
        monkeypatch.setattr(upd, "subprocess", self._fake_subprocess())
        monkeypatch.setattr(upd, "is_on_kb_list", lambda kb: True)
        monkeypatch.setattr(upd.time, "sleep", lambda s: None)
        assert upd.navigate_to_kb("AI") is True

    def test_extractor_activate_ima_swallows_timeout(self, monkeypatch):
        monkeypatch.setattr(extractor.subprocess, "run",
                            self._osascript_timeout_run())
        monkeypatch.setattr(extractor.time, "sleep", lambda s: None)
        extractor.activate_ima()  # 不得抛
