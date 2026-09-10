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

## 整包（bundle）

调用方可以一次只带身份，把这个身份此刻**所有开关**的开/关结果连同**整包版本**一次拿走：

```bash
GET /api/bundle?identity=<用户身份>
# => {"identity":"u123","version":"9f2c…","flags":{"new-checkout":{"enabled":true,"reason":"rollout"}, …}}
```

- **版本恒定**：版本 = 求值输入摘要（会影响此身份求值的全部输入：所有开关的
  求值字段、此人的单人强制、互斥组成员关系、此人的组内落定记录）+ 单调递增的
  内容序号。配置没变时，同一人多次来拿，每个开关的结果和版本都不变。
- **变了必新版本**：管理端改了任何会影响此人的一层（开关配置、对此人的强制、
  互斥组变动），序号 +1，他再来拿一定是新版本、按改完后的规则重算。
  与他无关的改动（如给别人的单人强制、改描述）不影响他的版本。
- **改回不复活**：序号只增不减——把配置改回去，摘要虽复原，版本也不会回到
  旧值；改过一次，旧包就永远失效，不存在"改回去旧包又能用"。
- **记录即所得**：管理端变更后系统用与调用方来拿时完全相同的求值重算每个
  已发整包，失效记录里记下的新版本，与本人再来拿时拿到的版本一字不差。
- **过期校验**：拿着旧版本来问，会明确告知是否已过期，并附带按当前规则重算的新包：

```bash
GET /api/bundle?identity=<用户身份>&version=<手中的版本>
# => {…, "valid": false}   # valid=false 即已过期，响应里是新版本与新结果
```

- **失效可见**：已发出的整包落库（`bundles` 表）。管理端每次变更配置后，
  系统重算每个已发整包的版本，版本变了的身份记入 `bundle_invalidations`，
  管理端可查到「谁、何时、改了什么、让哪些人的整包失效了」。

> 与单开关 `check` 一样，整包查询对组内开关可能写入落定记录（GET 有写副作用）。

## 定时生效（约个时间再生效）

管理端改开关配置（全关 / 默认值 / 放量比例 / 描述）时，可以带一个 `effective_at`
（unix 秒）把改动约到未来某个时刻生效：

```bash
# 把放量比例约到 2026-09-12 09:00:00（服务器时间）生效
curl -X PATCH http://localhost:8000/api/flags/new-checkout \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" \
  -d '{"rollout_percent": 50, "effective_at": 1789146000}'
# => 202 {"ok":true,"scheduled":true,"change_id":3,"effective_at":1789146000}
```

- **到点前一切照旧**：定时变更只躺在预约表里，不参与求值——来问的人还按现在的
  结果走，整包结果与整包版本都不换。
- **到点后自动生效**：第一个进来的请求（`check` / `bundle` / 管理端接口均可）
  把变更写到开关上（惰性应用，无需后台线程）。此后按新规则求值、整包换新版本；
  拿着到点前那包的版本来问会明确告知已过期（`valid=false`），响应里附带按新
  规则重算的新包。失效记录与审计里能查到这次生效（操作人记预约时的人）。
- **不约时间照旧立即生效**：不带 `effective_at` 的改动行为完全不变。
- **可取消**：还没到点的预约可以随时取消，取消后到点也不会生效；同一开关可约
  多个变更，到点按生效时间先后应用；删除开关会一并取消它未生效的预约。
- `effective_at` 必须是将来的时刻；想立即生效就不要带这个字段。

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

GET /api/bundle?identity=<用户身份>[&version=<手中的整包版本>]
# => {"identity":"u123","version":"9f2c…","flags":{"new-checkout":{"enabled":true,"reason":"rollout"}, …}}
#    带 version 时响应多一个 valid 字段：false 即该版本已过期，响应里是新版本与新结果
```

### 管理端（需请求头 `X-Admin-Token`，可选 `X-Actor` 记录操作人）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/flags` | 列出所有开关 |
| POST | `/api/flags` | 新建 `{name, description, default_enabled}` |
| PATCH | `/api/flags/<name>` | 改 `{default_enabled, rollout_percent, kill_switch, description}`；带 `effective_at`（unix 秒）则约到该时刻生效 |
| DELETE | `/api/flags/<name>` | 删除开关（其未生效的定时变更一并取消） |
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
| GET | `/api/scheduled-changes` | 还没到点的定时变更（开关、改动内容、生效时刻、预约人） |
| DELETE | `/api/scheduled-changes/<id>` | 取消一个还没到点的定时变更 |
| GET | `/api/bundles` | 已发出的整包（身份、版本、是否已过期 stale） |
| GET | `/api/bundles/invalidations?limit=100` | 整包失效记录：谁改了什么、让哪些身份的整包从哪个版本变成哪个版本 |

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
