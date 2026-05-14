# 提取器详细文档 — ima_ax_extractor.py

## 功能说明

从 IMA AI 知识库批量提取文章 URL，保存到 SQLite 数据库。

---

## 工作原理

### 1. AX 树解析
- 通过 `cua-driver` 获取窗口 AX 树
- 识别文章卡片特征结构：
  ```
  AXImage (缩略图)
  AXGroup > AXStaticText = "文章标题"
  AXImage (公众号图标)
  AXStaticText = "公众号"
  ```

### 2. 提取流程
```
get_window_state → 解析文章 → 点击文章 → 等待加载 → 提取 URL → 关闭 → 下一篇
```

### 3. URL 提取
- AppleScript 读取窗口 `AXDocument` 属性
- 标题从窗口标题提取

---

## 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `WAIT_CLICK_LOAD` | 3.0s | 点击后等待加载时间 |
| `WAIT_AFTER_CLOSE` | 1.5s | 关闭文章后等待时间 |
| `WAIT_SCROLL` | 2.0s | 滚动后等待时间 |
| `MAX_PAGES` | 65 | 最大翻页数 |
| `MAX_CONSECUTIVE_SEEN` | 40 | 连续遇到已存在文章后停止 |

---

## 命令行参数

```bash
--src <知识库名>    # 默认: AI
```

---

## 依赖条件

1. **cua-driver daemon** 运行中
2. **IMA** 已打开并位于目标知识库列表页
3. **辅助功能权限** 已授权

---

## 数据库操作

### 写入
```sql
INSERT OR IGNORE INTO articles (url, title, knowledge_base, status)
VALUES (?, ?, ?, 'success')
```

### 去重
- URL 唯一索引
- 标题去重（同页面内）

---

## 常见问题

**未识别到文章卡片**
- 确认在知识库列表页
- 检查窗口元素数 > 100

**点击失败**
- 窗口可能在其他 Space，自动激活重试

**URL 提取失败**
- 文章可能未完全加载，增加 `WAIT_CLICK_LOAD`
