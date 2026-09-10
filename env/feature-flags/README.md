# 特性开关服务（Feature Flags）

一套自包含的特性开关系统：管理页面 + 调用方查询接口，Flask + SQLite，单容器部署。

## 求值优先级（固定，代码中不可调整）

```
全关(kill_switch) > 结果冻结(freeze) > 开关依赖(depends_on) > 单人强制(override) > 属性打开条件(targeting) > 互斥组(group) > 比例放量(rollout) > 默认值(default)
```

- **全关**：打开后所有身份一律为关，覆盖一切，**包括冻住的结果**。
- **结果冻结**：见下节。
- **开关依赖**：见下节。
- **单人强制**：对指定 identity 强制开/关，覆盖属性条件、互斥组、放量与默认值；
  但救不回「被依赖开关是关」（依赖层在强制之前），也动不了冻住的结果。
- **属性打开条件**：见下节。
- **互斥组**：见下节。
- **比例放量**：`sha256("{flag}:{identity}") % 100 < percent` 则开，未命中则关。
  只要比例 > 0，这一层就给出定论，不再落到默认值；比例为 0 表示未启用放量。
  同一身份对同一开关永远落在同一侧，与进程、机器、重启无关；
  调高比例只会**新增**命中者，已命中者不会掉出。
- **默认值**：仅当放量比例为 0 时兜底。

## 结果冻结（freeze）

管理端可以把**某个人对某个开关「此刻」的结果**冻住。冻住时系统先按当时的全部
规则（依赖、单人强制、不带属性口径的属性条件、互斥组、放量、默认值）完整求一次
值，把得到的开/关存下来：

```bash
# 把 u123 此刻对 new-checkout 的结果冻住（先求后冻，返回冻住的值与当时理由）
curl -X PUT http://localhost:8000/api/flags/new-checkout/freezes \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" -H "Content-Type: application/json" \
  -d '{"identity": "u123"}'
# => {"ok":true,"changed":true,"enabled":true,"reason":"rollout"}

# 解冻
curl -X DELETE http://localhost:8000/api/flags/new-checkout/freezes \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" -d '{"identity": "u123"}'
```

- **冻住后雷打不动**：此后该人来问（单查、整包、带不带属性），只要本开关没被
  全关压着，一律给冻住的那个结果（`reason=freeze`）。改默认值、放量比例、属性
  条件、依赖关系、给此人加单人强制、改互斥组——都不动它；被别的开关依赖时，
  依赖者看到的也是这个冻住的结果。没冻的人不受影响，照改后的规则算。
- **全关仍压过冻住**：本开关一键全关期间，冻住的人也是一律关
  （`reason=kill_switch`）；解除全关后自动回到冻住的值。
- **解冻即按当时规则**：解冻没有任何残留，下一次来问立刻按解冻当时的配置求值。
- **冻/解冻让整包换新版本**：该身份拿过的整包（不含属性的包与各身属性包）全部
  序号 +1、换新版本，拿着冻/解冻前那包来问得到 `valid=false`，失效记录里记的新
  版本与本人再来拿时拿到的一字不差。冻住期间改默认值/放量/条件/依赖/强制，此人
  这些结果没变，他的包版本一字不变；但全关仍会让包换版本。
- **重复冻 = 按此刻规则重新冻**：同一个人再冻一次，若此刻结果（假设现在解冻会
  算出的值）与已冻住的相同，则什么都不变（`changed=false`，不产生失效）；不同则
  以新结果为准。
- **只对人不对属性**：冻住只认 identity，与来问时带不带 `attrs`、带什么属性无关；
  冻住那一刻按不带属性的口径求值。
- 删除开关时它的冻结记录一并删除；`GET /api/flags/<name>/freezes` 可查看该开关
  冻住了哪些人。

## 开关依赖（depends_on）

管理端可以指定本开关**先看另一个开关**。来问时（单查或整包），系统会用
**同一个身份、同一身属性**把被依赖的开关完整求值一遍：

- 被依赖的开关对这个人、这身属性**也是开**，本开关才有机会继续往下算
  （强制、属性条件、互斥组、放量、默认值照旧）；
- 被依赖的是**关**，本开关**一律判关**，`reason=depends_on`。这一层排在
  单人强制之前——对本开关的强制开也救不回来；只有本开关自己的全关能压过它
  （全关仍然一律关）。

```bash
# pay 开关默认开；promo-pay 依赖它：pay 关的人，promo-pay 必然关
curl -X PATCH http://localhost:8000/api/flags/promo-pay \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" -d '{"depends_on": "pay"}'

curl "http://localhost:8000/api/flags/promo-pay/check?identity=u123"
# pay 开：{"enabled":true,"reason":"default",…}（继续按本开关自己的规则算）
# pay 关：{"enabled":false,"reason":"depends_on",…}
```

- **同一身份、同一身属性**：被依赖开关按来问的同一 `identity` 与同一身 `attrs`
  求值。例如 pay 只对 `plan=pro` 的人开，那么 free 用户来问 promo-pay 时，
  pay 对这身属性是关，promo-pay 也是关。
- **可以成链**：C 依赖 B、B 依赖 A；A 关则 B、C 都关（链上每一步的理由都是
  `depends_on`）。整包求值时链上开关只算一次，单查任一开关与整包里的结果一致。
- **不能成环**：自依赖、互相绕着依赖（含间接成环）在管理端写入时直接 `400`
  拒绝，不会写出一个算不出来的配置。
- **改了立即按新的算**：设置、改依赖、解除（`""` / `null`）都立即生效；
  依赖关系是整包求值输入的一部分，改动后所有已发整包换新版本，拿着改前那包
  来问会得到 `valid=false`。也支持带 `effective_at` 预约定时改依赖。
- **删除被依赖开关**：依赖关系自动解除，本开关恢复按自己的规则算。
- 管理端列表中每个开关带 `depends_on` 字段（空串表示无依赖）。


## 属性打开条件（targeting）

调用方来问开关时，可以在身份之外再带上**这个人身上的属性**（`attrs`，一个扁平
JSON 对象）；管理端可以给开关定一个「打开条件」，来问属性与条件**全对上了才开**，
对不上、没带属性或开关没定条件，都按原来的规则（互斥组/放量/默认值）继续算：

```bash
# 管理端定条件：套餐为 pro，且等级是 3 或 5（多键 AND，值给列表是任一）
curl -X PATCH http://localhost:8000/api/flags/new-checkout \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" \
  -d '{"targeting": {"plan": "pro", "level": [3, 5]}}'

# 调用方来问时带属性（attrs 是 URL 编码的 JSON）
curl "http://localhost:8000/api/flags/new-checkout/check?identity=u123&attrs=%7B%22plan%22%3A%22pro%22%2C%22level%22%3A3%7D"
# => {"flag":"new-checkout","identity":"u123","attrs":{"plan":"pro","level":3},
#     "enabled":true,"reason":"targeting"}
```

- **条件形式**：`{"键": 标量}` 或 `{"键": [标量, …]}`。多个键之间是 **AND**
  （每个键都得来问属性里有且值相等），一个键给多个值时是 **OR**（任一相等即可）。
  属性里多带条件没要求的键不影响对上；条件的值支持字符串、数字、布尔、`null`，
  按 JSON 语义精确匹配（`true` ≠ `1`，`3` = `3.0`，`3` ≠ `"3"`）。
- **结果恒定**：判定是纯函数，同一人、同一身属性，问多少次、从哪台机器问，
  结果都一样；JSON 键的书写顺序不影响判定与整包版本。
- **优先级**：全关与单人强制仍然压过属性条件；属性命中与放量命中一样算
  「自然结果为开」——开关若在互斥组内，命中后仍要过组规则（见下节）。
- **整包按属性分包**：同一身份带不同属性来拿整包，是各自独立的包、各自有版本；
  不带属性的老包不受任何属性条件影响。管理端增改条件后，只有「此条件实际参与过
  求值」的属性包（即属性对得上的那些）才换新版本、记失效；对不上的属性包和不带
  属性的包版本一字不变。
- `targeting` 同样支持 `effective_at` 预约定时生效；传 `{}` 或 `null` 清除条件。

## 互斥组

管理端可把多个开关编入同一互斥组（一个开关最多进一个组）。对同一身份，
**组内最多一个开关为开，且身份不变时开着的那个不会换**：

- 开关先按「属性打开条件 / 比例放量 / 默认值」算出自然结果；自然结果为关时
  不参与组规则、不在组内占位（落定只认身份，与本次带没带属性、带了什么属性无关）。
- 自然结果为开且开关在组内时，由组规则裁决：该身份在组内已落定过别的开关
  则判关；否则落定为本开关并写库（`group_assignments` 表），此后恒定。
  并发请求下同一身份也只会有一个开关落定成功。
- **全关、依赖与单人强制优先于组规则**：强制开不受组内已有人开着的限制，
  全关仍然全关，被依赖开关关着时强制开也开不了；这几层解除后，原落定结果恢复。
- 未进组的开关完全按原规则求值。
- 被依赖的开关会先于依赖者求值：若两者同组，先落定的是被依赖者。
- 把开关移出组（或删除开关）会释放它在组内占有的落定记录，相关身份之后
  可重新落定组内其他开关；解散整组则清除全部落定记录，组内开关恢复按
  原规则求值。

> 注意：对组内开关的 `check` 查询可能在首次判开时写入落定记录（GET 有写副作用），
> 这是"同一人结果恒定"的实现基础。

## 整包（bundle）

调用方可以带身份（可再带一身属性 `attrs`），把这个身份此刻**所有开关**的开/关结果
连同**整包版本**一次拿走。整包按 **(身份, 属性)** 分别记账：同一人带不同属性是
不同的包、各自有版本；不带属性的包不受任何属性条件影响。

```bash
GET '/api/bundle?identity=<用户身份>&attrs=<URL编码的JSON属性>'
# => {"identity":"u123","attrs":{"plan":"pro"},"version":"9f2c…",
#     "flags":{"new-checkout":{"enabled":true,"reason":"targeting"}, …}}
```

- **版本恒定**：版本 = 求值输入摘要（会影响此身份此身属性求值的全部输入：所有开关的
  求值字段、此人的单人强制、互斥组成员关系、此人的组内落定记录、开关之间的依赖关系；
  带属性时还包括属性本身与对此人实际生效的属性条件）+ 单调递增的内容序号。配置没变时，
  同一 (身份, 属性) 多次来拿，每个开关的结果和版本都不变。
- **变了必新版本**：管理端改了任何会影响此包的一层（开关配置、对此人的强制、
  互斥组变动、依赖关系、对此人生效的属性条件），序号 +1，他再来拿一定是新版本、
  按改完后的规则重算。与他无关的改动（如给别人的单人强制、改描述、他对不上的
  属性条件）不影响他的版本。
- **改回不复活**：序号只增不减——把配置改回去，摘要虽复原，版本也不会回到
  旧值；改过一次，旧包就永远失效，不存在"改回去旧包又能用"。
- **记录即所得**：管理端变更后系统用与调用方来拿时完全相同的求值重算每个
  已发整包，失效记录里记下的新版本，与本人带同一身属性再来拿时拿到的版本一字不差。
- **过期校验**：拿着旧版本来问，会明确告知是否已过期，并附带按当前规则重算的新包。
  版本必须与同一身属性配对使用，属性不同的两个包互不算过期：

```bash
GET '/api/bundle?identity=<用户身份>&attrs=<URL编码的JSON属性>&version=<手中的版本>'
# => {…, "valid": false}   # valid=false 即已过期，响应里是新版本与新结果
```

- **失效可见**：已发出的整包落库（`bundles` 表，按身份 + 属性哈希分行）。管理端
  每次变更配置后，系统重算每个已发整包的版本，版本变了的包记入
  `bundle_invalidations`，管理端可查到「谁、何时、改了什么、让哪些人的哪身属性的
  整包失效了」。

> 与单开关 `check` 一样，整包查询对组内开关可能写入落定记录（GET 有写副作用）。

## 定时生效（约个时间再生效）

管理端改开关配置（全关 / 开关依赖 / 默认值 / 放量比例 / 属性打开条件 / 描述）时，可以带一个
`effective_at`（unix 秒）把改动约到未来某个时刻生效：

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

## 发布稿（好几处改动收成一稿，发布时一起生效）

管理端可以把好几处改动先**收进同一稿**，发布前不影响任何线上结果；点发布后，
稿里的改动**一起生效**，整包统一换新版本。

```bash
# 1) 开一个稿
DID=$(curl -s -X POST http://localhost:8000/api/drafts \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" -H "Content-Type: application/json" \
  -d '{"note": "秋季大促一揽子变更"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["draft_id"])')

# 2) 把多个开关的改动收进稿（同一字段后收的覆盖先收的，不同字段合并）
curl -s -X PUT http://localhost:8000/api/drafts/$DID/flags/promo \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" -H "Content-Type: application/json" \
  -d '{"default_enabled": true, "rollout_percent": 30}'
curl -s -X PUT http://localhost:8000/api/drafts/$DID/flags/beta \
  -H "X-Admin-Token: $TOKEN" -d '{"kill_switch": true, "targeting": {"plan": "pro"}}'

# 3) 发布：要么整稿一起生效，要么一处都不动
curl -s -X POST http://localhost:8000/api/drafts/$DID/publish \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice"
```

- **发布前一切照旧**：稿只是躺在 `drafts` / `draft_changes` 两张表里，**不参与求值**。
  来问一个开关、带属性来问、拿整包，全部按现在的配置算，整包版本一字不变，稿也不
  产生任何失效记录。
- **发布时一起生效**：发布是一个事务——稿里所有开关的字段一次性写入，随后像一次
  普通变更那样统一重算已发整包：整包换新版本，拿着发布前那包来问得到 `valid=false`。
- **发布时整体校验，成环整稿拒绝**：收入稿时只做字段级校验（A 依赖 B、B 依赖 A
  各自都能收进来）；发布前把整稿与现网配置合并成图统一校验——**合并后的依赖关系
  互相绕着（成环、自依赖）或依赖的开关已不存在，这一稿全都不生效**：一个字段都不
  写、整包版本不变，稿仍是「未发布」，改好或摘掉相关条目后可以重新发布。
- **没发布的稿能丢掉**：`DELETE /api/drafts/<id>` 整稿丢弃，不影响任何求值与版本；
  也可以用 `DELETE /api/drafts/<id>/flags/<name>` 只摘掉稿里某个开关的改动。
- **多稿并存**：可以同时开多个稿；发布其中一个不影响其他稿。已发布 / 已丢弃的稿
  保留记录（`GET /api/drafts?all=1`），但不能再改、再发布。
- 稿里可收的字段与立即生效 PATCH 一致：`kill_switch`、`default_enabled`、
  `rollout_percent`、`targeting`（`{}` 清条件）、`depends_on`（`""`/`null` 解除）、
  `description`。**稿不支持 `effective_at`**：发布的一刻就是整稿的生效时刻。
- 若稿里某个开关在发布前被删除，发布会被整稿拒绝（先把该开关的改动从稿里摘掉即可）。

## 问某个过去的时刻（历史重放）

调用方可以指定一个**过去的时刻**，问某个人当时**每个开关**开还是关；开着的开关
带上**当时那份配置**。结果与那一刻真来拿整包会拿到的一字不差：

```bash
curl "http://localhost:8000/api/history?identity=u123&at=1789000000"
# => {"identity":"u123","at":1789000000.0,
#     "flags":{"new-checkout":{"enabled":true,"reason":"rollout","config":{…}}, …}}
# 也支持 attrs（URL 编码 JSON）：用当时的属性条件 + 这身属性一起算
curl "http://localhost:8000/api/history?identity=u123&at=1789000000&attrs=%7B%22plan%22%3A%22pro%22%7D"
```

- **口径与整包一致**：只返回当时存在的开关，按固定优先级（全关 > 冻结 > 依赖 >
  强制 > 属性 > 组 > 放量 > 默认）逐开关求值；组落定也按当时的记录还原——那一刻
  真查过的人按当时落定算，没查过的按「名字序首个自然开」确定性模拟（与整包同序）。
- **配置是当时那份**：判开才带 `config`，判关（含全关、依赖关、强制关、没争到组、
  放量未命中、默认关）不带；**冻住的人按冻住的算**，带冻住那一刻的配置快照，
  全关期间同样不带。
- **约了时间没到点的不算**：到点前问历史拿到的是旧值；到了点（哪怕还没有任何
  请求触发惰性应用）问历史就是新值；已取消的预约到点也不算。
- **没发布的稿不算**：只有 `published_at <= at` 的发布稿参与重放，且稿里的改动
  在发布那一刻一起生效；丢弃 / 未发布的稿对任何时刻都不可见。
- **管理端改过的都留着**：每次已生效的管理端改动（开关增删改、强制、冻/解冻、
  组与成员、组落定）都进 append-only 的 `history_events` 流水，只增不改不删；
  删除的开关在删前的时刻仍查得到，删后消失（别人对它的依赖按自动解除算）。
- `at` 是 unix 秒，必须不晚于现在；未来时刻返回 400。该接口为调用方接口，无需鉴权。
- 老库升级时会把升级那刻的存量状态（开关 / 强制 / 冻结 / 组 / 落定）铺一条基线，
  升级前的中间改动无法追溯，升级后的每次改动都可重放。

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
GET '/api/flags/<name>/check?identity=<用户身份>[&attrs=<URL编码的JSON属性>]'
# => {"flag":"new-checkout","identity":"u123","enabled":true,"reason":"rollout"}
#    reason ∈ kill_switch | freeze | depends_on | override | targeting | group | rollout | default，表示结果由哪一层决定
#    identity / attrs 需 URL 编码；identity 允许包含斜杠、空格、引号等任意字符
#    attrs 必须是扁平 JSON 对象，值为标量（字符串/数字/布尔/null）；非法返回 400

GET '/api/bundle?identity=<用户身份>[&attrs=<URL编码的JSON属性>][&version=<手中的整包版本>]'
# => {"identity":"u123","version":"9f2c…","flags":{"new-checkout":{"enabled":true,"reason":"rollout"}, …}}
#    带了 attrs 时响应多一个 attrs 字段；整包按 (身份, 属性) 分别记账
#    带 version 时响应多一个 valid 字段：false 即该版本对此身属性已过期，响应里是新版本与新结果

GET '/api/history?identity=<用户身份>&at=<unix秒>[&attrs=<URL编码的JSON属性>]'
# 问过去时刻每个开关的开/关（带当时配置）；at 必须是过去的时刻；详见「问某个过去的时刻」
# => {"identity":"u123","at":1789000000.0,"flags":{…与整包同结构…}}
```

### 管理端（需请求头 `X-Admin-Token`，可选 `X-Actor` 记录操作人）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/flags` | 列出所有开关 |
| POST | `/api/flags` | 新建 `{name, description, default_enabled, targeting}` |
| PATCH | `/api/flags/<name>` | 改 `{default_enabled, rollout_percent, kill_switch, targeting, depends_on, description}`；`targeting` 为属性打开条件 JSON（`{}`/`null` 清除）；`depends_on` 为被依赖开关名（`""`/`null` 清除，自依赖/成环 400）；带 `effective_at`（unix 秒）则约到该时刻生效 |
| DELETE | `/api/flags/<name>` | 删除开关（其未生效的定时变更一并取消） |
| GET | `/api/flags/<name>/overrides` | 列出单人强制 |
| PUT | `/api/flags/<name>/overrides` | 设置 `{identity, enabled}` |
| DELETE | `/api/flags/<name>/overrides` | 移除，body `{identity}` |
| GET | `/api/flags/<name>/freezes` | 列出被冻住结果的人（冻住的值、冻住时的理由、操作人） |
| PUT | `/api/flags/<name>/freezes` | 冻住某人此刻的结果，body `{identity}`；返回 `{changed, enabled, reason}`（重复冻且值未变则 `changed=false`） |
| DELETE | `/api/flags/<name>/freezes` | 解冻，body `{identity}`；解冻后立即按当时规则求值 |
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
| POST | `/api/drafts` | 开一个发布稿，body `{note?}`；返回 `draft_id` |
| GET | `/api/drafts` / `/api/drafts/<id>` | 未发布稿列表（`?all=1` 含已发布/已丢弃）/ 稿详情（含每个开关已收的改动） |
| PUT | `/api/drafts/<id>/flags/<name>` | 把对该开关的改动收进稿（字段合并，同字段后覆盖先；成环等只在发布时判） |
| DELETE | `/api/drafts/<id>/flags/<name>` | 从稿里摘掉该开关的整段改动 |
| POST | `/api/drafts/<id>/publish` | 发布整稿：合并后成环/目标缺失则 400 整稿不动，否则一起生效、整包换版本 |
| DELETE | `/api/drafts/<id>` | 丢弃未发布的稿（不影响任何求值与版本） |
| GET | `/api/bundles` | 已发出的整包（身份、属性哈希、版本、是否已过期 stale） |
| GET | `/api/bundles/invalidations?limit=100` | 整包失效记录：谁改了什么、让哪些人的哪身属性的整包从哪个版本变成哪个版本 |

示例：

```bash
# 一键全关
curl -X PATCH http://localhost:8000/api/flags/new-checkout \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" -d '{"kill_switch": true}'

# 50% 放量
curl -X PATCH http://localhost:8000/api/flags/new-checkout \
  -H "X-Admin-Token: $TOKEN" -d '{"rollout_percent": 50}'

# 按属性定打开条件：pro 套餐且等级为 3 或 5 的人来问才开，其他人按原规则
curl -X PATCH http://localhost:8000/api/flags/new-checkout \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" \
  -d '{"targeting": {"plan": "pro", "level": [3, 5]}}'
curl 'http://localhost:8000/api/flags/new-checkout/check?identity=u123&attrs=%7B%22plan%22%3A%22pro%22%2C%22level%22%3A3%7D'
# => {"flag":"new-checkout","identity":"u123","attrs":{"plan":"pro","level":3},"enabled":true,"reason":"targeting"}

# 指定依赖：promo-pay 必须等 pay 对同一个人是开时才可能开（pay 关则一律关）
curl -X PATCH http://localhost:8000/api/flags/promo-pay \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" -d '{"depends_on": "pay"}'
# 解除依赖：-d '{"depends_on": ""}'

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
