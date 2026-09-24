# WorkBuddy2API（统一版）

把腾讯 CodeBuddy 账号包装成 **OpenAI 兼容 API** 的多账号网关，并内置 **Web 控制台**。

本版本把原有两个 Go 项目（`workbuddy2api` 网关 + `workbuddy2api-gui` 控制台）
合并重写为**单个 Python 3.12 / FastAPI 服务**：

- **一个端口**：OpenAI API、Web 控制台、SPA 页面、OAuth 登录全部走同一个端口（默认 `7863`）。
- **账号热加载**：网页 OAuth 登录 / 手工导入 / 删除账号后**立即生效，无需重启**（原 GUI 需要 `docker restart`）。
- **配置热加载**：在线改配置后立即应用到账号池、调度器、粘性路由等（仅 `listen` 变更需重启进程）。
- **零外部依赖**：不再需要 Upstash Redis，也不再挂载 `docker.sock`。

> ⚠️ 本项目是非官方网关，仅限**本人授权账号**、本机/私有环境测试。请遵守 CodeBuddy 平台服务条款。

---

## ✨ 核心能力

| 能力 | 说明 |
|---|---|
| 🔑 **网页 OAuth 登录** | 支持国内版(`cn`)/国际版(`global`)，浏览器完成授权，**成功后自动落盘并热加载到账号池** |
| 🔄 **多账号池** | 三因子加权随机选号（积分 ×10 + 闲置补偿 + 成功率 ×3），Top-5 候选 + 防惊群 |
| 🛡️ **熔断与冷却** | 429 软冷却指数退避、404 固定 60s、402 硬冷却至次日 04:00、连续失败熔断、在途租约 |
| 🧲 **会话粘性** | 同一会话绑定同一账号，TTL 滚动续期，失败自动解绑 |
| ⏰ **定时任务** | 每日签到（09/21）+ 余额解冻 + 猫猫旅行；22:00 token 保活 |
| ⚡ **流式 + 非流式** | 上游 SSE 逐帧规范化透传；非流式本地聚合 |
| 🧠 **推理模型兼容** | `reasoning_content` 保留、`tool_calls` 按 index 合并、effort 自动降级 |
| 📊 **可观测** | 每请求一行表格日志；`/healthz` 带 `service` 身份标识；按模型请求统计 |
| 💾 **状态持久化** | 池状态本地原子落盘，重启恢复 |
| 🗑️ **指纹脱敏** | 出站请求体黑名单指纹清洗（可关闭） |

---

## 🚀 快速开始

### 环境要求

- **Python 3.12** + [uv](https://docs.astral.sh/uv/)（本地运行）
- 或 **Docker + Docker Compose**（推荐部署）
- 一个（或多个）已注册的 CodeBuddy 账号
- 前端已随仓库提供源码；如需重新构建需 Node 20+（Docker 构建会自动完成）

### 方式一：本地运行（uv）

```bash
# 1. 准备配置
cp config.example.json config.json
#    至少设置 api_key（留空 = 不鉴权）与控制台口令 console.password

# 2. 安装依赖
uv sync

# 3. 构建前端（首次）
cd web && npm install && npm run build && cd ..

# 4. 启动（单端口）
uv run wb2api
#   或 uv run uvicorn wb2api.main:app --host 0.0.0.0 --port 7863
#   配置默认读 ./config.json（可用 -c 指定，或环境变量 WB2API_CONFIG 覆盖）
```

浏览器打开 `http://127.0.0.1:7863`，用 `console.username` / `console.password` 登录。

### 方式二：Docker Compose

```bash
mkdir -p auths data
cp config.example.json config.json
# 按需修改 config.json 的 api_key / console.password
docker compose up -d --build
```

访问 `http://<服务器IP>:7863`。

---

## ➕ 添加账号（重点：无需重启）

控制台「**添加账号**」页：

1. 选择区域（国内版 / 国际版）→ 点「发起授权」→ 复制授权链接到浏览器完成登录；
2. 页面自动轮询，拿到凭证后**立即落盘并热加载到账号池**；
3. 回到「仪表盘」即可看到新账号参与调度 —— **全程无需重启**。

也支持「账号管理 → 导入凭证」手工粘贴 `auths/workbuddy-<uid>.json` 内容。

> 若在外部（如另一台机器 / 手工）往 `auths/` 目录放入或删除凭证文件，
> 运行中的服务不会自动感知；到「账号管理」页点一次「**🔄 从磁盘同步**」即可热同步
> （等价于 `POST /api/accounts/sync`），**无需重启**。

---

## ⚙️ 配置说明

单一配置文件 `config.json`（完整样例见 [`config.example.json`](config.example.json)）。
所有时长字段支持 `600s` / `2h` / `30m` 等写法。

| 字段 | 默认 | 说明 |
|---|---|---|
| `listen` | `:7863` | **唯一**监听地址（API + 控制台 + SPA） |
| `api_key` | 空 | 网关鉴权密钥；**空 = 不鉴权**（公网必须设置） |
| `auth_dir` | `./auths` | 账号凭证目录 |
| `state_file` | `./data/state.json` | 账号池状态持久化 |
| `pricing_file` | `./data/pricing.json` | 官方价格表（统计页换算用） |
| `console.username` / `password` | `admin` / `workbuddy` | 控制台登录凭据（**请修改**） |
| `console.session_ttl` | `12h` | 控制台会话有效期 |
| `console.credentials_file` | `./data/credentials.json` | 网页改密码的持久化文件 |
| `console.read_only` | `false` | 全局只读：关闭一切写操作 |
| `console.dangerous_ops` | `false` | 解锁删除账号 / 恢复配置备份等 |
| `cooldown.soft_rate` | `600s` | 软限流冷却基数（连续触发指数退避） |
| `cooldown.soft_rate_max` | `2h` | 软冷却指数退避封顶 |
| `schedule.checkin_hours` | `[9,21]` | 每日签到整点（收尾顺带跑猫猫旅行） |
| `schedule.keepalive_hours` | `[22]` | 每日 token 保活整点 |
| `schedule.checkin_enabled` | `true` | 签到总开关（关掉则猫猫旅行一并停摆） |
| `schedule.keepalive_enabled` | `true` | 保活总开关 |
| `upstream.timeout_seconds` | `120` | 短 RPC（刷新/签到/余额/模型）总超时 |
| `upstream.header_timeout_seconds` | `120` | 聊天 SSE 首字节前超时 |
| `upstream.idle_timeout_seconds` | `300` | 聊天 SSE 流中空闲超时（活跃续命不掐） |
| `features.sanitize_blacklist_fingerprints` | `true` | 出站请求体指纹脱敏 |
| `pool.max_in_flight` | `3` | 单账号最大在途请求数（`0` = 不限） |
| `pool.breaker_threshold` | `3` | 连续失败触发熔断阈值 |
| `pool.breaker_cooldown` / `_max` | `30m` / `6h` | 熔断基础时长 / 退避封顶 |
| `pool.idle_weight_per_hour` / `_max` | `0.5` / `5.0` | 闲置补偿权重 |
| `session_sticky.enabled` / `ttl` | `true` / `30m` | 会话粘性开关 / 绑定 TTL |
| `server.max_body_mb` | `8` | 聊天请求体上限（超限 413） |
| `server.metrics_enabled` | `true` | 是否采集按模型统计 |
| `server.metrics_file` | `./data/metrics.json` | 统计持久化文件 |
| `server.metrics_retention_days` | `30` | 时间序列保留天数 |

### 环境变量覆盖

加载顺序：内置默认值 → `config.json` → 环境变量（变量非空才覆盖）：

`WB2API_CONFIG` · `WB2API_LISTEN` · `WB2API_API_KEY` · `WB2API_AUTH_DIR` · `WB2API_STATE_FILE` ·
`WB2API_PRICING_FILE` · `WB2API_CONSOLE_USERNAME` · `WB2API_CONSOLE_PASSWORD` · `WB2API_SESSION_TTL` ·
`WB2API_CREDENTIALS_FILE` · `WB2API_SOFT_RATE` · `WB2API_SOFT_RATE_MAX` · `WB2API_TIMEOUT_SECONDS` ·
`WB2API_HEADER_TIMEOUT_SECONDS` · `WB2API_IDLE_TIMEOUT_SECONDS` · `WB2API_MAX_BODY_MB` ·
`WB2API_METRICS_FILE` · `WB2API_METRICS_RETENTION_DAYS` · `WB2API_SANITIZE_FINGERPRINTS`(bool) ·
`WB2API_READ_ONLY`(bool) · `WB2API_DANGEROUS_OPS`(bool) · `WB2API_METRICS_ENABLED`(bool) ·
`WB2API_CHECKIN_ENABLED`(bool) · `WB2API_KEEPALIVE_ENABLED`(bool) · `WB2API_SESSION_STICKY`(bool)

---

## 🔌 API 端点（同一端口）

### OpenAI 兼容（`api_key` 非空时需 `Authorization: Bearer <api_key>`）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/chat/completions` | 补全（流式/非流式），请求体上限 `server.max_body_mb` |
| `POST` | `/v1/a/{uid}/chat/completions` | 固定账号补全（供上游网关按账号轮询） |
| `GET` | `/v1/a/{uid}/quota` | 单账号配额 |
| `GET` | `/v1/models` | 模型列表（动态拉取，缓存 1h，失败回落静态表） |
| `GET` | `/v1/accounts` | 账号列表 + 固定端点路径 |
| `GET` | `/v1/quota` | 全账号配额（缓存 30s） |
| `GET` | `/v1/stats` · `POST /v1/stats/reset` | 按模型请求统计 / 重置 |
| `GET` | `/status` | 账号池状态 |
| `GET` | `/healthz` | 健康检查（无鉴权，带 `service` 字段 + `X-Service` 头） |

### 控制台（Cookie 或 `Authorization: Bearer <session-token>`）

`/api/session` · `/api/login` · `/api/logout` · `/api/password` · `/api/overview` · `/api/accounts` ·
`/api/accounts/{uid}` · `/api/accounts/import` · `/api/accounts/sync` · `/api/accounts/{uid}/{checkin|refresh|travel|credits}` ·
`/api/tasks/{checkin|refresh|travel|credits}` · `/api/tasks` · `/api/tasks/{id}` ·
`/api/login/start` · `/api/login/{id}` · `/api/login/{id}/poll` · `/api/login/{id}/cancel` ·
`/api/models` · `/api/stats` · `/api/stats/reset` · `/api/pricing` · `/api/chat` · `/api/chat/stream` ·
`/api/config` · `/api/config/reset` · `/api/system` · `/api/system/restart`

`/api/accounts/sync` 从磁盘重新扫描凭证并热同步到账号池（外部新增/删除文件后使用）。

`/api/system/restart` 保留为兼容端点，实际返回“账号与配置均支持热加载，无需重启”。

### 调用示例

```bash
# 健康检查（有可用账号 200，否则 503）
curl -s http://localhost:7863/healthz

# 模型列表
curl -s http://localhost:7863/v1/models -H "Authorization: Bearer your-api-key"

# 流式聊天
curl -sN http://localhost:7863/v1/chat/completions \
  -H "Authorization: Bearer your-api-key" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

---

## 🏗️ 架构与目录

```
├── src/wb2api/
│   ├── main.py           # FastAPI 入口（单端口装配）
│   ├── config.py         # 统一配置 + 环境变量 + 热重载
│   ├── models.py         # 共享数据模型（Account / 状态 / 任务 / 会话）
│   ├── app_state.py      # 共享状态容器（账号/配置热加载的核心）
│   ├── deps.py           # 鉴权依赖（api_key / 控制台会话 / CSRF）
│   ├── auth_store.py     # 凭证读写（双形态、原子写、uid 校验）
│   ├── pool.py           # 账号池状态机（冷却/熔断/租约/加权/持久化）
│   ├── upstream.py       # 上游客户端（chat SSE/refresh/签到/积分/旅行/模型/OAuth）
│   ├── headers.py        # CN/GLOBAL 请求头
│   ├── payload.py        # 出站请求体改写 + 指纹脱敏
│   ├── sse.py            # SSE 聚合 + 白名单重建
│   ├── chat_service.py   # 聊天轮转核心（粘性/租约/错误策略/统计）
│   ├── session.py        # 会话粘性路由
│   ├── scheduler.py      # 定时签到/保活/旅行
│   ├── metrics.py        # 按模型统计 + 小时时间序列
│   ├── pricing.py        # 官方价换算
│   ├── oauth.py          # 登录会话管理
│   ├── tasks.py          # 批量任务管理
│   ├── ops.py            # 控制台业务层
│   ├── staticfiles.py    # 前端产物定位
│   └── routers/          # openai_api / admin_api / spa
├── web/                  # React SPA（复用原 GUI 前端）
├── tests/                # pytest
├── config.example.json
├── Dockerfile
└── docker-compose.yml
```

### 为什么不再需要重启

旧架构里网关只在**启动时**扫描 `auths/` 与读取 `config.json`，因此新增账号必须重启进程。
新版把网关与控制台合进**同一进程**，并由 `AppState` 统一持有账号池与配置：

- 新增/导入账号 → 写盘后直接 `pool.add(...)`，立即参与调度；
- 删除账号 → 删盘后 `pool.sync_from_dir(...)`，立即移出；
- 改配置 → `ConfigManager.write()` 后 `AppState.apply_config(...)` 热应用到各子系统。

---

## 🛠️ 开发

```bash
# 安装（含开发依赖）
uv sync

# 测试
uv run pytest -q

# 本地启动
uv run wb2api

# 前端开发（热更新，/api 代理到 7863）
cd web && npm run dev
```

---

## 🛡️ 安全与合规

- 控制台能读写账号凭证，**务必修改默认口令**，不要暴露到公网（如需公网请置于 HTTPS 反代 + 强口令之后）。
- `auths/` 内含明文 `accessToken` / `refreshToken`，切勿提交 git；`.gitignore` 已排除 `auths/`、`data/`、`config.json`。
- 仅限本人授权账号、本机/私有环境测试。使用者需遵守 CodeBuddy 服务条款，自行承担使用风险。

## 📄 License

MIT（与原项目一致）。
