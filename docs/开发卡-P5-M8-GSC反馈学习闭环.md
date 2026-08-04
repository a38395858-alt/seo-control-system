# 开发卡：P5 / M8 GSC 反馈学习闭环

## 目标

把已发布内容的 GSC 查询与网页表现接入可恢复 Agent，并形成可追溯、可治理、可召回的表现记忆：

```text
读取项目上下文
→ 采集已发布网页的 GSC 表现快照
→ 判断观察期与证据是否合格
→ 更新表现记忆与来源证据
→ 后续内容 Agent 按主题召回
→ 对比记忆辅助与无记忆基线
```

GSC 只描述发布后的搜索表现并补充锚文本，不作为竞品写法学习的主要来源，也不证明写作方法与排名变化存在因果关系。

## 模块边界

- `application/gsc_feedback` 负责项目边界、发布 URL 匹配、快照写入、证据门槛、表现记忆与时效治理。
- `AgentToolService` 只暴露 `capture_gsc_performance` 与 `update_memory_governance` 两个服务端工具；模型不能读取 SQLite、OAuth token、Cookie 或 API Key。
- `web.py` 负责 API、持久 Agent 任务、步骤、断点和任务调度，不重复实现 GSC 学习规则。
- GSC OAuth 与浏览器采集仍属于数据接入；反馈学习任务只读取当前项目已经入库的数据。

## 持久化工作流

工作流标识：

- `requested_action = gsc_feedback_learning`
- `workflow_version = gsc-feedback-v1`

节点：

1. `load_project_context`
2. `capture_gsc_performance`
3. `update_memory_governance`
4. `completed`

每个工具调用写入 Agent 步骤和工具审计。任务失败后保留最后一次已提交检查点，可从失败节点重试；服务启动时把中断的运行任务恢复到队列。

## 证据门槛

- 第一次快照状态为 `observing`，不生成表现记忆。
- 与前一次有效快照至少间隔 7 天。
- 当前网页至少有 100 次展示和 3 个查询词。
- 达标后状态为 `qualified`，否则为 `insufficient`。
- 表现记忆固定使用 `inference_level = observed` 和 `causality = descriptive_only`。
- 90 天未验证标记为 `needs_review`，180 天未验证标记为 `stale`。

## 记忆来源与使用关系

每条表现记忆必须写入 `content_learning_memory_sources`：

- `source_type = gsc`
- `source_id = snapshot_id`
- 来源 URL 为公开发布 URL
- 保存快照内容哈希、采集时间和证据摘要

生成表现记忆时不得反向创建 `content_memory_links`。只有未来文章实际召回并使用该记忆时，内容 Agent 才能建立使用关系。

## 效果评估

已发布文章显示：

- `memory_assisted`：生成时是否使用项目记忆
- `evaluation_group`：`memory_assisted` 或 `no_memory_baseline`
- `used_memories`：记忆 ID、类型、主题、选择原因和来源快照
- 内容质量分、GSC 表现分与综合观察分

至少 5 篇具备合格 GSC 数据后才显示 Spearman 相关系数；该系数只能用于描述性观察，不得宣称因果。

## 验收标准

- 学习 API 返回 `job_id`，任务中心可查看 3 个节点和工具审计。
- 首次快照不创建记忆，第二次合格快照创建或更新表现记忆。
- 表现记忆具有来源证据、置信度、时效、适用范围和观察级推断标记。
- 来源文章不会被错误标记为使用了未来产生的表现记忆。
- 文章效果列表可区分记忆辅助与无记忆基线，并展示具体使用依据。
- 跨项目不能读取任务、快照、记忆或来源。
- API、步骤和工具审计不包含 OAuth token、Cookie、密码或 API Key。
- Python 测试、React 构建和浏览器验收通过。

