# 模块 1：任务状态与恢复

## 目标

把内容研究、记忆学习、正文生成、审稿和发布拆为可追踪的后台任务。用户切换页面或服务异常后，任务可以从最后成功节点恢复。

## 新增数据模型

```text
agent_jobs
  id, project_id, content_asset_id, requested_action, status,
  current_node, workflow_version, input_json, result_json,
  error_summary, created_at, started_at, completed_at

agent_steps
  id, job_id, node_name, attempt, status, model_provider, model,
  input_summary, output_ref_json, duration_ms, error_summary,
  started_at, completed_at

approval_requests
  id, project_id, job_id, approval_type, payload_json,
  status(pending|approved|rejected|expired), decided_at, decided_by
```

## 状态机

`queued → planning → running → waiting_input → waiting_approval → retrying → completed | failed | cancelled`

- `waiting_input`：缺少标题、知识库或模型配置。
- `waiting_approval`：等待用户确认蓝图、草稿发布或公开发布。
- `retrying`：只用于网络、限流、临时爬取失败；每节点最多 3 次。
- `failed`：保留最后成功节点和具体错误，可点击“从此节点重试”。

## 后端接口

| 接口 | 用途 |
|---|---|
| `POST /api/agent-jobs` | 创建任务 |
| `GET /api/agent-jobs?project_id=` | 当前项目任务列表 |
| `GET /api/agent-jobs/:id?project_id=` | 任务、节点和输出详情 |
| `POST /api/agent-jobs/:id/retry` | 从失败节点恢复 |
| `POST /api/agent-jobs/:id/cancel` | 请求取消未完成节点 |
| `POST /api/approvals/:id` | 批准或拒绝审批 |

## 实现规则

1. 每个节点开始和结束均写入 `agent_steps`，不能只写内存日志。
2. 内容、图片、发布等现有表仍是真实产出来源；`result_json` 只保存关联 ID。
3. 服务启动时将异常中断的 `running` 任务标记为 `retrying` 或 `failed`，不得静默丢失。
4. 后台任务不得依赖浏览器页面存活。

## 验收标准

- 执行任务后刷新页面，仍能看到准确状态和历史步骤。
- 模拟某节点异常后，重试不重复已完成节点。
- 所有任务、步骤、审批请求均隔离到项目。
