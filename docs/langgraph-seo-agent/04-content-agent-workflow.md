# 模块 4：内容智能工作流

## 目标

以 LangGraph 将现有“竞品研究 → Brief → 大纲 → 正文 → 配图 → 发布”串为可学习、可暂停、可审稿的内容生产图。

## 节点图

```text
validate_project
  → load_context
  → retrieve_memories
  → research_competitors (可跳过)
  → extract_style_cards
  → plan_blueprint
  → approval_blueprint (可配置)
  → draft_materials
  → write_article
  → generate_tags_links_images
  → quality_review
  → rewrite_targeted (最多两次)
  → prepare_publish
  → approval_publish
  → publish_by_backend
```

## 节点输入输出

| 节点 | 输入 | 输出 |
|---|---|---|
| `load_context` | project、title、keyword | 当前知识库、现有版本、发布设置 |
| `retrieve_memories` | 标题、关键词、意图 | 写法/品牌/事实/反馈记忆 ID |
| `plan_blueprint` | 记忆与竞品摘要 | 5–7 个自然 H2、每段目的、资料分配 |
| `write_article` | 蓝图、品牌和事实 | 版本化 Markdown 草稿 |
| `quality_review` | 草稿、规则、来源 | 评分、问题、定向重写指令 |
| `prepare_publish` | 通过 QA 的草稿 | tags、图片、内链、发布门禁结果 |

## 写作质量规则

- H2 数量按内容自然决定，通常约 5–7；不强制 FAQ。
- 产品/公司资料只在 1–2 个最相关 H2 中自然出现。
- 关键词仅首次有意义出现时适度加粗；不得在标题、表格或链接中堆砌。
- 表格必须生成真实 HTML `<table>`，图片为 800×600 WebP 并带 alt 和图片说明。
- 无匹配权威来源时不输出“参考资料”栏目。

## 审稿评分表

`搜索意图、结构深度、具体性、品牌自然度、事实可验证性、原创表达、表格/图片可发布性` 各 0–5 分。低于项目阈值时只重写问题节点，不全篇无限重写。

## 验收标准

- 文章生成时可查看蓝图、使用记忆和审稿原因。
- 失败或拒绝蓝图后不破坏已存在文章。
- 单篇最多两次自动定向重写，超过后要求人工决定。
