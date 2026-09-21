# 数据库文档 — ima_articles.db

## 结构说明

SQLite 数据库，存储从 IMA 提取的文章信息。

---

## 表结构

### articles 表

```sql
CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,              -- 文章 URL（唯一索引）
    title TEXT,                             -- 文章标题
    knowledge_base TEXT,                    -- 知识库名称
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- 提取时间
    y_position INTEGER,                     -- Y 位置（预留）
    status TEXT DEFAULT 'success',          -- 状态：success=正常 / deleted=已被发布者删除(永久跳过)
    obsidian_saved INTEGER DEFAULT 0,       -- 是否已保存到 Obsidian
    obsidian_saved_at TEXT,                 -- 保存时间
    published_date TEXT                     -- 发布日期 (YYMMDD)
);
```

### dead_articles 表（2026-09-21 提取侧拦截页黑名单）

```sql
CREATE TABLE dead_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title_norm TEXT UNIQUE NOT NULL,        -- 归一化标题（normalize_title_for_compare）
    title TEXT,                             -- 原始列表标题
    reason TEXT,                            -- 发布者删除 / 违规不可查看 / 账号被屏蔽
    url TEXT,                               -- 尽力而为（launchd 下拦截页 URL 读不到）
    marked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

提取器在幻影重试耗尽后探测文章页 AX 文本（<100 字且命中 `ima_common.DELETED_REASON_MAP`
——与 saver `_deleted_reason` 同表同阈值），判定为微信拦截页（违规/发布者删除/账号
屏蔽）时写入，页级预检与逐卡走查永久跳过。以标题为键是因为 launchd 下拦截页读不到
URL（`articles.status='deleted'` 的 URL 键机制管 saver 侧，两者互补）。误标解除：

```sql
SELECT * FROM dead_articles;
DELETE FROM dead_articles WHERE title_norm = (SELECT title_norm FROM dead_articles WHERE title LIKE '%…%');
```

---

## 索引

```sql
CREATE INDEX idx_url ON articles(url);
CREATE INDEX idx_kb ON articles(knowledge_base);
CREATE INDEX idx_obsidian_saved ON articles(obsidian_saved);
```

---

## 常用查询

### 统计信息
```sql
-- 总文章数
SELECT COUNT(*) FROM articles;

-- 知识库数量
SELECT COUNT(DISTINCT knowledge_base) FROM articles;

-- 微信文章统计
SELECT
    COUNT(*) FILTER (WHERE url LIKE '%mp.weixin.qq.com%') AS wechat,
    COUNT(*) FILTER (WHERE obsidian_saved = 1) AS saved
FROM articles;
```

### 待保存文章
```sql
SELECT id, url, title, knowledge_base
FROM articles
WHERE obsidian_saved = 0
  AND status = 'success'
  AND url LIKE '%mp.weixin.qq.com%'
ORDER BY id ASC
LIMIT ?;
```

### 已删除文章（永久跳过）

saver 检测到「文章已被发布者删除」时把 status 改为 'deleted'，自动从所有
`WHERE status='success'` 查询消失（saver/reclaim/incremental 共 4 处一致），
不再重试、不计失败。如需重新尝试，手工改回 'success'：

```sql
SELECT id, url, title, knowledge_base
FROM articles
WHERE status = 'deleted'
  AND url LIKE '%mp.weixin.qq.com%'
ORDER BY id DESC;

-- 撤销删除标记（让某篇重新进入待保存队列）
UPDATE articles SET status = 'success' WHERE id = ?;
```

### 按知识库分组
```sql
SELECT
    knowledge_base,
    COUNT(*) AS total,
    SUM(obsidian_saved) AS saved
FROM articles
GROUP BY knowledge_base
ORDER BY total DESC;
```

---

## 维护操作

### 清理重复
```sql
DELETE FROM articles WHERE id NOT IN (
    SELECT MIN(id) FROM articles GROUP BY url
);
```

### 重置保存状态
```sql
UPDATE articles SET obsidian_saved = 0, obsidian_saved_at = NULL;
```

---

## 文件位置

```
/Users/berton/Github/Ima2Obsidian/ima_articles.db
```
