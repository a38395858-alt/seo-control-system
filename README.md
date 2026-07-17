# SEO 中控系统

本项目是面向 SEO 团队的本地关键词工作台。当前一期聚焦“关键词挖掘 → 审核 → 分类 → 入库 → 机会评估”的闭环，后续可扩展至内容生产、发布、GSC、排名跟踪和内链优化。

## 当前功能

### 关键词挖掘

- 输入一个或多个种子关键词，基于 Google Suggest 下拉建议递归扩展。
- 支持语言、国家/地区、最大请求数和递归层数控制。
- 单个下拉请求失败时保留已有结果，并在任务日志显示网络、限流或响应错误。
- Google Suggest 仅用于发现关联词，不会伪造为真实搜索量。

### SEO 审核与分类

- 对扩展词判断是否适合做 SEO 内容、是否与种子词同主题。
- 输出搜索意图、建议动作、审核原因和置信度。
- 未配置服务端 AI 时使用本地规则初筛；配置 OpenAI/DeepSeek 兼容服务后可使用 AI 语义审核。
- 自动归类为对比评测、购买服务、教程问答、商业调研或主题内容。

### 关键词库

- 审核完成后将符合条件的关键词写入 SQLite 关键词库。
- 保存关键词来源、种子词、审核历史、分类、搜索意图和需求预估指数。
- 支持关键词单条删除、当前项目清空与 CSV 导出。
- 删除使用软删除，保留数据恢复和审计空间。

### VOL、KD 与机会分

- 可输入 Google Ads 导出的真实 VOL 和 SERP 指标，计算关键词难度（KD）与机会分。
- KD 综合域名强度、引用域、标题匹配率、大站占比和意图竞争度。
- 未接入 Google Ads API 时，页面只显示“需求预估指数”，不会将其标为真实 VOL。

## 技术栈

- 前端：React + Vite + TypeScript
- 后端：Python 标准库 HTTP 服务
- 数据库：SQLite
- 构建产物：`web/`，由本地服务 `http://127.0.0.1:8000` 托管

详细规范见 [前端架构-React开发规范](docs/前端架构-React开发规范.md)。

## 启动方式

```powershell
# 安装并构建前端
cd frontend
npm install
npm run build

# 回到项目根目录，启动服务
cd ..
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
python -m seo_control serve --host 127.0.0.1 --port 8000
```

浏览器打开：<http://127.0.0.1:8000>

默认数据库固定保存在：`data/seo-control.sqlite3`。

## AI 审核配置（可选）

服务端支持 OpenAI 兼容接口。请在启动服务前设置以下环境变量，密钥不要提交到 Git：

```powershell
$env:SEO_AI_API_KEY = "你的密钥"
$env:SEO_AI_BASE_URL = "https://你的兼容接口/v1"
$env:SEO_AI_MODEL = "你的模型名称"
```

未配置时系统仍可运行，并使用本地规则完成基础初筛。

## 数据来源说明

| 数据 | 当前来源 | 说明 |
| --- | --- | --- |
| 关键词发现 | Google Suggest | 用于发现关联词和长尾词 |
| 真实 VOL / CPC / Ads 竞争度 | Google Ads CSV 或后续 API | 未接入时不展示为真实值 |
| SEO 审核 | 本地规则或 AI 服务 | 作为分流依据，保留人工复核空间 |
| KD / 机会分 | VOL + SERP 指标计算 | 需要真实 SERP 数据后才具备决策价值 |
