# 保存器详细文档 — ima_obsidian_saver.py

## 功能说明

从数据库读取文章 URL，通过浏览器 + Obsidian Web Clipper 保存到 Obsidian Vault。

---

## 工作原理

### 1. 日期提取
从微信文章页面 HTML 提取发布日期，支持多种格式：

```javascript
// 方法1: create_time: JsDecode('YYYY-MM-DD HH:MM')
// 方法2: ori_create_time / create_timestamp (Unix 时间戳)
// 方法3: var createTime = 'YYYY-MM-DD HH:MM'
// 方法4: publish_time (URL 编码的 JSON 中)
```

### 2. 保存流程
```
提取日期 → 打开文章 → 触发 Web Clipper → 等待保存 → 查找文件 → 重命名 → 关闭标签
```

### 3. 文件重命名
- 格式: `YYMMDD title.md`
- 自动清理非法字符
- 可选移动到指定文件夹

---

## 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `WAIT_PAGE_LOAD` | 6.0s | 页面加载等待时间 |
| `WAIT_CLIP_SAVE` | 4.0s | Web Clipper 保存等待时间 |
| `WAIT_FILE_APPEAR` | 2.0s | 文件出现等待时间 |
| `DEFAULT_LIMIT` | 1300 | 每次最多处理文章数 |

---

## 命令行参数

```bash
--limit <数量>         # 每次处理文章数
--dry-run             # 预览模式
--browser <浏览器>     # chrome/edge/safari
--mode <模式>          # quick/clipper
--des <文件夹>         # 目标文件夹名称
```

---

## 浏览器快捷键

| 浏览器 | Quick Clip | Clipper |
|--------|-----------|---------|
| Chrome | Option+Shift+O | Cmd+Shift+O |
| Edge | Option+Shift+O | Cmd+Shift+O |
| Safari | Option+Shift+O | Cmd+Shift+O |

---

## 依赖条件

1. **浏览器** 已安装 Obsidian Web Clipper 扩展
2. **Obsidian** 应用运行并打开目标 Vault
3. **Web Clipper** 已连接到 Obsidian

---

## 文件查找策略

### 第一步：精确匹配
文件名与标题匹配的最近创建文件

### 第二步：新文件检测
不存在于保存前快照中的新文件

---

## 常见问题

**未找到保存的文件**
- 检查 Obsidian 是否运行
- 确认 Web Clipper 已连接
- 增加 `WAIT_CLIP_SAVE` 时间

**文件名过长**
- 自动截断到 100 字符
- 非法字符替换为 `-`

**目标文件夹不存在**
- 自动创建
