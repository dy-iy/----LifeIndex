# 三倍做多人生 · Lifefund

一个把课程安排、学习计时、课后复盘和个人成长指数连接起来的微信小程序。记录每节课的学习过程，通过 AI 整理复盘信息，再用分时走势与 K 线展示上课指数的变化。

目前主要实现上课场景；算法、家务、娱乐指数仍为预留入口。指数是应用内的学习反馈数值，不涉及真实资金或证券交易。

## 功能

- **课表管理**：配置学期、节次与周次，添加、修改和删除课程；删除也清理过去的课表安排。
- **AI 课表助手**：一次理解文字与 1—3 张截图，直接保存 JSONB 课程；支持缺项、分周安排、自然语言修改和重叠课程。
- **学习计时**：开始、暂停、继续和结束计时，由服务端保存状态与有效时长。
- **课后复盘**：按参与情况填写学习内容与专注自评，查看待复盘和已复盘记录。
- **AI 复盘处理**：当前链路为结构化总结、多空判断、规则校验与结算，支持失败重试。
- **上课指数**：结合计时状态和复盘事件生成连续行情，展示分时及日、周、月 K 线。
- **账户与资料**：微信登录、会话续期、头像与昵称管理；时区仅在「我的」统一设置，默认北京时间。
- **微信转发**：全部页面支持从右上角菜单转发给好友或群聊，接收者打开小程序首页。

课表与学习历史分别保存。删除或替换课表会清理对应课程和实际课次，已结束计时、复盘、参与记录及指数流水使用独立快照继续保留。某课次仍在计时或暂停中时，其删除、修改会被阻止，结束计时后可以重试；未计时的当前课次允许立即删除或替换，并在课表及计时页刷新后生效。正式库已于 2026-10-12 迁移至 `0019_independent_learning` 并重启单一后端；小程序源码仍需重新编译或发布。

## 技术与目录

前端使用微信原生小程序（JavaScript、WXML、WXSS）；后端使用 Python、FastAPI、SQLAlchemy、PostgreSQL 和 Alembic。课表助手使用 LangGraph 保存工作流状态，模型请求由后端调用 DeepSeek。

```text
.
├── miniprogram/
│   ├── pages/                     # 首页、课表、计时、复盘、行情与设置
│   ├── components/                # 共用组件
│   ├── services/                  # API 与客户端状态
│   └── config/backend.example.js  # 后端地址示例
├── backend/
│   ├── main.py                    # API 入口及后台线程生命周期
│   ├── worker_runtime.py          # 单进程后台任务管理
│   ├── agents/                    # 模型角色、提示词与工作流
│   ├── migrations/                # 数据库迁移
│   ├── schedule_worker.py         # 课表任务逻辑与人工 CLI
│   ├── learning_worker.py         # 复盘、维护逻辑与人工 CLI
│   ├── ecosystem.config.js        # PM2 进程配置
│   ├── deploy/                    # Nginx 与 systemd 配置示例
│   ├── .env.example              # 后端环境变量示例
│   └── requirements.lock.txt      # 固定版本依赖
└── project.config.example.json    # 微信开发者工具项目示例
```

后端只运行一个 `lifefund-backend` 进程。API 接收请求并保存任务，其 lifespan 同时启动课表、复盘评估和维护三个后台线程，处理课表识别保存、复盘评估、行情推进和过期计时收尾；线程在服务关闭时停止领取新任务并收尾。任务与恢复状态仍保存在 PostgreSQL。没有配置数据库时不启动后台线程。

## 本地运行

准备 Python 3.10 或更高版本、PostgreSQL、微信开发者工具，以及自己的微信小程序配置和 DeepSeek API Key。以下命令使用 Bash。

### 1. 配置后端

在项目根目录执行；已有本地配置时直接编辑现有文件，避免覆盖。

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
cp -n .env.example .env
```

先创建供本项目使用的 PostgreSQL 数据库和账户，再编辑 `backend/.env`：

| 变量 | 用途 |
| --- | --- |
| `DATABASE_URL` | 自己的 PostgreSQL 连接串，替换示例用户名、密码、地址和库名 |
| `WECHAT_APP_ID` | 自己的微信小程序 AppID，与前端项目配置一致 |
| `WECHAT_APP_SECRET` | 小程序 AppSecret，仅保存在后端 |
| `DEEPSEEK_API_KEY` | 模型服务密钥，仅保存在后端 |
| `SESSION_TTL_SECONDS` | 登录会话有效期 |
| `AI_DAILY_REQUEST_LIMIT` | 评估和遗留聊天接口的每日次数配额 |
| `EVALUATION_DAILY_BUDGET_MICRO_CNY` | 每用户每日复盘评估预算，单位为百万分之一元 |

其他费用估算参数见 [环境变量示例](backend/.env.example)。课表助手目前没有每日次数或费用上限，真实模型调用会产生费用。

### 2. 初始化数据库

确认 `.env` 指向自己准备好的数据库后，在 `backend` 目录执行：

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m schedule_worker --setup
```

第一条命令应用业务表迁移，第二条初始化课表工作流的检查点表。服务启动不会自动替你执行这两步。

### 3. 启动服务

在 `backend` 目录运行一个进程，后台任务随 API 启动：

```bash
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8003
```

只运行一个 Uvicorn 实例，不使用 `--workers`、`--reload` 或 PM2 cluster。运行时持有本机部署目录的独占锁，同一目录的第二个 API 实例会启动失败；锁在后台线程全部退出或进程终止后释放，不创建锁文件。独立 CLI 仍供人工初始化和单轮诊断使用，例如 `python -m schedule_worker --setup`、`python -m schedule_worker --once`、`python -m learning_worker --once`；正式部署不另行启动 worker 常驻进程。

本机访问 `http://127.0.0.1:8003/health` 应返回 `{"status":"ok"}`，接口文档位于 `http://127.0.0.1:8003/docs`。健康检查只确认 API 进程响应，不能代替数据库、微信登录或模型服务的功能验证。

### 4. 配置小程序

回到项目根目录执行：

```bash
cp -n project.config.example.json project.config.json
cp -n miniprogram/config/backend.example.js miniprogram/config/backend.js
```

将 `project.config.json` 的 `appid` 改为自己的小程序 AppID，将 `miniprogram/config/backend.js` 的 `BASE_URL` 改为自己的 HTTPS 后端地址。使用微信开发者工具导入项目根目录，并在小程序平台配置对应的合法域名。

真机需要能访问该后端地址；`127.0.0.1` 只适用于同一台机器上的本地调试。AppSecret、API Key、数据库密码和上传签名私钥均不得放进小程序代码。

## 部署

可以使用 [PM2 配置](backend/ecosystem.config.js) 管理唯一的 `lifefund-backend`。安装 PM2、完成环境配置及数据库初始化后，从项目根目录执行：

```bash
pm2 startOrRestart backend/ecosystem.config.js --only lifefund-backend --update-env
pm2 save
```

从旧多进程部署切换时，先停止并删除本项目的 `lifefund-schedule-worker`、`lifefund-learning-worker`、`lifefund-learning-maintenance`，再启动新版 `lifefund-backend` 并保存 PM2 列表；修改配置文件不会自动删除 PM2 中已有的进程。操作只针对 Lifefund，不使用 `pm2 delete all`。

[Nginx 示例](backend/deploy/nginx/lifefund-api.example.conf) 需要替换域名并配置自己的 HTTPS 证书。[systemd 示例](backend/deploy/systemd/lifefund-api.example.service) 使用通用安装路径，同样只启动一个进程并由 API 管理后台线程。PM2 与 systemd 选择一种方式；后台任务不另建服务。两种配置均预留 210 秒停止时间，供进行中的任务与线程收尾；超过宽限被强制终止的任务依靠持久化状态和租约恢复。

## GitHub 发布与敏感信息

公开仓库提供源码与示例配置。实际 `.env`、签名私钥、本地小程序配置、真实后端地址配置、部署配置、数据库备份、内部历史文档和个人截图均已加入 [.gitignore](.gitignore)。克隆后按上面的步骤生成自己的本地配置。

`.gitignore` 只影响未跟踪文件，不能清除已暂存的文件或历史提交。在具有完整 Git 元数据的本地仓库中，先检查是否仍在跟踪应忽略的文件：

```bash
git ls-files -ci --exclude-standard
```

如果有输出，可在 Bash 中将这些文件移出 Git 索引，保留磁盘上的原文件：

```bash
git ls-files -ci --exclude-standard -z | xargs -0 -r git rm --cached --
```

提交前查看 `git status --short` 和 `git diff --cached`，确认变更中不含真实凭据、个人数据或部署记录。若密钥曾经进入提交或已推送，应先吊销或轮换，并清理相关 Git 历史；只新增忽略规则或删除当前文件不能消除历史泄露。不要使用强制添加绕过忽略规则，也不要直接上传整个工作目录的压缩包。

## 当前范围

仓库目前不包含自动化测试及历史验收文件。运行前应使用自己的开发数据库验证课表、计时、复盘和行情流程；微信原生渲染与真机行为需要在开发者工具和设备上检查。旧版复盘模块仍用于历史任务兼容。
