# 前端架构：React 开发规范

## 1. 技术选型

前端统一采用 **React + Vite + TypeScript**，不再为新增功能编写原生页面脚本。

| 层 | 技术 | 责任 |
| --- | --- | --- |
| 构建 | Vite | 本地开发、生产构建与静态资源打包 |
| UI | React | 页面、组件、表单、表格、状态展示 |
| 类型 | TypeScript | API 入参、返回值与页面状态约束 |
| 服务端 | Python 标准库 HTTP 服务 | 业务 API、数据处理与静态构建产物托管 |
| 数据库 | SQLite | 当前本地关键词数据、审核、分类与指标 |

当前不引入大型 UI 组件库。待多页面、复杂筛选和权限系统完善后，统一评估引入 Ant Design；不得在不同页面混用多个 UI 体系。

## 2. 目录约定

```text
frontend/
  src/
    App.tsx              # 关键词工作台页面组合
    api.ts               # 所有后端 HTTP 调用
    types.ts             # API 与视图模型类型
    styles.css           # React 页面专属样式
  package.json
  vite.config.ts
web/                     # Vite 构建产物，由 Python 服务托管
src/seo_control/         # Python API 与领域逻辑
tests/                   # 后端契约、领域规则与前端静态契约测试
```

`web/` 是构建产物，不作为新的业务前端源码目录。新增前端功能必须修改 `frontend/src/`，再执行构建。

## 3. 组件与状态边界

- 页面状态：当前项目、扩展任务、审核结果、关键词库列表。
- 业务 API：只在 `api.ts` 中封装；组件不得散落拼接 URL。
- 可复用组件：任务日志、关键词结果表、审核面板、关键词库、KD 评分器。
- 后端仍是唯一数据事实来源；浏览器本地状态只用于当前交互和项目 ID 缓存。
- 所有删除、清空等操作必须有二次确认，后端以项目范围做校验并执行软删除。

## 4. 数据真实性规则

- Google Suggest 只能发现关键词，不能生成真实 VOL。
- 未接 Google Ads CSV/API 时，页面只显示“需求预估指数”，不得标成 VOL。
- AI 审核是辅助分流；审核结果、置信度、模型/规则来源必须保存，不能自动物理删除关键词。
- KD 仅在输入 VOL 与 SERP 指标后计算；缺失数据时显示待补充。

## 5. 开发与构建

```powershell
cd frontend
npm install
npm run dev       # Vite 开发模式
npm run build     # 输出到 ../web，由 http://127.0.0.1:8000 托管
```

每次前端变更至少执行：

```powershell
npm run build
python -m unittest discover -s tests
```

## 6. 后续演进

当页面增加项目切换、权限、任务队列、图表和多模块路由时，引入：

- React Router：模块路由与深链接。
- TanStack Query：请求缓存、轮询、失效刷新。
- Zustand：跨页面的项目与筛选状态。
- ECharts：VOL、KD、排名、GSC 趋势图表。

后端在需要异步任务、并发队列与多用户部署时，再从当前本地 HTTP 服务迁移至 FastAPI；前端 API 契约保持兼容。
