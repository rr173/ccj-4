# 特性开关服务（Feature Flags）

一套自包含的特性开关系统：管理页面 + 调用方查询接口，Flask + SQLite，单容器部署。

## 多环境隔离

管理端可以创建多个环境。每个环境都有独立的开关配置、冻结 / 强制 / 互斥组 /
发布稿 / 定时变更 / 历史，以及独立的整包账本和版本序号；一个环境里的改动不会
影响另一个环境。唯一跨环境的动作是管理端显式发起的**推送**（见「跨环境推送」
一节）：把源环境此刻的开关规则复制到目标环境，源全程只读。

```bash
# 创建环境（管理接口）
curl -X POST http://localhost:8000/api/environments \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" -d '{"name":"prod"}'
curl -X POST http://localhost:8000/api/environments \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" -d '{"name":"staging"}'

# 查询已有环境
curl http://localhost:8000/api/environments -H "X-Admin-Token: $TOKEN"
```

除 `/healthz`、管理页与创建环境接口外，所有接口都必须显式指定环境，二选一：

- 查询参数：`?environment=prod`（也接受短参数 `?env=prod`）
- 请求头：`X-Environment: prod`（也接受 `X-Env: prod`）

漏带环境返回 `400 {"error":"environment is required ..."}`；环境不存在返回
`404 {"error":"environment not found: ..."}`。调用方单查与拿整包都必须说环境；
环境决定访问哪份配置，普通查询响应不额外改变原有字段：

```bash
curl "http://localhost:8000/api/flags/new-checkout/check?environment=prod&identity=u123"
curl -H "X-Environment: staging" \
  "http://localhost:8000/api/bundle?identity=u123"
```

版本校验只在同一环境、同一身份、同一身属性内有效：
在 staging 改配置只会让 staging 的旧包过期；同一个人拿 prod 的包仍有效。
同一人、同一身属性、同一环境多次查询结果恒定。

环境名最长 64 个字符，首尾不能是空白，不能包含 `/`、`\\` 或控制字符。

## 求值优先级（固定，代码中不可调整）

```
全关(kill_switch) > 结果冻结(freeze) > 开关依赖(depends_on) > 单人强制(override) > 属性打开条件(targeting) > 互斥组(group) > 比例放量(rollout，含定档 variants) > 默认值(default)
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
  若**定了档（variants）**，这一层把所有人按档分完：落进哪一档就开并带回那一档的
  名字（`variant`），见「定档」一节；没定档的开关永远只回答开/关。
  若定了**放量条件**（`rollout_condition`），比例只对来问属性对上条件的人生效，
  没对上的直接落到默认值（见「条件按比例放量」一节）。
  若定了**有序放量规矩**（`rollout_rules`），放量层只看规矩：从上往下第一条
  对上的按它的比例定论，一条都对不上落到默认值（见「有序放量规矩」一节）。
  定了档时比例 / 放量条件 / 规矩都不参与求值，清空档后恢复。
- **默认值**：放量比例为 0（未启用放量），或定了放量条件但没对上时兜底。

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

## 条件按比例放量（rollout_condition）

管理端可以给放量比例再定一个「放量条件」（与 `targeting` 同形的 JSON 对象）：
**比例放量只对来问属性对上条件的人生效**——对上的人里按比例开一部分、剩下的
关；没对上的人（含没带属性来问的）不看比例，还按这个开关原来的默认值走：

```bash
# 只对 pro 套餐的人放 30%：pro 里三成开、七成关；其他人按默认值
curl -X PATCH http://localhost:8000/api/flags/new-checkout \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" \
  -d '{"rollout_percent": 30, "rollout_condition": {"plan": "pro"}}'

curl "http://localhost:8000/api/flags/new-checkout/check?identity=u123&attrs=%7B%22plan%22%3A%22pro%22%7D"
# 对上：{"enabled":true|false,"reason":"rollout"}（由分桶决定，这一层定论）
curl "http://localhost:8000/api/flags/new-checkout/check?identity=u456&attrs=%7B%22plan%22%3A%22free%22%7D"
# 没对上：{"enabled":…,"reason":"default"}（不看比例，按默认值）
```

- **条件形式**：与 `targeting` 完全相同（多键 AND、值列表 OR、标量精确匹配），
  传 `{}` 或 `null` 清除；没定条件时与只有比例时一字不差（比例对所有人分桶）。
- **结果恒定**：分桶只认开关与身份（`sha256("{flag}:{identity}") % 100`），
  条件只认来问属性——同一个人、同一身属性，问多少次、从哪台机器问都一样。
- **优先级**：全关、结果冻结、开关依赖、单人强制都排在它前面，照样压过它；
  对上的命中与放量命中一样算「自然结果为开」，在互斥组内仍要过组规则。
- **改比例或改条件 → 整包换新版本**：比例 > 0 且定了条件时，这个条件决定每个
  人走放量层还是默认值层，因此它是**所有人**（含不带属性的包）的求值输入——
  改比例或改条件，所有已发整包序号 +1 换新版本，拿着改前那包来问得到
  `valid=false`。比例为 0 时条件不参与求值，改它不动任何版本。
- `rollout_condition` 同样支持 `effective_at` 预约定时生效、可收进发布稿、
  可在预演里试；建开关时也可以直接带上。

## 有序放量规矩（rollout_rules）

管理端可以给一个开关定**好几条放量规矩**，一条一条往下写——每条是**一串要对上
的条件**（与 `targeting` 同形的非空 JSON 对象）和**一个比例**（0-100）：

```bash
# pro 套餐放 30%，free 套餐放 10%，其余人按默认值
curl -X PATCH http://localhost:8000/api/flags/new-checkout \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" \
  -d '{"rollout_rules": [{"condition": {"plan": "pro"}, "percent": 30},
                         {"condition": {"plan": "free"}, "percent": 10}]}'

curl "http://localhost:8000/api/flags/new-checkout/check?identity=u123&attrs=%7B%22plan%22%3A%22pro%22%7D"
# 对上第一条：{"enabled":true|false,"reason":"rollout"}（按 30% 分桶，这一层定论）
curl "http://localhost:8000/api/flags/new-checkout/check?identity=u456&attrs=%7B%22plan%22%3A%22ent%22%7D"
# 一条都对不上：{"enabled":…,"reason":"default"}（不看任何比例，按默认值）
```

- **从上往下对**：来问时按列表顺序对条件，**对上哪条就按那条的比例**分桶——
  命中开、未命中关（`reason=rollout`，这一层定论，不再看后面的规矩，也不落
  默认值）；**一条都对不上**（含没带属性来问的）不看任何比例，还按这个开关
  原来的默认值走（`reason=default`）。
- **条件形式**：与 `targeting` 完全相同（多键 AND、值列表 OR、标量精确匹配），
  每条的条件必须非空；比例是 0-100 的整数（0 = 对上的全关，100 = 对上的全开）。
  传 `[]` 或 `null` 清除全部规矩。
- **结果恒定**：分桶只认开关与身份（`sha256("{flag}:{identity}") % 100`），
  条件只认来问属性——同一个人、同一身属性，问多少次、从哪台机器问都一样。
- **优先级**：全关、结果冻结、开关依赖、单人强制都排在它前面，照样压过它；
  对上的命中与放量命中一样算「自然结果为开」，在互斥组内仍要过组规则。
- **与单条比例的关系**：定了规矩时，单条的 `rollout_percent` / `rollout_condition`
  **不参与求值**；清空规矩后老路恢复（没定规矩时与只有单条比例时一字不差）。
- **改任何一条 → 整包换新版本**：定了规矩时，哪个人走放量层、按哪条的比例走，
  都由这些规矩决定，因此它们是**所有人**（含不带属性的包）的求值输入——改任何
  一条的条件或比例（含增删、调序、清空），所有已发整包序号 +1 换新版本，拿着
  改前那包来问得到 `valid=false`。
- `rollout_rules` 同样支持 `effective_at` 预约定时生效、可收进发布稿、可在预演
  里试；建开关时也可以直接带上。

## 定档（variants：多档实验，回答「落在哪一档」）

管理端可以给一个开关**定几档**：每档一个名字、一个比例（0-100 的整数），
**几档的比例加起来必须刚好满 100**。定了档之后，放量层不再只回答开/关，而是把
来问的人按稳定分桶落进**恰好一档**：结果为开，并把这一档的名字随结果带回去；
**没定档的开关还是只回答开或关**，响应里没有 `variant` 这个键。

```bash
# 三档：对照 50% / 实验A 30% / 实验B 20%（比例和必须恰好 100）
curl -X PATCH http://localhost:8000/api/flags/new-checkout \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" \
  -d '{"variants": [{"name":"control","percent":50},
                    {"name":"treat-a","percent":30},
                    {"name":"treat-b","percent":20}]}'

curl "http://localhost:8000/api/flags/new-checkout/check?identity=u123"
# => {"flag":"new-checkout","identity":"u123","enabled":true,
#     "reason":"variants","variant":"treat-a"}
# 清空档（回到只回答开/关）：
#   -d '{"variants": []}'
```

- **落档规则**：沿用放量分桶 `sha256("{flag}:{identity}") % 100`，按档的书写顺序
  累加比例划区间（上例：0-49 落 control、50-79 落 treat-a、80-99 落 treat-b）。
  分桶只认开关与身份，**同一人、同一身属性（与带不带属性无关）问多少次、从哪台
  机器问，都落同一档**；档名在响应（check 与整包）里用 `variant` 字段带回。
- **校验**：至少一档；档名非空且不能重复；比例是 0-100 的整数（0% 合法，等于先
  占位、谁也落不进去，之后可调大）；**各档比例加起来不等于 100 一律 400 拒绝**
  （差一点都不收）。
- **改了就按新的算**：改某一档的名字或比例（含增删、调序、清空）立即生效——
  分桶值不变、落的区间按新比例重划；**只改名字，桶不换、带回去的名字换成新的**；
  改比例会让一些人移到相邻的档（不是重新随机）。档是**所有人（含不带属性的包）**
  的求值输入，任何一档改动都让已发整包序号 +1 换新版本，拿着改前那包来问得到
  `valid=false`。
- **与其他层的关系**：
  - 定档排在属性打开条件之后、原放量/默认值的位置（优先级⑦）。**属性命中（⑤）
    仍先定论为开**（`reason=targeting`），但只要开着且开关定了档，`variant` 同样
    按同一分桶补上——档名只看最终开不开，与由哪一层决定无关。
  - 定了档时这一层对所有人定论为开、不再落默认值；单条放量比例、放量条件与有序
    放量规矩都**不参与求值**（配置保留回显，清空档后老路恢复）。
  - **全关、结果冻结、开关依赖关、单人强制关**照样压过它：这些路径判关，响应里
    没有 `variant`。**单人强制开、互斥组争胜**这些开的路径也带此人此刻的档名。
  - 被别的开关**依赖**时，依赖者只看本开关的开/关，不看档名。
- **冻结**：冻在开且当时定了档的人，拿**冻住那一刻的档名快照**（与冻住那一刻挂的
  配置同口径），此后改档名/比例都不动他；重新冻但开/关没变也不刷新，想拿新档名需
  先解冻。若冻住后把整开关的档清空，则与「没定档只回答开/关」一致，不再带
  `variant`（历史重放冻结那一刻仍能还原当时档名）。
- 定档同样支持建开关时直接带上、`effective_at` 预约定时生效、收进发布稿、预演、
  跨环境推送（连同档名与比例一起推）与历史重放。整包、历史、预演的每个开关结果里，
  定档且为开的那项带 `variant` 字段，其余没有这个键。

## 身份合并（把几个身份收成同一个人）

同一个人可能用好几个身份来问（网页登录账号、App 设备号、第三方 open id……）。
管理端可以把**几个身份收成同一个人**：

```bash
# 把 alice / alice-app / alice-web 收成同一个人（至少两个）
curl -X POST http://localhost:8000/api/identities/merges \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" \
  -H "Content-Type: application/json" \
  -d '{"identities": ["alice", "alice-app", "alice-web"]}'
# => {"ok":true,"changed":true,"id":1,"primary":"alice",
#     "identities":["alice","alice-app","alice-web"]}
```

- **至少要写两个**：少于两个、不是非空字符串列表、身份有重复，这次收不成（400）
  并说清楚；一个身份都不写库。
- **已在另一拨人里就收不成**：要收的身份里只要有一个已经属于**另一拨**，这一次
  一个都不收（409），错误里点出是谁、已经在哪一拨。想改「谁跟谁是同一个人」，
  先把那一拨拆开再重新收；整拨原封不动再收一次是幂等的（`changed=false`，
  不换任何版本）。
- **按同一个人算**：收完之后，用其中**任何一个身份**来问（单查、整包、历史、
  预演），这个人对每个开关的开/关、理由、档名、整包版本都按同一个人算；换一个
  收在一起的身份来问，结果**一字不差**。主身份取成员里名字序最小者（与请求书写
  顺序无关），分桶、单人强制、结果冻结、互斥组落定都落在主身份名下。
- **管理端状态也合并**：给别名下单人强制 / 冻结果子，落在同一个人身上、对拨内
  所有身份生效。合并时各身份名下既有的强制 / 冻结 / 组落定 / 整包账本一起迁到
  主身份（同一开关或同一组冲突时主身份优先，否则取成员名字序最小者）。
- **拆开以后各算各的**：

  ```bash
  curl -X DELETE http://localhost:8000/api/identities/merges/1 \
    -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice"
  ```

  拆开不删状态：之前合并迁到主身份名下的强制 / 冻结 / 落定仍挂在主身份身上，
  其余身份回到自己名下独立的状态——从此这几个身份各算各的。
- **改过就按新的算**：合并 / 拆开 / 重新收都立即生效，相关已发整包换新版本，
  拿着改前那包来问得到 `valid=false`；历史重放也按「那一刻」的归属还原——合并前
  各算各的，合并期算同一个人，拆开后又各算各的。
- 身份合并是**每个环境各自一份**的（与强制 / 冻结 / 互斥组一样），跨环境推送只
  推开关规则、不推身份归属。`GET /api/identities/merges` 可查看当前每拨人
  （主身份排在首位）。

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

管理端改开关配置（全关 / 开关依赖 / 默认值 / 放量比例 / 放量条件 / 放量规矩 /
定档 / 属性打开条件 / 描述）时，可以带一个
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
  `rollout_percent`、`rollout_condition`（`{}` 清放量条件）、`rollout_rules`
  （`[]` 清放量规矩）、`variants`（`[]` 清档）、`targeting`
  （`{}` 清条件）、`depends_on`（`""`/`null` 解除）、
  `description`。**稿不支持 `effective_at`**：发布的一刻就是整稿的生效时刻。
- 若稿里某个开关在发布前被删除，发布会被整稿拒绝（先把该开关的改动从稿里摘掉即可）。

## 预演（改前先看一眼）

管理端要改某个开关、或手头有一稿还没发，可以先**点几个人**看一眼：这些人
现在每个开关开没开；**要是现在就改完、或者现在就把这稿发出去**，会变成什么样。
开着的开关带上那份配置。预演只是先看一眼——真的开关和稿都不会动：

```bash
# 情形一：要是现在就把 promo 改成「默认开 + 50% 放量」，u1/u2 会变成什么样
curl -X POST http://localhost:8000/api/preview \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" -H "Content-Type: application/json" \
  -d '{"identities": ["u1", "u2"], "flag": "promo",
       "changes": {"default_enabled": true, "rollout_percent": 50}}'

# 情形二：要是现在就把稿 #3 发出去（draft_id 与 flag+changes 二选一）
curl -X POST http://localhost:8000/api/preview \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" -H "Content-Type: application/json" \
  -d '{"identities": ["u1", "u2"], "draft_id": 3}'

# => {"mode":"changes","flag":"promo","changes":{"default_enabled":true,"rollout_percent":50},
#     "identities":{"u1":{"current":{…每个开关现在开没开…},
#                         "preview":{…改完后开没开…},
#                         "changed":["promo"]}, "u2":{…}}}
```

- **两份全量结果**：每个人给 `current`（现在每个开关开没开）与 `preview`
  （改完 / 发布后会变成什么样），结构与整包的 `flags` 相同（开着的带 `config`，
  冻住的人带冻住那一刻那份）；`changed` 列出结果会变的开关名
  （开/关、决定层或配置任一不同都算）。
- **真的什么都不动**：预演在「现在」的内存快照上套用假想改动求值——不落库、
  不换整包版本、不写组落定、不进审计与历史流水，稿还是 open。
- **全按现在的规矩算**：全关、冻结、依赖、单人强制、属性条件、互斥组、放量、
  默认全部照现网；**还没到点的定时变更与别的没发布的稿不算进去**。
  可带 `attrs`（JSON 对象）预演「带这身属性来问」的情形。
- **稿发不出去照实说**：稿若现在发布会失败（目标开关被删、合并后成环），
  响应带 `publishable:false` 与原因，`preview` 与 `current` 相同
  （发不出去就是什么都不会变）。
- **校验与真改同一口径**：改开关的预演按立即生效 PATCH 的规则校验（字段非法、
  自依赖、成环都 400），不支持 `effective_at`——预演的就是「现在就改完」。
  稿预演按发布时的整稿合并校验判定 `publishable`。
- 一次最多点 100 个人；身份放在 JSON body 里，特殊字符无需转义。

## 跨环境推送（push）

管理端可以把**一批开关从一个环境此刻的规则一次性推到另一个环境**——写明
从哪来、到哪去、推哪几个开关：

```bash
curl -X POST http://localhost:8000/api/push \
  -H "X-Admin-Token: $TOKEN" -H "X-Actor: alice" -H "Content-Type: application/json" \
  -d '{"source": "staging", "target": "prod", "flags": ["new-checkout", "promo-pay"]}'
# => {"ok":true,"source":"staging","target":"prod",
#     "pushed":["new-checkout","promo-pay"],"created":["promo-pay"],"updated":["new-checkout"]}
```

（`source`/`target` 也可写成 `from`/`to`；该接口是目录级操作，环境写在 body 里，
不需要 `?environment=`。）

- **少写了推不成**：`source` / `target` / `flags` 缺一、`flags` 不是非空的开关
  名单、源与目标是同一个环境，都返回 400 并说明。
- **写了个没有的环境推不成**：源或目标环境不存在返回 404，并指明是哪一边；
  点名的开关在源里不存在同样 404 列出缺的，一个都不推。
- **推成后**：目标环境里这些开关按源**此刻**的规则算（默认值 / 全关 / 放量比例 /
  放量条件 / 放量规矩 / 定档 / 属性打开条件 / 挂的配置 / 依赖，连同描述）；目标里
  没有的开关就地新建。**没点名的还是目标自己的**，一个字段都不动。
- **源一份都不被改掉**：源环境全程只读——规则、审计、历史、整包账本与版本，
  这次推送一律不碰。
- **依赖按名字落到目标**：被依赖的开关必须在推送后的目标里存在（目标已有或这次
  一起推），否则这次一个都不推，返回 400 说明缺谁。
- **会成环就一个都不推**：若这么推会让目标里的开关互相绕着依赖（成环 / 自依赖），
  这一次一个开关都不改，目标保持推之前的规则，返回 400 说明；审计里记
  `push_rejected` 与原因。
- **目标环境上的一次原子变更**：全部校验通过后一个事务写入，记审计与历史流水，
  目标已发整包统一重算、换新版本（失效记录里能查到这次推送）。
- **目标自己的状态不抄也不清**：目标本地的单人强制、结果冻结、互斥组与落定、
  定时变更、发布稿都是目标自己的状态——推送只覆盖开关本身的规则，与「在目标上
  直接改这些开关」同一口径（冻住的人仍按冻住的算，等等）。

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
  放量未命中、默认关）不带；**冻住的人按冻住的算**，带冻住那一刻的配置快照与档名
  快照，全关期间同样不带。定了档且为开的开关带当时的 `variant`。
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
#    reason ∈ kill_switch | freeze | depends_on | override | targeting | group | rollout | variants | default，表示结果由哪一层决定
#    定了档且最终为开时还带 "variant":"<档名>"；没定档的开关永远没有 variant 键
#    identity / attrs 需 URL 编码；identity 允许包含斜杠、空格、引号等任意字符
#    attrs 必须是扁平 JSON 对象，值为标量（字符串/数字/布尔/null）；非法返回 400

GET '/api/bundle?identity=<用户身份>[&attrs=<URL编码的JSON属性>][&version=<手中的整包版本>]'
# => {"identity":"u123","version":"9f2c…","flags":{"new-checkout":{"enabled":true,"reason":"rollout"}, …}}
#    定了档且为开的开关还带 "variant":"<档名>"；没定档的开关没有 variant 键
#    带了 attrs 时响应多一个 attrs 字段；整包按 (身份, 属性) 分别记账
#    带 version 时响应多一个 valid 字段：false 即该版本对此身属性已过期，响应里是新版本与新结果

GET '/api/history?identity=<用户身份>&at=<unix秒>[&attrs=<URL编码的JSON属性>]'
# 问过去时刻每个开关的开/关（带当时配置）；at 必须是过去的时刻；详见「问某个过去的时刻」
# => {"identity":"u123","at":1789000000.0,"flags":{…与整包同结构…}}
```

### 管理端（需请求头 `X-Admin-Token`，可选 `X-Actor` 记录操作人）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/environments` | 列出所有环境 |
| POST | `/api/environments` | 创建环境 `{name}` |
| POST | `/api/push` | 跨环境推送 `{source, target, flags}`（也接受 `from`/`to`）：把源环境此刻的规则推到目标；少写/环境没有/会成环都推不成并说明，源全程只读 |
| GET | `/api/flags` | 列出所有开关 |
| POST | `/api/flags` | 新建 `{name, description, default_enabled, targeting, rollout_condition, rollout_rules, variants}` |
| PATCH | `/api/flags/<name>` | 改 `{default_enabled, rollout_percent, rollout_condition, rollout_rules, variants, kill_switch, targeting, depends_on, description}`；`targeting` 为属性打开条件 JSON（`{}`/`null` 清除）；`rollout_condition` 为放量条件 JSON（`{}`/`null` 清除）；`rollout_rules` 为有序放量规矩列表（`[]`/`null` 清除）；`variants` 为定档列表 `[{"name","percent"}]`（档名非空不重复、比例 0-100、和必须恰好 100；`[]`/`null` 清除）；`depends_on` 为被依赖开关名（`""`/`null` 清除，自依赖/成环 400）；带 `effective_at`（unix 秒）则约到该时刻生效 |
| DELETE | `/api/flags/<name>` | 删除开关（其未生效的定时变更一并取消） |
| GET | `/api/flags/<name>/overrides` | 列出单人强制 |
| PUT | `/api/flags/<name>/overrides` | 设置 `{identity, enabled}` |
| DELETE | `/api/flags/<name>/overrides` | 移除，body `{identity}` |
| GET | `/api/flags/<name>/freezes` | 列出被冻住结果的人（冻住的值、冻住时的理由、档名快照、操作人） |
| PUT | `/api/flags/<name>/freezes` | 冻住某人此刻的结果，body `{identity}`；返回 `{changed, enabled, reason}`（重复冻且值未变则 `changed=false`） |
| DELETE | `/api/flags/<name>/freezes` | 解冻，body `{identity}`；解冻后立即按当时规则求值 |
| GET | `/api/groups` | 列出互斥组（含成员开关、`updated_by` 最近修改人） |
| POST | `/api/groups` | 新建组 `{name, description}` |
| DELETE | `/api/groups/<name>` | 解散组（落定记录一并清除） |
| PUT | `/api/groups/<name>/flags` | 开关进组，body `{flag}`（已在别组则 409） |
| DELETE | `/api/groups/<name>/flags` | 开关移出组，body `{flag}`（释放其落定记录） |
| GET | `/api/identities/merges` | 列出每拨「收成同一个人」的身份（主身份排首位） |
| POST | `/api/identities/merges` | 把几个身份收成同一个人，body `{identities:[…]}`；至少两个，某个身份已在另一拨则 409 并说明，整拨原样重收为幂等 no-op |
| DELETE | `/api/identities/merges/<id>` | 拆开这拨人；拆开后这几个身份各算各的 |

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
| POST | `/api/preview` | 预演（只读，什么都不动）：点名几个人，看「现在」与「改完/发布后」每个开关开没开；body `{identities, attrs?, flag+changes 或 draft_id}` |
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
| `FLAG_DB` | `/data/flags.db` | 环境目录库路径前缀；环境目录在 `<前缀>_environments.db`，各环境库在 `<前缀>_envs/` |
| `PORT` | `8000` | 监听端口 |

## 本地开发

```bash
pip install -r requirements.txt
ADMIN_TOKEN=test python app.py
```
