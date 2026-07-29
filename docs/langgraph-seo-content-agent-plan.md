# SEO 内容学习智能体与 LangGraph 接入规划书

本规划已拆成可独立开发、测试和验收的模块。实施时每次只完成一个模块及其验收，不一次性重构现有 SEO 系统。

## 模块目录与实施顺序

| 顺序 | 模块 | 目的 | 前置依赖 |
|---|---|---|---|
| 0 | [架构与边界](./langgraph-seo-agent/00-architecture-and-boundaries.md) | 确定 LangGraph、现有后端和 Hermes 的职责 | 无 |
| 1 | [任务状态与恢复](./langgraph-seo-agent/01-task-state-and-recovery.md) | 让任务可追踪、暂停、恢复、审批 | 模块 0 |
| 2 | [内容学习记忆](./langgraph-seo-agent/02-content-learning-memory.md) | 建立竞品写法、品牌、事实和反馈记忆 | 模块 1 |
| 3 | [模型路由与切换](./langgraph-seo-agent/03-model-routing-and-switching.md) | GPT/DeepSeek 随时切换、可审计协作 | 模块 1 |
| 4 | [内容智能工作流](./langgraph-seo-agent/04-content-agent-workflow.md) | 蓝图、生成、审稿、定向重写 | 模块 1–3 |
| 5 | [发布审批与 WordPress](./langgraph-seo-agent/05-publishing-approval.md) | 审批门禁、草稿/公开发布、回写链接 | 模块 1、4 |
| 6 | [前端与 API](./langgraph-seo-agent/06-frontend-and-api.md) | 页面入口、任务可视化、模型选择与接口 | 模块 1–5 |
| 7 | [评测、上线与 Hermes](./langgraph-seo-agent/07-evaluation-rollout-and-hermes.md) | 质量评测、灰度、可选 Hermes 接入 | 模块 4–6 |

## 总验收目标

系统必须在不替换现有项目、知识库、内容和发布能力的前提下，实现：长期学习、GPT/DeepSeek 可切换、任务可恢复、发布可审批、每一步可追溯。

## 推荐开发顺序

先实施模块 0、1、2、3，再开始模块 4 的正文智能工作流。GSC 反馈和 Hermes 都放在功能稳定、内容质量通过评测后再接入。
