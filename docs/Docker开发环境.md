# Docker 开发环境

本环境为新版 B2B SEO Agent 平台准备，**不会替换或删除**现有 `http://127.0.0.1:8000` 关键词/内容工作台。

## 服务

| 服务 | 地址 | 用途 |
| --- | --- | --- |
| Platform API | `http://127.0.0.1:8010` | FastAPI 基础 API；业务接口后续接入 |
| PostgreSQL 16 | `localhost:5432` | 新版多站点、任务、来源、内容资产数据 |
| Redis 7 | `localhost:6379` | Celery 消息队列与任务结果 |
| Celery Worker | 容器内 | 后续 SERP 抓取、H2 写作、内链和图片任务 |

现有网页工作台新增“Agent 控制台”入口：`http://127.0.0.1:8000/agent-platform`。可创建隔离站点、提交内容任务，并查看 Docker 服务和 Celery 任务状态。

## 启动

```powershell
# 可选：按需覆盖端口与本地密码；.env 已被 Git 忽略。
Copy-Item .env.example .env

docker compose up --build -d
docker compose ps
Invoke-RestMethod http://127.0.0.1:8010/health
Invoke-RestMethod http://127.0.0.1:8010/ready
```

若 Docker Desktop 在中文目录下报 BuildKit / gRPC 编码错误，先用兼容构建模式构建 API 镜像，再启动服务：

```powershell
$env:DOCKER_BUILDKIT = "0"
$env:COMPOSE_BAKE = "false"
docker compose build api
docker compose up -d --no-build
```

## 日常操作

```powershell
docker compose logs -f api worker
docker compose down                 # 停止容器，保留数据库卷
docker compose down -v              # 删除容器和本地开发数据卷
```

不要把 `.env`、数据库导出、浏览器 Profile、OAuth Token 或 API Key 提交到 Git。生产部署必须改用独立强密码、受限端口、TLS 和密钥管理服务。
