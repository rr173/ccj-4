# 特性开关服务（Feature Flags）

一套自包含的特性开关系统：管理页面 + 调用方查询接口，Flask + SQLite，单容器部署。

## 求值优先级（固定，代码中不可调整）

```
全关(kill_switch) > 单人强制(override) > 互斥组(group) > 比例放量(rollout) > 默认值(default)
```

- **全关**：打开后所有身份一律为关，覆盖一切。
- **单人强制**：对指定 identity 强制开/关，覆盖互斥组、放量与默认值。
- **互斥组**：见下节。
- **比例放量**：`sha256("{flag}:{identity}") % 100 < percent` 则开，未命中则关。
  只要比例 > 0，这一层就给出定论，不再落到默认值；比例为 0 表示未启用放量。
  同一身份对同一开关永远落在同一侧，与进程、机器、重启无关；
  调高比例只会**新增**命中者，已命中者不会掉出。
- **默认值**：仅当放量比例为 0 时兜底。

## 互斥组

管理端可把多个开关编入同一互斥组（一个开关最多进一个组）。对同一身份，
**组内最多一个开关为开，且身份不变时开着的那个不会换**：

- 开关先按「比例放量 / 默认值」算出自然结果；自然结果为关时不参与组规则、
  不在组内占位。
- 自然结果为开且开关在组内时，由组规则裁决：该身份在组内已落定过别的开关
  则判关；否则落定为本开关并写库（`group_assignments` 表），此后恒定。
  并发请求下同一身份也只会有一个开关落定成功。
- **全关与单人强制优先于组规则**：强制开不受组内已有人开着的限制，
  全关仍然全关；这两层解除后，原落定结果恢复。
- 未进组的开关完全按原规则求值。
- 把开关移出组（或删除开关）会释放它在组内占有的落定记录，相关身份之后
  可重新落定组内其他开关；解散整组则清除全部落定记录，组内开关恢复按
  原规则求值。

> 注意：对组内开关的 `check` 查询可能在首次判开时写入落定记录（GET 有写副作用），
> 这是"同一人结果恒定"的实现基础。

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
#    reason ∈ kill_switch | override | group | rollout | default，表示结果由哪一层决定
#    identity 需 URL 编码；允许包含斜杠、空格、引号等任意字符
```

### 管理端（需请求头 `X-Admin-Token`，可选 `X-Actor` 记录操作人）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/flags` | 列出所有开关 |
| POST | `/api/flags` | 新建 `{name, description, default_enabled}` |
| PATCH | `/api/flags/<name>` | 改 `{default_enabled, rollout_percent, kill_switch, description}` |
| DELETE | `/api/flags/<name>` | 删除开关 |
| GET | `/api/flags/<name>/overrides` | 列出单人强制 |
| PUT | `/api/flags/<name>/overrides` | 设置 `{identity, enabled}` |
| DELETE | `/api/flags/<name>/overrides` | 移除，body `{identity}` |
| GET | `/api/groups` | 列出互斥组（含成员开关、`updated_by` 最近修改人） |
| POST | `/api/groups` | 新建组 `{name, description}` |
| DELETE | `/api/groups/<name>` | 解散组（落定记录一并清除） |
| PUT | `/api/groups/<name>/flags` | 开关进组，body `{flag}`（已在别组则 409） |
| DELETE | `/api/groups/<name>/flags` | 开关移出组，body `{flag}`（释放其落定记录） |

> identity 一律放在 JSON body / 查询参数里，不进 URL 路径，
> 因此含 `/`、空格、`"`、`'` 等字符的身份都能正常设置、查询、移除。
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

# 建互斥组并把两个开关编进去
curl -X POST http://localhost:8000/api/groups \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" -d '{"name": "homepage-exp", "description": "首页方案互斥"}'
curl -X PUT http://localhost:8000/api/groups/homepage-exp/flags \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" -d '{"flag": "new-checkout"}'

# 单人强制开（identity 在 body 里，特殊字符无需转义）
curl -X PUT http://localhost:8000/api/flags/new-checkout/overrides \
  -H "X-Admin-Token: $TOKEN" -d '{"identity": "u/123 x", "enabled": true}'
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
