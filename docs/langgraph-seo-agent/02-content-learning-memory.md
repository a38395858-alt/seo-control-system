# 模块 2：内容学习记忆

## 目标

把竞品、公司资料、权威事实、GSC 表现和人工修改转换为项目可检索的长期记忆；学习写作策略，不复制竞品原文。

## 记忆类型

| 类型 | 来源 | 生成时作用 |
|---|---|---|
| 竞品写法卡 | 成功采集的同行文章 | 标题、结构、论证、表格、CTA 方法 |
| 品牌记忆 | `project_knowledge_documents` | 产品/公司事实、页面 URL、表达边界 |
| 权威事实 | 已验证来源库 | 有条件地支持论点与外链 |
| 表现记忆 | GSC 与发布数据 | 找出有效页面结构与选题方向 |
| 编辑偏好 | 人工评分与修改 | 语气、H2、表格、配图与禁用表达 |

## 新增表

```text
content_learning_memories
  id, project_id, memory_type, topic, summary, evidence_json,
  source_url, source_content_hash, embedding, quality_score,
  status(active|disabled|superseded), created_at, updated_at

content_memory_links
  id, content_asset_id, memory_id, role(style|brand|fact|performance),
  relevance_score, selected_by_model, selected_by_user, created_at

content_feedback
  id, project_id, content_asset_id, draft_id, feedback_type,
  rating, note, before_excerpt, after_excerpt, created_by, created_at
```

## 写法卡生成

1. Python 爬虫或 Hermes 取得可读正文。
2. DeepSeek 提取固定 JSON：读者意图、开头策略、H2 模式、信息格式、优势、遗漏点。
3. GPT 复核相关性、去除不适合品牌的策略，并打质量分。
4. 写入记忆卡并保存来源 URL、内容 hash、采集时间。
5. 原文只用于分析；写法卡不得保存可直接拼接发布的竞品段落。

## 检索规则

- 每次文章生成最多注入 3–5 张相关写法卡、1–2 个品牌事实包和必要权威事实。
- 任何事实必须能回到当前项目的知识库或来源；无依据时标记验证需求或不写。
- 第一阶段用 SQLite FTS/标签；每项目记忆超过约 1,000 条后再引入 Qdrant 索引。

## 验收标准

- 内容详情可看到本篇使用了哪些记忆、来自哪里、为什么选中。
- 用户可禁用低质量记忆，后续生成不会再召回。
- 文章不出现竞品长句复用；来源只用于策略学习和验证。
