# P3 / M6：内容生产 Agent

## 目标

把现有内容生成能力迁入项目隔离、可暂停、可恢复、可审批的 Agent 主链路：

```text
读取项目上下文
→ 召回学习记忆
→ 生成内容蓝图
→ 人工审批蓝图
→ H2 分段写作与组装
→ QA
→ 最多两次定向重写与复审
→ 生成人工可读的生成依据报告
```

发布前门禁和 WordPress 公开发布属于 P4，本阶段不得绕过现有发布审批。

## 输入与输出

输入：`project_id`、`content_asset_id`、写作模型、审核模型、目标读者、业务目标及可选的一方资料。

输出：审批后的内容蓝图、版本化正文、QA 结果、定向重写版本、使用记忆与来源依据报告。

## 节点与检查点

| 节点 | 持久化结果 | 中断后恢复规则 |
|---|---|---|
| `load_project_context` | 项目和内容资产 | 已完成不重复读取 |
| `retrieve_project_memories` | 记忆 ID 与选择理由 | 从检查点继续 |
| `generate_content_blueprint` | Brief、大纲、原资产状态 | 重试时按持久化结果幂等 |
| `approve_blueprint` | 独立审批请求 | 未审批不执行写作 |
| `generate_article` | 草稿版本 ID | 已有本任务草稿不重复写作 |
| `review_article` | QA、模型、分数与定向指令 | 从当前草稿复审 |
| `rewrite_targeted_1/2` | 新草稿版本与父版本 | 最多两次 |
| `generation_basis_report` | 记忆、来源、选择理由和风险 | 与 Agent 任务唯一绑定 |

## 审批与回滚

- 蓝图生成后 `agent_jobs.status=waiting_approval`。
- 拒绝蓝图时取消 Agent 任务，并恢复生成前的 `current_brief_id`、`current_outline_id` 和内容状态；候选蓝图保留为审计历史。
- 批准蓝图后只恢复当前任务，不接受其他项目或其他任务的审批。
- 发布审批与蓝图审批不可互相替代。

## QA 与重写

- QA 状态为 `approved` 时完成 P3。
- 有定向指令时每次最多应用两条，重写后必须重新 QA。
- 同一 Agent 任务最多创建两个定向重写版本。
- 两次后仍未通过，任务进入 `waiting_input`，保留当前草稿供人工处理，不自动发布。

## 工具边界

本阶段实现三个白名单工具：

- `generate_content_blueprint`
- `generate_article`
- `review_article`

工具必须重新校验 `project_id`、`agent_job_id`、`content_asset_id`，并写入 `agent_tool_audits`。模型不能直接访问数据库、AI 密钥或 WordPress。

## 验收标准

1. 蓝图未批准前不会创建正文。
2. 拒绝蓝图恢复原资产指针，历史文章不受影响。
3. 审批通过后可从检查点继续生成。
4. QA 最多触发两次定向重写，超过后等待人工处理。
5. 每个节点记录输入、输出、模型、耗时和错误。
6. 每篇 Agent 草稿拥有可解释生成依据报告。
7. 项目隔离、重试、暂停、取消和服务重启恢复测试通过。
