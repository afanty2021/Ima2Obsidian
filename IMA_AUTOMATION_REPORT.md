# IMA 知识库自动化脚本总结报告

> **项目名称**: IMA 知识库文章自动提取与保存系统
> **创建时间**: 2026-05-12
> **技术栈**: Python, AppleScript, cua-driver, SQLite
> **状态**: 生产就绪

---

## 📋 目录

1. [项目概述](#项目概述)
2. [开发决策](#开发决策)
3. [技术架构](#技术架构)
4. [使用指南](#使用指南)
5. [性能分析](#性能分析)
6. [已知限制](#已知限制)
7. [未来优化](#未来优化)

---

## 项目概述

### 核心功能

本系统由两个协同工作的 Python 脚本组成，实现从 IMA 知识库批量提取微信文章 URL 并自动保存到 Obsidian 的完整工作流：

| 脚本 | 功能 | 输入 | 输出 |
|------|------|------|------|
| `ima_ax_extractor.py` | 从 IMA 提取文章 URL | IMA 知识库窗口 | SQLite 数据库 |
| `ima_obsidian_saver.py` | 保存文章到 Obsidian | 数据库记录 | Obsidian Markdown 文件 |

### 业务价值

- **效率提升**: 手动保存一篇文章需 30 秒，自动化后平均 15 秒/篇
- **规模化处理**: 支持单次处理数百篇文章
- **数据持久化**: SQLite 数据库确保不重复提取
- **结构化存储**: 自动按 `YYMMDD 标题.md` 格式命名并分类

---

## 开发决策

### 1. 为什么选择 cua-driver 而非纯 AppleScript？

**决策**: 使用 cua-driver + AX 树混合方案

**理由**:
- **精确性**: cua-driver 提供的 `element_index` 比 AppleScript 的位置点击更可靠
- **稳定性**: AX 树解析不依赖 UI 布局变化
- **后台能力**: cua-driver 支持不激活窗口的点击操作

**对比**:
```
AppleScript 点击:
  click at {x, y}  # 布局变化即失效

cua-driver 点击:
  click element_index=1234  # 稳定的元素引用
```

### 2. 为什么使用 SQLite 而非 JSON 文件？

**决策**: 使用 SQLite 存储文章元数据

**理由**:
- **去重效率**: `url` 字段的 UNIQUE 约束自动去重
- **查询性能**: 索引支持快速查询已存在文章
- **原子性**: 事务支持防止数据损坏
- **扩展性**: 易于添加新字段（`obsidian_saved`, `published_date`）

### 3. 为什么分两个脚本而非一个？

**决策**: 提取与保存分离

**理由**:
- **容错性**: 提取失败不影响已保存的文章
- **灵活性**: 可选择性保存部分文章
- **调试便利**: 可独立测试每个环节
- **资源管理**: 提取完成后可关闭 IMA，节省内存

### 4. 激活策略的权衡

**初始方案**: 每次操作前激活应用
**优化方案**: 仅在必要时激活

**优化效果**:
| 脚本 | 优化前激活频率 | 优化后激活频率 | 改善 |
|------|---------------|---------------|------|
| ima_ax_extractor.py | 11 次/页 | 2 次/页 | -82% |
| ima_obsidian_saver.py | 3 次/篇 | 2 次/篇 | -33% |

**保留激活的原因**:
- IMA 可能在其他 Space，需要激活才能获取 AX 树
- Web Clipper 快捷键必须由前台应用接收

---

## 技术架构

### 系统流程图

```mermaid
graph LR
    A[IMA 知识库] --> B[ima_ax_extractor.py]
    B --> C[cua-driver daemon]
    C --> D[AX 树解析]
    D --> E[SQLite 数据库]
    E --> F[ima_obsidian_saver.py]
    F --> G[浏览器 + Web Clipper]
    G --> H[Obsidian Vault]

    style B fill:#e1f5ff
    style F fill:#e1f5ff
    style E fill:#fff4e1
```

### 核心技术栈

#### 1. **ima_ax_extractor.py**

| 技术 | 用途 | 关键实现 |
|------|------|----------|
| **cua-driver** | 后台点击与滚动 | `run_cua_call("click", {...})` |
| **AX 树解析** | 文章卡片识别 | 解析 `tree_markdown` 中的 "公众号" 标记 |
| **AppleScript** | URL 提取 | 读取 `AXDocument` 属性 |
| **asyncio** | 异步等待 | `await asyncio.sleep(WAIT_CLICK_LOAD)` |

#### 2. **ima_obsidian_saver.py**

| 技术 | 用途 | 关键实现 |
|------|------|----------|
| **requests + 正则** | 发布日期提取 | 匹配 `create_time: JsDecode('...')` |
| **AppleScript** | 浏览器控制 | 模拟快捷键 `Cmd+Shift+O` |
| **文件监控** | 检测新保存的文件 | 对比保存前后的文件列表 |
| **Path 操作** | 文件移动与重命名 | `md_file.rename(new_path)` |

### 数据库 Schema

```sql
CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,              -- 微信文章 URL
    title TEXT,                             -- 文章标题
    knowledge_base TEXT,                    -- 知识库名称（AI/产品/...）
    extracted_at TIMESTAMP,                 -- 提取时间
    y_position INTEGER,                     -- 列表中的 Y 坐标
    status TEXT DEFAULT 'success',          -- 提取状态
    obsidian_saved INTEGER DEFAULT 0,       -- 是否已保存到 Obsidian
    obsidian_saved_at TEXT,                 -- 保存到 Obsidian 的时间
    published_date TEXT                     -- 文章发布日期（YYMMDD）
);

CREATE INDEX idx_url ON articles(url);
CREATE INDEX idx_kb ON articles(knowledge_base);
CREATE INDEX idx_obsidian_saved ON articles(obsidian_saved);
```

---

## 使用指南

### 前置条件

#### 系统要求
- macOS 12.0+（AX API 需要）
- Python 3.10+
- 辅助功能权限已授予

#### 软件安装
```bash
# 1. 安装 cua-driver
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/lume/scripts/install.sh)"

# 2. 安装 Python 依赖
pip install requests

# 3. 配置 Obsidian Web Clipper
# - 在 Chrome/Edge 安装 Obsidian Web Clipper 扩展
# - 配置快捷键为 Option+Shift+O (quick_clip)
# - 确保 Obsidian 应用已运行
```

### 基础使用

#### 步骤 1: 启动 cua-driver daemon

```bash
cua-driver serve &
```

#### 步骤 2: 提取文章 URL

```bash
# 从 "AI" 知识库提取（默认）
python3 ima_ax_extractor.py --src "AI"

# 从其他知识库提取
python3 ima_ax_extractor.py --src "产品"
```

**输出示例**:
```
════════════════════════════════════════════════════════
IMA AI 知识库提取器（AX Tree 版本）
════════════════════════════════════════════════════════

✅ 数据库: ima_articles.db (已有 116 篇)
✅ cua-driver daemon 运行中

查找 IMA 窗口（AI 知识库）...
✅ 窗口: PID=12345, window_id=2, 1200x800

当前窗口标题: AI - 知识库
✅ 确认在 AI 知识库列表页

───────────────────────────────────────────────────
第 1 页
───────────────────────────────────────────────────
  识别到 10 篇文章

  [1] 深度学习在 NLP 中的应用... (element 234)
    点击文章 (element 234)...
    ✅ URL: https://mp.weixin.qq.com/s/xxxxx...
    ✅ 标题: 深度学习在 NLP 中的应用
    ✅ 新文章已保存 (总计: 1)
```

#### 步骤 3: 保存到 Obsidian

```bash
# 保存到 "AI" 文件夹
python3 ima_obsidian_saver.py --des "AI" --limit 30

# 预览模式（不实际保存）
python3 ima_obsidian_saver.py --des "AI" --dry-run

# 使用 Safari 浏览器
python3 ima_obsidian_saver.py --des "AI" --browser safari
```

**输出示例**:
```
════════════════════════════════════════════════════════
IMA 微信文章 → Obsidian 自动保存器
════════════════════════════════════════════════════════

数据库统计:
  微信文章总数: 118
  已保存到 Obsidian: 39
  待保存: 79

Obsidian Vault: /Users/berton/Documents/Obsidian Vault
目标文件夹: AI
浏览器: Google Chrome

本次处理: 30 篇

[1/30] 提取日期...
    发布日期: 240512
  打开: 深度学习在 NLP 中的应用...
    触发 quick_clip (option+shift+o)...
    查找并重命名...
    移动: 微信文章... → AI/240512 深度学习在 NLP 中的应用.md...
    ✅ 完成
```

### 高级用法

#### 多知识库管理

```bash
# 提取不同知识库
python3 ima_ax_extractor.py --src "AI"
python3 ima_ax_extractor.py --src "产品"
python3 ima_ax_extractor.py --src "技术"

# 分别保存到不同文件夹
python3 ima_obsidian_saver.py --des "AI" --kb "AI"
python3 ima_obsidian_saver.py --des "产品" --kb "产品"
```

#### 数据库查询

```bash
# 查看各知识库统计
sqlite3 ima_articles.db "
SELECT knowledge_base, COUNT(*) as count
FROM articles
GROUP BY knowledge_base;
"

# 查找未保存的文章
sqlite3 ima_articles.db "
SELECT title, url
FROM articles
WHERE obsidian_saved = 0
LIMIT 10;
"
```

---

## 性能分析

### 时间开销分解

#### ima_ax_extractor.py

| 操作 | 时间 | 占比 |
|------|------|------|
| 等待页面加载 | 3.0s | 50% |
| 等待关闭 | 1.5s | 25% |
| 滚动加载 | 2.0s | 17% |
| 其他操作 | 0.5s | 8% |
| **单篇文章总计** | **6.0s** | 100% |

**理论效率**: 10 篇/分钟

#### ima_obsidian_saver.py

| 操作 | 时间 | 占比 |
|------|------|------|
| 页面加载等待 | 6.0s | 43% |
| Clipper 保存 | 4.0s | 29% |
| 文件出现等待 | 2.0s | 14% |
| 文章间隔 | 1.5s | 11% |
| 其他操作 | 0.5s | 3% |
| **单篇文章总计** | **14.0s** | 100% |

**理论效率**: 4.3 篇/分钟

### 优化建议

1. **缩短等待时间**（风险：可能降低成功率）
   - `WAIT_PAGE_LOAD`: 6.0s → 4.0s
   - `WAIT_CLIP_SAVE`: 4.0s → 3.0s

2. **并行处理**（复杂度：高）
   - 使用多浏览器窗口并行保存
   - 需要重写状态管理逻辑

---

## 已知限制

### 技术限制

| 限制 | 影响 | 缓解方案 |
|------|------|----------|
| **需要前台激活** | 运行时无法使用电脑 | 分批处理，或使用独立机器 |
| **依赖 UI 结构** | IMA 更新可能导致失效 | 维护 "公众号" 标记检测逻辑 |
| **浏览器快捷键** | 必须配置 Web Clipper | 提供配置检查脚本 |
| **单线程处理** | 大批量耗时较长 | 接受限制或重写为多线程 |

### 使用限制

1. **知识库识别**: 依赖窗口标题包含知识库名称
2. **去重机制**: 仅基于 URL，相同 URL 不同版本会跳过
3. **文件命名**: 标题过长会截断至 100 字符
4. **日期提取**: 部分文章可能无法提取日期，使用当前日期

---

## 未来优化

### 短期优化（1-2 周）

- [ ] 添加配置文件支持（替代硬编码路径）
- [ ] 实现断点续传（记录处理进度）
- [ ] 添加日志文件（记录错误和警告）
- [ ] 支持增量提取（仅提取新文章）

### 中期优化（1-2 月）

- [ ] Web 界面（简化操作）
- [ ] 多知识库并行提取
- [ ] 智能等待时间（根据网络状况调整）
- [ ] 文章内容去重（基于相似度）

### 长期优化（3-6 月）

- [ ] 完全后台化（使用浏览器 DevTools Protocol）
- [ ] 云端部署（远程运行）
- [ ] AI 辅助分类（自动识别文章类型）
- [ ] 全文搜索集成（Obsidian 插件）

---

## 附录

### A. 常见问题

**Q: 提取失败怎么办？**
```bash
# 检查 cua-driver 是否运行
pgrep -f "cua-driver serve"

# 检查辅助功能权限
# 系统设置 → 隐私与安全性 → 辅助功能
```

**Q: Web Clipper 没有触发？**
- 检查快捷键配置：`Option+Shift+O`
- 确保浏览器在前台
- 尝试手动触发快捷键测试

**Q: 文件没有移动到指定文件夹？**
- 检查 Obsidian Vault 路径是否正确
- 确保有文件夹创建权限

### B. 故障排除

```bash
# 查看数据库状态
sqlite3 ima_articles.db ".schema"
sqlite3 ima_articles.db "SELECT * FROM articles LIMIT 5;"

# 重置特定知识库
sqlite3 ima_articles.db "DELETE FROM articles WHERE knowledge_base = 'AI';"

# 导出数据
sqlite3 ima_articles.db ".output articles.json" ".mode json" "SELECT * FROM articles;"
```

### C. 相关文件

```
cua/
├── ima_ax_extractor.py         # 提取脚本
├── ima_obsidian_saver.py       # 保存脚本
├── ima_articles.db             # SQLite 数据库
└── IMA_AUTOMATION_REPORT.md    # 本报告
```

---

**报告版本**: 1.0.0
**最后更新**: 2026-05-12
**作者**: AI Assistant (PAI 4.0.3)
