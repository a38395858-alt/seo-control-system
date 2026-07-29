# 模块 0：架构与边界

## 目标

在不推翻现有 `src/seo_control/web.py`、SQLite 与 React 页面的前提下，引入 LangGraph 作为智能工作流层。

## 职责划分

```text
React 页面 → 现有 HTTP API → SQLite / 业务服务
                              ↓
                       LangGraph 工作流
                              ↓
              GPT-5.6 / DeepSeek / 可选 Hermes
```

| 层 | 负责 | 不负责 |
|---|---|---|
| 现有后端 | 项目隔离、CRUD、文件、配置、发布执行、真实数据 | 复杂多轮编排 |
| LangGraph | 多步任务、暂停、恢复、模型路由、审批节点 | 保存凭据、绕过权限、直接公开发布 |
| GPT/DeepSeek | 分析、规划、生成、审稿 | 最终权限判断、数据库直写 |
| Hermes（可选） | 受控网页研究和结构抽取 | WordPress、数据库、跨项目资料 |

## 强制边界

1. 每个工作流状态都必须有 `project_id`；每次工具调用后端二次校验所属项目。
2. 模型只可调用白名单业务工具，不接触数据库连接、文件系统路径或加密凭据。
3. WordPress 密码只由后端解密和使用；模型只能请求“准备发布”或“执行已批准发布”。
4. LangGraph 失败不会影响已有内容版本、图片和发布记录。

## 预计改动范围

- 新目录：`src/seo_control/application/agent_workflows/`
- 依赖：`langgraph`，不替换现有 HTTP server。
- 新增任务与审批数据表（见模块 1）。

## 验收标准

- 不接入任何模型也能正常使用现有关键词、内容、图片和 WordPress 功能。
- 任意工作流工具调用均能证明当前 `project_id` 已校验。
- 不存在模型可直连 WordPress 或 SQLite 的路径。
