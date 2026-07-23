"""find_and_rename 须能认领 Web Clipper 畸形嵌套目录深处的 .md。

背景：微信验证页过渡态下，Web Clipper 偶发把含 \\n 的页面 title 当文件名，生成多层
嵌套目录（\\n 被当路径分隔），深处是内容完好的 .md（见 id=2913）。find_and_rename
原用 glob("*.md") 非递归只扫顶层，漏掉深层 → 永不认领 → 标"未找到保存的文件"。
改 CLIPPINGS_DIR 用 rglob 修复；VAULT_DIR 保持 glob 避免扫全 vault 拖慢。
"""
import ima_obsidian_saver as saver


class TestMalformedNestedDir:
    def test_claims_md_nested_by_newline_title(self, tmp_path, monkeypatch):
        """含 \\n 的 title 被 Web Clipper 存成嵌套目录，深处 .md 仍应被认领到目标文件夹"""
        vault = tmp_path / "Vault"
        clippings = vault / "Clippings"
        clippings.mkdir(parents=True)
        monkeypatch.setattr(saver, "VAULT_DIR", vault)
        monkeypatch.setattr(saver, "CLIPPINGS_DIR", clippings)

        title = "朋友A君的儿子高考语文成绩136分录取了"
        # 模拟 2913：title 含 \n，被文件系统拆成嵌套目录，末端 .md 文件名是正文片段
        deep = clippings / "朋友A君的儿子" / "n" / "n这所学校2016年"
        deep.mkdir(parents=True)
        (deep / "片段.md").write_text("正文内容", encoding="utf-8")

        renamed, _ = saver.find_and_rename_in_vault(
            title, "260723", existing_files=set(), target_folder="Andrew"
        )
        assert renamed is True
        moved = list((vault / "Andrew").glob("*.md"))
        assert len(moved) == 1  # 深层 .md 被捞到 Andrew/

    def test_skips_weixin_verify_page_clipping(self, tmp_path, monkeypatch):
        """Web Clipper 把验证页存成 md（title=微信公众平台/含环境异常），不应被认领为文章。

        兜底防线：无论验证页检测（handle_verify_page）有没有命中、点没点掉「去验证」，
        只要落盘文件本身是验证页内容，find_and_rename 就不能认领它当文章（防错误数据）。
        """
        vault = tmp_path / "Vault"
        clippings = vault / "Clippings"
        clippings.mkdir(parents=True)
        monkeypatch.setattr(saver, "VAULT_DIR", vault)
        monkeypatch.setattr(saver, "CLIPPINGS_DIR", clippings)

        verify_md = clippings / "微信公众平台.md"
        verify_md.write_text(
            '---\ntitle: "微信公众平台"\n---\n环境异常\n当前环境异常，完成验证后即可继续访问',
            encoding="utf-8",
        )

        renamed, _ = saver.find_and_rename_in_vault(
            "某文章标题比较长用于测试", "260724", existing_files=set(), target_folder="X"
        )
        assert renamed is False  # 验证页落盘不被认领
        assert verify_md.exists()  # 仍留在 Clippings，未被移动
