# 特性开关服务（Feature Flags）

一套自包含的特性开关系统：管理页面 + 调用方查询接口，Flask + SQLite，单容器部署。

## 求值优先级（固定，代码中不可调整）

```
全关(kill_switch) > 单人强制(override) > 比例放量(rollout) > 默认值(default)
```

- **全关**：打开后所有身份一律为关，覆盖一切。
- **单人强制**：对指定 identity 强制开/关，覆盖放量与默认值。
- **比例放量**：`sha256("{flag}:{identity}") % 100 < percent` 则开。
  同一身份对同一开关永远落在同一侧，与进程、机器、重启无关；
  调高比例只会**新增**命中者，已命中者不会掉出。
- **默认值**：以上各层都未决定时兜底。

## 快速开始

```bash
# Docker Compose（推荐）
ADMIN_TOKEN=你的强随机串 docker compose up -d --build

# 或纯 Docker
docker build -t feature-flags .
docker run -d -p 8000:8000 -e ADMIN_TOKEN=你的强随机串 \
  -v flags-data:/data feature-flags
```

打开 `http://localhost:8000`，在页面顶部填入管理 Token 和操作人姓名即可使用。
数据持久化在容器卷 `/data/flags.db`（SQLite）。

## 接口

### 调用方（无需鉴权）

```bash
GET /api/flags/<name>/check?identity=<用户身份>
# => {"flag":"new-checkout","identity":"u123","enabled":true,"reason":"rollout"}
#    reason ∈ kill_switch | override | rollout | default，表示结果由哪一层决定
```

### 管理端（需请求头 `X-Admin-Token`，可选 `X-Actor` 记录操作人）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/flags` | 列出所有开关 |
| POST | `/api/flags` | 新建 `{name, description, default_enabled}` |
| PATCH | `/api/flags/<name>` | 改 `{default_enabled, rollout_percent, kill_switch, description}` |
| DELETE | `/api/flags/<name>` | 删除开关 |
| GET | `/api/flags/<name>/overrides` | 列出单人强制 |
| PUT | `/api/flags/<name>/overrides/<identity>` | 设置 `{enabled: true/false}` |
| DELETE | `/api/flags/<name>/overrides/<identity>` | 移除单人强制 |
| GET | `/api/audit?limit=100` | 最近操作记录（谁、何时、改了哪一层） |

示例：

```bash
# 一键全关
curl -X PATCH http://localhost:8000/api/flags/new-checkout \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" -d '{"kill_switch": true}'

# 50% 放量
curl -X PATCH http://localhost:8000/api/flags/new-checkout \
  -H "X-Admin-Token: $TOKEN" -d '{"rollout_percent": 50}'

# 单人强制开
curl -X PUT http://localhost:8000/api/flags/new-checkout/overrides/u123 \
  -H "X-Admin-Token: $TOKEN" -d '{"enabled": true}'
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_TOKEN` | `dev-admin-token` | 管理端令牌，**生产必须覆盖** |
| `FLAG_DB` | `/data/flags.db` | SQLite 路径 |
| `PORT` | `8000` | 监听端口 |

## 本地开发

```bash
pip install -r requirements.txt
ADMIN_TOKEN=test python app.py
```
