"""特性开关服务：管理端 + 调用方查询接口。

环境隔离：管理端可以创建多个环境（POST /api/environments）。每个环境使用独立
SQLite 库，开关、配置、冻结、强制、互斥组、发布稿、定时变更、历史与整包账本
都完全隔离。除健康检查、环境列表 / 创建和管理页外，所有接口都必须显式带环境：
?environment=<名字>（也接受 ?env）或 X-Environment 请求头（也接受 X-Env）；
漏带返回 400，环境不存在返回 404。
管理端在一个环境内改了会影响求值的东西，只会推进这个环境里相关整包的版本；
拿着这个环境改前的版本来问得到 valid=false，同一人在另一个环境里拿自己的包
仍然 valid=true。求值输入摘要里也带环境名，版本不能跨环境冒充。同一人、同一身
属性、同一环境多次查询仍是纯函数结果。

求值优先级（固定，不可配置）：
    1. 全关（kill switch）        -> 一律关
    1.5 对照组（control group）   -> 被管理端点进对照组的人，不看冻结 / 依赖 /
       强制 / 属性 / 组 / 放量 / 定档，每个开关只走它自己的默认开或关
       （reason=control）；没进的人不受这层影响
    2. 结果冻结（freeze）          -> 此人对本开关冻住的那一刻的结果，与之后的一切配置改动无关
    3. 开关依赖（depends_on）     -> 依赖的开关对此人此身属性不是开，本开关必须关
    4. 单人强制（override）        -> 强制开 / 强制关
    5. 属性打开条件（targeting）   -> 来问带的属性全对上则开；对不上落到下面
    6. 互斥组（group）            -> 组内同一身份最多一个开
    7. 比例放量（rollout）         -> 定了档（variants）时这一层把人按档分完：
       每档一个名字、一个比例，各档加起来恰好 100，谁来问都落进恰好一档，本层
       定论为开并把这一档的名字带回去（reason=variants），没有「关」的余口；
       没定档时，定了有序放量规矩（rollout_rules）只看规矩：
       从上往下第一条对上的按它的比例定论（命中开、未命中关），一条都对不上落到
       默认值；没定规矩时走单条比例（rollout_percent，可再定放量条件
       rollout_condition）：比例 > 0 时此层定论，命中开、未命中关；定了放量
       条件时只有来问属性对上条件的人走这一层，没对上的（含没带属性）直接落到
       默认值
    8. 默认值（default_enabled）   -> 没定档、没启用放量（比例为 0 且没定规矩），或定了
       放量条件 / 规矩但来问属性一条都没对上时兜底

结果冻结（freeze）：管理端可以把某个人对某个开关「此刻」的结果冻住。冻住时
系统先按当时的全部规则（依赖、强制、属性以不带属性的口径、互斥组、放量、默认）
完整求一次值，把得到的开/关存下来；此后该人再来问（单查、整包、带不带属性），
只要本开关没被全关压着，一律给冻住的那个结果——改默认值、放量比例、属性条件、
依赖关系（乃至给此人加单人强制、改互斥组）都不动它；被别的开关依赖时，依赖者
看到的也是这个冻住的结果。全关是唯一的例外：本开关一旦全关，冻住期间也一律关
（reason=kill_switch），全关解除后仍回到冻住的值。解冻后没有任何残留，立刻按
解冻当时的规则求值。冻 / 解冻都让该身份的已发整包（含他拿过的各身属性包）整包
换新版本，拿着冻/解冻前那包来问算过期；冻住期间改其他配置，该开关对此人的结果
不变，但配置变更本身仍按原规则让相关整包推进版本。

开关依赖（depends_on）：管理端可以指定本开关「先看另一个开关」。来问时
（单查或整包），用同一个身份、同一身属性把被依赖的开关完整求值一遍：
被依赖的是开，本开关才有机会继续往下算；被依赖的是关，本开关一律判关
（reason=depends_on）——这一层在单人强制之前，所以对本开关的强制开也救
不回来，只有本开关的全关能压过它（全关仍然一律关）。依赖可以成链
（C 依赖 B 依赖 A，A 关则 B、C 都关），但不能互相绕着依赖：自依赖与
成环在管理端写入时直接拒绝。被依赖开关被删除时，依赖关系自动解除。

属性打开条件：调用方来问时可以带上「这个人身上的属性」（attrs，一个 JSON
对象，如 {"plan":"pro","level":3}）。管理端给开关定一个条件（targeting，
同样是 JSON 对象）：条件里每个键都在来问属性里且值相等才算对上（多键 AND），
条件值写成列表表示任一对上即可（同键 OR）。对上了这一层直接开；对不上、
没带属性或开关没定条件，都按原来的规则（放量 / 默认值）算。判定是纯函数，
同一人、同一身属性，问多少次结果都一样。属性命中与放量命中一样算「自然
结果为开」，开关若在互斥组内仍要过组规则；全关与单人强制仍然优先于它。

互斥组（mutex group）：管理端可把多个开关编入同一组，一个开关最多进一组。
对同一身份，组内最多一个开关为开：第 4/6/7 层判开后，若开关在组内，则由
组规则裁决——该身份在组内已落定过别的开关则判关，否则落定为本开关并
写库；之后身份不变，落定的开关不变（落定只认身份，与本次带没带属性、
带了什么属性无关）。全关、开关依赖与单人强制优先于组规则
（强制开不受组内已有人开着的限制，全关仍然全关，依赖关着时强制开也开不了）；
未进组的开关按原规则求值，不参与组规则。

放量分桶：sha256("{flag_name}:{identity}") % 100，同一身份对同一开关
永远落在同一侧，与进程、机器、重启无关。

条件按比例放量（rollout_condition）：管理端可以给放量比例再定一个「放量
条件」（与属性打开条件同形的 JSON 对象）。定了之后，比例放量只对来问属性
对上条件的人生效：对上的人按 rollout_percent 分桶，命中开、未命中关
（reason=rollout，这一层定论）；没对上的人（含没带属性来问的）不看比例，
直接按这个开关原来的默认值走（reason=default）。没定条件时与只有比例时
完全一致。判定仍是纯函数：同一个人、同一身属性，问多少次结果都不变。
全关、结果冻结、开关依赖、单人强制都排在它前面，照样压过它；对上的命中
与放量命中一样算「自然结果为开」，在互斥组内仍要过组规则。条件是求值
输入的一部分：比例 > 0 且定了条件时，改比例或改条件都让已发出的整包
（含不带属性的包——没对上的人走哪一层也由这个条件决定）换新版本，拿着
改前那包来问算过期；比例为 0 时条件不参与求值，改它不动任何版本。

有序放量规矩（rollout_rules）：管理端可以给一个开关定好几条放量规矩，一条
一条往下写——每条是一串要对上的条件（与属性打开条件同形的非空 JSON 对象）
和一个比例（0-100）。来问时从上往下对：对上哪条就按那条的比例分桶，命中开、
未命中关（reason=rollout，这一层定论，不再看后面的规矩）；一条都对不上
（含没带属性来问的）不看任何比例，还按这个开关原来的默认值走
（reason=default）。定了规矩时，单条的放量比例与放量条件不参与求值；清空
规矩（[] / null）后老路恢复。判定仍是纯函数：分桶只认开关与身份，条件只认
来问属性——同一个人、同一身属性，问多少次结果都不变。全关、结果冻结、开关
依赖、单人强制都排在它前面，照样压过它；对上的命中与放量命中一样算「自然
结果为开」，在互斥组内仍要过组规则。规矩是求值输入的一部分：定了规矩时，
改任何一条的条件或比例（含增删、调序、清空）都让已发出的整包（含不带属性
的包——走哪一层由规矩决定）换新版本，拿着改前那包来问算过期；没定规矩时
它不参与求值，摘要保持没这功能时的原样。

定档（variants）：管理端可以给一个开关定好几档，每档一个名字、一个比例
（0-100 的整数），各档比例加起来必须恰好 100。定了档之后来问，放量层不再
给「开/关」，而是按 sha256("{flag_name}:{identity}") % 100 的分桶把人落进
恰好一档（按档的书写顺序累加比例划区间），最终结果为开，并把这一档的名字
随 check / 整包带回去（variant 字段；reason=variants）——分桶只认开关与
身份，同一人、同一身属性，问多少次、从哪台机器问都落同一档，与带不带属性
无关。没定档（[] / null 清空）的开关仍然只回答开或关，响应里没有 variant
这个键。定了档时这一层对所有人定论为开、不再落默认值；单条放量比例、放量
条件与有序放量规矩都不参与求值（清空档后它们恢复）。全关、结果冻结、开关
依赖、单人强制仍排在它前面：这些层判关时没有档名；单人强制开、属性打开
条件、互斥组争胜这些「开」的路径也把此人此刻的档名一并带上（与由哪一层
决定无关）。被别的开关依赖时，依赖者只看开/关，不看档名。改某一档的名字
或比例（含增删、调序、清空）立即按新的算——分桶值不变、落的区间按新比例
重划，改名不换桶、只换带回去的名字；档是所有人（含不带属性的包）的求值
输入，任何一档改动都让已发整包换新版本，拿着改前那包来问算过期。冻住的人
拿冻住那一刻的档名快照（与冻住那一刻挂的配置同口径），此后改档不动他；
重新冻且开/关翻转才刷新，想拿新档名需先解冻。

整包（bundle）：调用方带身份（可再带一身属性），一次拿走所有开关的开/关
结果与整包版本。整包按 (身份, 属性) 分别记账：同一人带不同属性来拿是不同
的包、各自有版本；不带属性的包不受任何属性条件影响（管理端增改条件时它的
版本一字不变）。
版本 = 求值输入摘要（会影响此身份此身属性求值的全部输入，含开关之间的
依赖关系）+ 单调递增的内容序号：配置不变时同一 (身份, 属性) 反复来拿，
结果与版本都不变；管理端改动任何会影响此包的一层（包括依赖关系）后，
序号 +1，版本必变；配置改回去摘要虽复原，但序号不回头，旧版本永远不会
再有效。带 version 参数来问可校验手中的包是否过期。
已发出的整包落库，管理端每次变更后用与调用方来拿时完全相同的求值重算
各整包，内容变了的 (身份, 属性) 记入 bundle_invalidations——记下的新版本
与本人带同一身属性再来拿时拿到的是同一个。管理端可看到「谁改了什么、
让哪些人的整包失效了」。

定时生效（scheduled change）：管理端改开关配置时可带 effective_at 约一个
未来时刻。到点之前，求值、整包结果与整包版本都按原样（定时变更只躺在
scheduled_changes 表里，不参与求值）；到点之后，第一个进来的请求把变更
写到开关上（惰性应用，无需后台线程），此后按新规则求值、整包换新版本，
到点前发出的整包版本随之过期。不带 effective_at 的改动仍然改完即生效。
已约未生效的变更可取消；同一开关可约多个，到点按生效时间先后应用。

发布稿（draft）：管理端可以把好几处改动先收进同一稿（draft_changes 按
开关合并，同一字段后收的覆盖先收的）。稿只躺在 drafts / draft_changes
两张表里，发布前来问（单查、带属性问、拿整包）一律按现在的配置算，稿里
的改动一字不生效，整包版本也不动。发布时先把整稿与现网配置合并后统一
校验：稿里的开关都还在、依赖目标都存在、合并后的依赖图不成环——任何一条
不过，这一稿全都不生效（已写入的也回滚，稿仍是 open，可改可丢）；全部
通过才一次性应用、统一重算已发整包：稿里的改动一起生效，整包换新版本，
拿着发布前那包来问算过期。成环只在发布时按整稿合并图判（收入时各条改动
单独看都合法，所以 A 依赖 B、B 依赖 A 能各自收进同一稿，发布时才被一起
拒绝）。没发布的稿可以整条丢弃（不影响任何求值），也可以摘掉稿里某个
开关的改动。稿不支持 effective_at：发布时刻就是生效时刻。

预演（preview）：管理端改某个开关之前、或手里有一稿还没发，可以先点几个人
看一眼（POST /api/preview）：这些人现在每个开关开没开；要是现在就改完、
或者现在就把这稿发出去，会变成什么样。开着的开关带上配置。预演全程只读——
在「现在」的内存快照上套用假想改动后按同一套优先级求值，真的开关、稿、
整包版本、组落定、审计与历史流水一概不动；全关、冻结、依赖按现在的规矩算，
还没到点的定时变更与别的没发布的稿不算进去。稿若现在发不出去（目标开关
没了 / 合并成环），预演照实回答 publishable=false 与原因，结果与现在相同。

跨环境推送（push）：管理端可以把一批开关从源环境此刻的规则一次性推到另一个
环境（POST /api/push，body 写明 source / target / flags）。少写了字段、
环境不存在、点名的开关在源里没有，这次都推不成并说明；推成后目标环境里这些
开关按源此刻的规则算（目标没有的就地新建），没点名的还是目标自己的；源环境
全程只读，一份规则都不被这次改掉。若这么推会让目标里的开关互相绕着依赖
（或被依赖的开关在目标里不存在），这一次一个开关都不改，目标保持推之前的
规则并说明。推送是目标环境上的一次原子变更：记审计与历史流水、目标已发整包
统一换新版本；目标本地的单人强制、冻结、互斥组、定时变更与发布稿都是目标
自己的状态，推送不抄也不清。

整份规矩还原（restore，POST /api/restore，body {"at": unix 秒}）：把本环境
此刻「已生效」的整份开关规矩，一次性换成过去某一刻当时已生效的那份，口径与
/api/history 一致——只取 at 那一刻存在且生效的开关规则（描述不参与求值，保留
现网）；当时还没有或已删除的开关整个删掉，当时存在后来被删的按那一刻规矩重建。
约了没到点的改动、没发布的稿不算进那份：前者随这次还原全部取消（到点也不再
生效），后者原样保留。单人强制 / 冻结 / 互斥组与落定 / 对照名单 / 身份合并等
按人按组的状态不抄也不清。还原是该环境上的一次原子变更：记审计与历史流水
（append-only，只补「此刻」的新事件），已发整包统一换新版本；原样再还原一次
是幂等 no-op。少写环境 / at、环境不存在、at 非数字或在未来都换不成（400/404）。
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from functools import wraps

from flask import Flask, g, jsonify, render_template, request
from flask.testing import FlaskClient

DB_PATH = os.environ.get("FLAG_DB", "/data/flags.db")
_DB_BASE, _ = os.path.splitext(DB_PATH)
CATALOG_DB_PATH = _DB_BASE + "_environments.db"
ENV_DB_DIR = _DB_BASE + "_envs"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin-token")

app = Flask(__name__)

ENVIRONMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS environments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    db_file    TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS flags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL DEFAULT '',
    default_enabled INTEGER NOT NULL DEFAULT 0,
    rollout_percent INTEGER NOT NULL DEFAULT 0,
    -- 放量条件（canonical JSON）：''=没定，比例放量对所有人分桶；定了之后
    -- 只有来问属性对上条件的人按比例分桶，没对上的落到默认值
    rollout_condition TEXT NOT NULL DEFAULT '',
    -- 有序放量规矩（canonical JSON 列表）：''=没定。定了之后放量层只看规矩：
    -- 来问时从上往下对，第一条对上的按它的比例分桶定论；一条都对不上落到默认值
    rollout_rules TEXT NOT NULL DEFAULT '',
    -- 定档（canonical JSON 列表 [{name, percent}, …]）：''=没定档。定了之后
    -- 放量层把所有人按分桶落进恰好一档（各档比例加起来必须 100），结果为开
    -- 并带回档名；没定档时只回答开/关。改档立即按新比例/新名字算。
    variants TEXT NOT NULL DEFAULT '',
    kill_switch     INTEGER NOT NULL DEFAULT 0,
    targeting_rule  TEXT NOT NULL DEFAULT '',  -- 属性打开条件（canonical JSON）；''=未定条件
    -- 开关挂的配置（canonical JSON）：''=没挂。开关对此人判开时，这份配置随
    -- check / 整包一起发给此人；判关（含全关）时不带。冻住在开的人拿冻住那一刻
    -- 这份（freezes.frozen_config），此后改这里不动他；改这里只让此刻判开的人的
    -- 已发整包换新版本，判关的人与没挂配置时一样不受影响。
    flag_config     TEXT NOT NULL DEFAULT '',
    -- 本开关依赖的另一个开关：被依赖者对此人此身属性不是开时，本开关必须关；
    -- NULL=无依赖。被依赖开关删除时自动置空（ON DELETE SET NULL）
    depends_on_flag_id INTEGER REFERENCES flags(id) ON DELETE SET NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS overrides (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_id    INTEGER NOT NULL REFERENCES flags(id) ON DELETE CASCADE,
    identity   TEXT NOT NULL,
    enabled    INTEGER NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(flag_id, identity)
);
-- 结果冻结：管理端把 (开关, 人) 此刻的结果冻住。frozen_enabled 是冻住那一刻
-- 按当时完整规则求出的开/关；frozen_reason 记下它当时由哪一层决定（审计/展示用）。
-- 冻住后求值只认本行，与 flags/overrides/依赖/组的后续变化无关；全关仍压过。
-- 开关删除时冻结记录一并删除（ON DELETE CASCADE）。
CREATE TABLE IF NOT EXISTS freezes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_id       INTEGER NOT NULL REFERENCES flags(id) ON DELETE CASCADE,
    identity      TEXT NOT NULL,
    frozen_enabled INTEGER NOT NULL,
    frozen_reason TEXT NOT NULL DEFAULT '',
    -- 冻住那一刻开关挂的配置快照（canonical JSON）：冻住在开的人此后
    -- 单查/整包都带这份；冻住在关则恒为 ''（不带）。全关期间也不带。
    frozen_config TEXT NOT NULL DEFAULT '',
    -- 冻住那一刻此人落的档名快照（''=冻在关或当时开关没定档）：冻住在开且
    -- 当时定了档的人此后带这份档名；改档名/比例不动他，想拿新档需先解冻。
    frozen_variant TEXT NOT NULL DEFAULT '',
    created_by    TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    UNIQUE(flag_id, identity)
);
CREATE INDEX IF NOT EXISTS idx_freezes_identity ON freezes(identity);
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,
    flag_name  TEXT NOT NULL,
    layer      TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mutex_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    updated_by  TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES mutex_groups(id) ON DELETE CASCADE,
    flag_id  INTEGER NOT NULL REFERENCES flags(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, flag_id)
);
-- 一个开关最多进一个组
CREATE UNIQUE INDEX IF NOT EXISTS idx_group_members_flag ON group_members(flag_id);
-- (组, 身份) 的落定记录：该身份在组内唯一开着的开关，先到先得，不再更换
CREATE TABLE IF NOT EXISTS group_assignments (
    group_id   INTEGER NOT NULL REFERENCES mutex_groups(id) ON DELETE CASCADE,
    identity   TEXT NOT NULL,
    flag_id    INTEGER NOT NULL REFERENCES flags(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    PRIMARY KEY (group_id, identity)
);
-- 已发出的整包：(身份, 属性) -> 最近一次整包的版本与结果。
-- 同人带不同属性来拿是不同的包；attrs_hash 为 '' 表示没带属性的包。
CREATE TABLE IF NOT EXISTS bundles (
    identity     TEXT NOT NULL,
    attrs_hash   TEXT NOT NULL DEFAULT '',
    attrs_json   TEXT NOT NULL DEFAULT '',   -- 属性原文（canonical JSON），管理端展示用
    version      TEXT NOT NULL,              -- 最近一次发出的整包版本
    content_hash TEXT NOT NULL DEFAULT '',   -- 当前求值输入的内容摘要
    generation   INTEGER NOT NULL DEFAULT 0, -- 内容变化序号，只增不减
    results      TEXT NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (identity, attrs_hash)
);
-- 整包失效记录：哪次变更（谁、改了什么）让哪个 (身份, 属性) 的整包过期了
CREATE TABLE IF NOT EXISTS bundle_invalidations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,
    change      TEXT NOT NULL,
    identity    TEXT NOT NULL,
    attrs_hash  TEXT NOT NULL DEFAULT '',
    old_version TEXT NOT NULL,
    new_version TEXT NOT NULL,
    created_at  REAL NOT NULL
);
-- 定时生效的配置变更：到点前不参与求值、不影响整包版本；
-- 到点后由下一个进来的请求惰性应用（见 apply_due_scheduled_changes）
CREATE TABLE IF NOT EXISTS scheduled_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_id      INTEGER NOT NULL,          -- 目标开关；删除开关时未生效的预约一并取消
    flag_name    TEXT NOT NULL,             -- 冗余存一份名字，开关删了记录仍可读
    changes      TEXT NOT NULL,             -- JSON：到点要写入的字段与值
    effective_at REAL NOT NULL,             -- 生效时刻（unix 秒）
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending/applied/cancelled/failed
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    applied_at   REAL
);
-- 发布稿：好几处改动先收成一稿，发布时一起生效（原子）。
-- status: open（未发布，可继续收改动 / 丢弃）/ published（已发布）/ discarded（已丢弃）。
-- 稿只躺在本表里，不参与求值；只有发布才会写到开关上。
CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    published_at REAL
);
-- 稿内每个开关的改动：按 (稿, 开关) 合并（同一字段后收的覆盖先收的）。
-- 不加外键到 flags：开关在发布前被删掉时，发布校验负责整稿拒绝，记录本身仍可读。
CREATE TABLE IF NOT EXISTS draft_changes (
    draft_id   INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    flag_name  TEXT NOT NULL,             -- 冗余名字，与 scheduled_changes 同理
    changes    TEXT NOT NULL,             -- canonical JSON：合并后的字段与值
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (draft_id, flag_name)
);
-- 历史流水（append-only，只增不改不删）：管理端每一次「已经生效」的变更都落一条，
-- 供 GET /api/history 重放任意过去时刻的完整求值状态。
--   kind:
--     flag_upsert     开关新建 / 立即生效的 PATCH（payload 为写完后的求值字段整快照）
--     flag_delete     开关删除（其强制 / 冻结 / 组成员关系 / 组落定一并按删除语义清）
--     override / override_delete   单人强制的设置 / 移除（payload: {enabled}）
--     freeze / freeze_delete       结果冻结 / 解冻（payload: {enabled, reason, config}）
--     group_upsert / group_delete  互斥组建 / 解散
--     member_add / member_remove   开关进组 / 出组（subject=组名，payload: {flag}）
--     assignment                    某身份在组内落定（查询写副作用，payload: {flag}）
--     identity_merge / identity_split  把几个身份收成同一个人 / 拆开
--                                      （payload: {identities:[…], primary:"…"}，
--                                      merge 的迁移副作用不单独进流水，重放时按组
--                                      状态还原「那一刻这几个身份算同一个人」）
--     control_add / control_remove    点名进对照组 / 移出对照组（identity=主身份；
--                                      重放按时间序回放即还原那一刻的对照名单；
--                                      在对照层默认值接管一切，全关仍压过）
-- 预约生效与发布稿不在这里：重放时直接读 scheduled_changes（到点且未取消）与
-- drafts（status=published 且 published_at<=时刻），与本流水按时间合并。
CREATE TABLE IF NOT EXISTS history_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at REAL NOT NULL,
    kind        TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',   -- 开关名 / 组名
    identity    TEXT NOT NULL DEFAULT '',   -- override/freeze/assignment 对应身份
    payload     TEXT NOT NULL DEFAULT '',   -- canonical JSON
    actor       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_time ON history_events(occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_history_identity ON history_events(identity, occurred_at);
-- 身份合并（同一个人的几个身份）：一拨人一个 identity_groups 行，成员在
-- identity_members 里；is_primary=1 的那个身份是这拨人的「主身份」。
-- 所有按人存的状态（单人强制 / 结果冻结 / 组落定 / 整包账本）与分桶都按主身份
-- 算，所以来问时用这一拨里任何一个身份，每个开关的开/关都按同一个人算、一字
-- 不差。identity 上的唯一索引从库层面保证「一个身份最多在一拨人里」——要改谁
-- 跟谁是同一个人，得先拆开（DELETE 整组，成员行级联删除）再重新收。
CREATE TABLE IF NOT EXISTS identity_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_members (
    group_id  INTEGER NOT NULL REFERENCES identity_groups(id) ON DELETE CASCADE,
    identity  TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, identity)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_member_unique
    ON identity_members(identity);
-- 对照组：管理端点名的一拨人（identity 存主身份，与强制 / 冻结同口径）。
-- 进了对照组的人来问时不再按人分开算（不看冻结 / 依赖 / 强制 / 属性 / 组 /
-- 放量 / 定档），每个开关只走它自己的默认开或关（reason=control），全关仍压
-- 过一切。没进的人照旧走各开关原来的算法。点名 / 改名单立即生效，相关已发
-- 整包换新版本。老库从空名单起步，与升级前一字不差。
CREATE TABLE IF NOT EXISTS control_group (
    identity   TEXT NOT NULL PRIMARY KEY,
    created_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

LAYERS = ("kill_switch", "control", "freeze", "depends_on", "override",
          "targeting", "group", "rollout", "default")


# ---------------------------------------------------------------- attrs / targeting

def validate_attrs(attrs):
    """校验「这个人身上的属性」对象：键为非空字符串、值为标量的扁平 dict。
    非法抛 ValueError。parse_attrs 与预演接口（body 里直接给 JSON 对象）共用。"""
    if not isinstance(attrs, dict):
        raise ValueError("attrs must be a JSON object")
    for k, v in attrs.items():
        if not isinstance(k, str) or k == "":
            raise ValueError("attr keys must be non-empty strings")
        if isinstance(v, bool) or v is None or isinstance(v, (str, int, float)):
            continue
        raise ValueError(f"attr '{k}' must be a scalar (string/number/bool/null)")
    return attrs


def parse_attrs(raw):
    """解析调用方来问时带的属性（查询参数 attrs，URL 编码的 JSON 对象）。

    只接受「键为字符串、值为标量（string/number/bool/null）」的扁平对象；
    返回规范化后的 dict（原样大小写，不做 strip——属性值也是精确匹配的一部分）。
    缺省 / 空串表示没带属性；非法输入抛 ValueError，由接口转 400。
    """
    if raw is None or raw == "":
        return None
    try:
        attrs = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("attrs must be URL-encoded JSON, e.g. %7B%22plan%22%3A%22pro%22%7D")
    return validate_attrs(attrs)


def scalar_equal(a, b):
    """属性值精确相等：bool 与 number 不互等（True ≠ 1），其余按 JSON 语义。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def validate_attr_condition(rule, what):
    """校验「属性条件」形规则（targeting 与 rollout_condition 同形），返回规范化 JSON 串。

    形式：{键: 标量} 或 {键: [标量, …]}；多个键之间是 AND，
    一个键给多个值时是 OR。None / {} 表示清除条件。非法抛 ValueError。
    what 是字段名，只用于报错文案。
    """
    if rule is None:
        return ""
    if not isinstance(rule, dict):
        raise ValueError(f"{what} must be a JSON object"
                         " (use {} or null to clear)")
    if not rule:
        return ""  # 空对象 = 清除条件

    def check_scalar(v, where):
        if isinstance(v, bool) or v is None or isinstance(v, (str, int, float)):
            return
        raise ValueError(f"{what} value at {where} must be a scalar"
                         " (string/number/bool/null)")

    norm = {}
    for k, v in rule.items():
        if not isinstance(k, str) or k == "":
            raise ValueError(f"{what} keys must be non-empty strings")
        if isinstance(v, list):
            if not v:
                raise ValueError(f"{what} list for '{k}' must not be empty")
            for item in v:
                check_scalar(item, f"'{k}'")
            norm[k] = v
        else:
            check_scalar(v, f"'{k}'")
            norm[k] = v
    return canonical_json(norm)


def validate_targeting(rule):
    """校验管理端定的属性打开条件，返回规范化 JSON 串（见 validate_attr_condition）。"""
    return validate_attr_condition(rule, "targeting")


def validate_rollout_condition(rule):
    """校验管理端定的放量条件，返回规范化 JSON 串（与 targeting 同形）。"""
    return validate_attr_condition(rule, "rollout_condition")


def validate_rollout_rules(rules):
    """校验「有序放量规矩」，返回规范化 JSON 串（'' = 没定规矩）。

    形式：[{"condition": {键: 标量|[…]}, "percent": 0-100}, …]；列表顺序即
    优先级——来问时从上往下对，第一条对上的按它的比例分桶。每条的条件必须是
    非空的属性条件（与 targeting 同形；空条件对不上任何人，不如不写这条）。
    None / [] 表示清除规矩。非法抛 ValueError。
    """
    if rules is None:
        return ""
    if not isinstance(rules, list):
        raise ValueError("rollout_rules must be a list of rules"
                         ' ({"condition": {...}, "percent": 0-100});'
                         " use [] or null to clear")
    if not rules:
        return ""
    if len(rules) > 100:
        raise ValueError("rollout_rules: at most 100 rules")
    norm = []
    for i, r in enumerate(rules):
        where = f"rollout_rules[{i}]"
        if not isinstance(r, dict) or set(r) != {"condition", "percent"}:
            raise ValueError(f"{where} must be an object with exactly"
                             ' "condition" and "percent"')
        cond_json = validate_attr_condition(r["condition"], f"{where}.condition")
        if not cond_json:
            raise ValueError(f"{where}.condition must be a non-empty condition"
                             " ({} matches no one; drop the rule instead)")
        pct = r["percent"]
        if isinstance(pct, bool) or not isinstance(pct, int) or not 0 <= pct <= 100:
            raise ValueError(f"{where}.percent must be an int in [0,100]")
        norm.append({"condition": json.loads(cond_json), "percent": pct})
    return canonical_json(norm)


def validate_variants(variants):
    """校验「定档」，返回规范化 JSON 串（'' = 没定档）。

    形式：[{"name": "对照", "percent": 50}, {"name": "实验", "percent": 50}]；
    列表顺序即分桶区间的顺序（从上往下累加比例划区间）。每档要有一个非空
    名字（允许任意字符，含空白，不做 strip）和一个 0-100 的整数比例；档名不
    能重复；至少一档；各档比例加起来必须**恰好 100**（差一点都不收）。
    比例为 0 的档合法（写出来先占位，谁也落不进去，之后可调大）。
    None / [] 表示清除档（返回 ''）。非法抛 ValueError。
    """
    if variants is None:
        return ""
    if not isinstance(variants, list):
        raise ValueError("variants must be a list of tiers"
                         ' ({"name": "...", "percent": 0-100});'
                         " use [] or null to clear")
    if not variants:
        return ""
    if len(variants) > 100:
        raise ValueError("variants: at most 100 tiers")
    norm = []
    names = set()
    total = 0
    for i, v in enumerate(variants):
        where = f"variants[{i}]"
        if not isinstance(v, dict) or set(v) != {"name", "percent"}:
            raise ValueError(f"{where} must be an object with exactly"
                             ' "name" and "percent"')
        name = v["name"]
        if not isinstance(name, str) or name == "":
            raise ValueError(f"{where}.name must be a non-empty string")
        if name in names:
            raise ValueError(f"variants tier names must be unique"
                             f" (duplicate name: {name!r})")
        pct = v["percent"]
        if isinstance(pct, bool) or not isinstance(pct, int) or not 0 <= pct <= 100:
            raise ValueError(f"{where}.percent must be an int in [0,100]")
        names.add(name)
        total += pct
        norm.append({"name": name, "percent": pct})
    if total != 100:
        raise ValueError(f"variants percents must sum to exactly 100"
                         f" (got {total})")
    return canonical_json(norm)


def condition_dict_matches(cond, attrs):
    """条件（dict 形态）对不对得上来问属性：条件每个键都在属性里且值相等
    （列表值任一即可）。空条件 / 没带属性一律算对不上。"""
    if not cond or not attrs:
        return False
    for k, wanted in cond.items():
        if k not in attrs:
            return False
        got = attrs[k]
        values = wanted if isinstance(wanted, list) else [wanted]
        if not any(scalar_equal(got, w) for w in values):
            return False
    return True


def targeting_matches(rule_json, attrs):
    """属性对没对上条件：条件每个键都在属性里且值相等（列表值任一即可）。"""
    if not rule_json:
        return False
    return condition_dict_matches(json.loads(rule_json), attrs)


def rollout_gate_open(rollout_condition_json, attrs):
    """条件放量的门：没定条件（''）对所有人开；定了条件只对来问属性对上的人开
    （没带属性一律算对不上）。门关上的人不看比例，直接落到默认值。"""
    return not rollout_condition_json or targeting_matches(rollout_condition_json, attrs)


def validate_config(value):
    """校验管理端给开关挂的配置，返回规范化 JSON 串。

    配置可以是任意 JSON 值（对象 / 数组 / 标量），随开的结果一起下发；
    None 表示清除配置（返回 ""）。NaN / Infinity 不是合法 JSON，拒绝。
    """
    if value is None:
        return ""

    def check(v, path="$"):
        if v is None or isinstance(v, (str, bool, int)):
            return
        if isinstance(v, float):
            if not math.isfinite(v):
                raise ValueError(f"config at {path} must be finite JSON (no NaN/Infinity)")
            return
        if isinstance(v, dict):
            for k, vv in v.items():
                if not isinstance(k, str):
                    raise ValueError("config object keys must be strings")
                check(vv, f"{path}.{k}")
            return
        if isinstance(v, list):
            for i, vv in enumerate(v):
                check(vv, f"{path}[{i}]")
            return
        raise ValueError(f"config at {path} is not a JSON value")

    check(value)
    return canonical_json(value)


def parse_flag_config(flag_config_json):
    """把库里存的配置 JSON 串还原成值；没挂（''）时返回 None。"""
    return json.loads(flag_config_json) if flag_config_json else None


# ---------------------------------------------------------------- environment / db helpers

def validate_environment_name(name):
    """校验环境名：每个环境是独立配置与整包账本，名字只允许安全的短标识。"""
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ValueError("environment is required and must not have surrounding whitespace")
    if len(name) > 64:
        raise ValueError("environment must be at most 64 characters")
    if re.search(r"[\x00-\x1f\x7f/\\]", name):
        raise ValueError("environment must not contain control characters, '/' or '\\'")
    return name


def environment_db_path(name):
    """环境物理库路径：只由校验后的名字摘要决定，避免路径穿越并保持文件名稳定。"""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    return os.path.join(ENV_DB_DIR, f"{digest}.db")


def connect_sqlite(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_catalog_db():
    if "catalog_db" not in g:
        g.catalog_db = connect_sqlite(CATALOG_DB_PATH)
    return g.catalog_db


def init_catalog():
    conn = connect_sqlite(CATALOG_DB_PATH)
    conn.executescript(ENVIRONMENT_SCHEMA)
    conn.commit()
    conn.close()


def init_environment_db(name, actor_name="system"):
    """创建环境登记与独立 SQLite 库；已存在时直接返回现有路径。"""
    validate_environment_name(name)
    path = environment_db_path(name)
    init_catalog()
    conn = connect_sqlite(CATALOG_DB_PATH)
    try:
        conn.execute(
            "INSERT INTO environments (name, db_file, created_by, created_at)"
            " VALUES (?,?,?,?)",
            (name, path, actor_name, time.time()),
        )
        conn.commit()
        created = True
    except sqlite3.IntegrityError:
        created = False
    conn.close()
    env_conn = connect_sqlite(path)
    env_conn.executescript(SCHEMA)
    env_conn.commit()
    env_conn.close()
    return path, created


def get_environment_record(name):
    if not name:
        return None
    return get_catalog_db().execute(
        "SELECT * FROM environments WHERE name=?", (name,)
    ).fetchone()


def request_environment():
    """读取本次请求显式指定的环境。查询参数 / 头都可用，给了多个且不一致则拒绝。"""
    candidates = [request.args.get("environment"), request.args.get("env"),
                  request.headers.get("X-Environment"), request.headers.get("X-Env")]
    values = [v for v in candidates if v]
    if len(set(values)) > 1:
        raise ValueError("environment parameters must match"
                         " (use environment/env or X-Environment/X-Env)")
    raw = values[0] if values else None
    if raw is None or raw == "":
        return None
    return validate_environment_name(raw)


def require_request_environment():
    """所有开关与整包接口都必须显式指定一个已存在的环境。"""
    try:
        name = request_environment()
    except ValueError as e:
        return None, (jsonify({"error": str(e)}), 400)
    if name is None:
        return None, (jsonify({"error": "environment is required"
                                          " (use ?environment=<name>, ?env=<name>,"
                                          " X-Environment or X-Env header)"}), 400)
    rec = get_environment_record(name)
    if rec is None:
        return None, (jsonify({"error": f"environment not found: {name}"}), 404)
    return name, None


def get_db():
    if "db" not in g:
        name = getattr(g, "environment", None)
        if not name:
            raise RuntimeError("database access requires a request environment")
        rec = get_environment_record(name)
        if rec is None:
            raise RuntimeError(f"environment not found: {name}")
        g.db = connect_sqlite(rec["db_file"])
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()
    catalog = g.pop("catalog_db", None)
    if catalog is not None:
        catalog.close()


def init_db():
    """初始化环境目录；每个已登记环境独立执行建表 / 老库迁移。"""
    init_catalog()
    catalog = connect_sqlite(CATALOG_DB_PATH)
    env_rows = catalog.execute("SELECT name, db_file FROM environments ORDER BY id").fetchall()
    catalog.close()
    for env in env_rows:
        _init_environment_schema(env["db_file"])


def _init_environment_schema(path):
    conn = connect_sqlite(path)
    conn.executescript(SCHEMA)
    # 老库迁移：bundles 增加 content_hash / generation（旧行下次来拿时按新规则重算）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bundles)")}
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE bundles ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
    if "generation" not in cols:
        conn.execute("ALTER TABLE bundles ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
    # 属性条件：flags 增加 targeting_rule
    flag_cols = {r[1] for r in conn.execute("PRAGMA table_info(flags)")}
    if "targeting_rule" not in flag_cols:
        conn.execute("ALTER TABLE flags ADD COLUMN targeting_rule TEXT NOT NULL DEFAULT ''")
    # 条件按比例放量：flags 增加 rollout_condition（老库一律从没定条件起步，
    # 求值与整包摘要与升级前一字不差）
    if "rollout_condition" not in flag_cols:
        conn.execute("ALTER TABLE flags ADD COLUMN rollout_condition TEXT NOT NULL DEFAULT ''")
    # 有序放量规矩：flags 增加 rollout_rules（老库一律从没定规矩起步，
    # 求值与整包摘要与升级前一字不差）
    if "rollout_rules" not in flag_cols:
        conn.execute("ALTER TABLE flags ADD COLUMN rollout_rules TEXT NOT NULL DEFAULT ''")
    # 定档：flags 增加 variants（老库一律从没定档起步，求值与整包摘要与
    # 升级前一字不差）
    if "variants" not in flag_cols:
        conn.execute("ALTER TABLE flags ADD COLUMN variants TEXT NOT NULL DEFAULT ''")
    # 开关依赖：flags 增加 depends_on_flag_id（老库一律从无依赖起步；
    # 不能在 ALTER 上加外键，但删除路径里同样会把指向已删开关的依赖置空）
    if "depends_on_flag_id" not in flag_cols:
        conn.execute("ALTER TABLE flags ADD COLUMN depends_on_flag_id INTEGER")
    # 开关挂的配置：flags 增加 flag_config（老库一律从没挂配置起步）
    if "flag_config" not in flag_cols:
        conn.execute("ALTER TABLE flags ADD COLUMN flag_config TEXT NOT NULL DEFAULT ''")
    # 结果冻结快照：freezes 增加 frozen_config（老的冻结行从空快照起步，
    # 与「冻住期间改配置不动冻住的人」一致；想带新配置需重新冻一次）
    freeze_cols = {r[1] for r in conn.execute("PRAGMA table_info(freezes)")}
    if "frozen_config" not in freeze_cols:
        conn.execute("ALTER TABLE freezes ADD COLUMN frozen_config TEXT NOT NULL DEFAULT ''")
    # 定档：freezes 增加 frozen_variant（老的冻结行从空档名起步，与「冻住期间
    # 改档不动冻住的人」一致；想拿档名需重新冻一次）
    if "frozen_variant" not in freeze_cols:
        conn.execute("ALTER TABLE freezes ADD COLUMN frozen_variant TEXT NOT NULL DEFAULT ''")
    # 整包按 (身份, 属性) 分别记账：把旧的「身份主键」整包表重建为复合主键，
    # 已发的老包原样保留（它们是不带属性的包，attrs_hash=''）
    pk = conn.execute("PRAGMA table_info(bundles)").fetchall()
    if any(r[5] for r in pk) and "attrs_hash" not in cols:
        conn.executescript(
            "ALTER TABLE bundles RENAME TO bundles_old;"
            "CREATE TABLE bundles ("
            " identity TEXT NOT NULL, attrs_hash TEXT NOT NULL DEFAULT '',"
            " attrs_json TEXT NOT NULL DEFAULT '', version TEXT NOT NULL,"
            " content_hash TEXT NOT NULL DEFAULT '',"
            " generation INTEGER NOT NULL DEFAULT 0, results TEXT NOT NULL,"
            " updated_at REAL NOT NULL, PRIMARY KEY (identity, attrs_hash));"
            "INSERT INTO bundles (identity, attrs_hash, attrs_json, version,"
            " content_hash, generation, results, updated_at)"
            " SELECT identity, '', '', version, content_hash, generation,"
            " results, updated_at FROM bundles_old;"
            "DROP TABLE bundles_old;"
        )
    inv_cols = {r[1] for r in conn.execute("PRAGMA table_info(bundle_invalidations)")}
    if "attrs_hash" not in inv_cols:
        conn.execute("ALTER TABLE bundle_invalidations"
                     " ADD COLUMN attrs_hash TEXT NOT NULL DEFAULT ''")
    # 历史流水：老库第一次升级时把「此刻」的存量状态铺成一条基线（以各记录自己的
    # 创建时间落事件），升级前发生过的中间改动无法追溯，但升级后新发生的每次
    # 管理端改动都会进流水、可按任意过去时刻重放。新库（还没有任何开关）不铺。
    hist_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "history_events" in hist_tables:
        n = conn.execute("SELECT COUNT(*) c FROM history_events").fetchone()["c"]
        if n == 0:
            for r in conn.execute("SELECT * FROM flags"):
                conn.execute(
                    "INSERT INTO history_events"
                    " (occurred_at, kind, subject, identity, payload, actor)"
                    " VALUES (?, 'flag_upsert', ?, '', ?, 'system-migration')",
                    (r["created_at"], r["name"], canonical_json({
                        "default_enabled": bool(r["default_enabled"]),
                        "rollout_percent": r["rollout_percent"],
                        "rollout_condition": r["rollout_condition"],
                        "rollout_rules": r["rollout_rules"],
                        "variants": r["variants"],
                        "kill_switch": bool(r["kill_switch"]),
                        "targeting": r["targeting_rule"],
                        "config": r["flag_config"],
                        "depends_on": "",   # 基线里的依赖名下面补
                    })))
            dep_partials = []
            for r in conn.execute("SELECT * FROM flags"):
                if r["depends_on_flag_id"] is not None:
                    pname = conn.execute(
                        "SELECT name FROM flags WHERE id=?",
                        (r["depends_on_flag_id"],)).fetchone()
                    if pname is not None:
                        dep_partials.append((r["created_at"], r["name"],
                                             pname["name"]))
            for created_at, name, dep_name in dep_partials:
                conn.execute(
                    "INSERT INTO history_events"
                    " (occurred_at, kind, subject, payload, actor)"
                    " VALUES (?, 'flag_upsert', ?, ?, 'system-migration')",
                    (created_at, name,
                     canonical_json({"depends_on": dep_name})))
            for r in conn.execute(
                    "SELECT o.*, f.name AS fname FROM overrides o"
                    " JOIN flags f ON f.id=o.flag_id"):
                conn.execute(
                    "INSERT INTO history_events"
                    " (occurred_at, kind, subject, identity, payload, actor)"
                    " VALUES (?, 'override', ?, ?, ?, 'system-migration')",
                    (r["created_at"], r["fname"], r["identity"],
                     canonical_json({"enabled": bool(r["enabled"])})))
            for r in conn.execute(
                    "SELECT z.*, f.name AS fname FROM freezes z"
                    " JOIN flags f ON f.id=z.flag_id"):
                conn.execute(
                    "INSERT INTO history_events"
                    " (occurred_at, kind, subject, identity, payload, actor)"
                    " VALUES (?, 'freeze', ?, ?, ?, ?)",
                    (r["created_at"], r["fname"], r["identity"],
                     canonical_json({"enabled": bool(r["frozen_enabled"]),
                                     "reason": r["frozen_reason"],
                                     "config": r["frozen_config"],
                                     "variant": r["frozen_variant"]}),
                     r["created_by"] or "system-migration"))
            for r in conn.execute("SELECT * FROM mutex_groups"):
                conn.execute(
                    "INSERT INTO history_events"
                    " (occurred_at, kind, subject, actor)"
                    " VALUES (?, 'group_upsert', ?, ?)",
                    (r["created_at"], r["name"], r["updated_by"] or "system-migration"))
            for r in conn.execute(
                    "SELECT g.name AS gname, f.name AS fname, g.created_at AS gat"
                    " FROM group_members m JOIN mutex_groups g ON g.id=m.group_id"
                    " JOIN flags f ON f.id=m.flag_id"):
                conn.execute(
                    "INSERT INTO history_events"
                    " (occurred_at, kind, subject, payload, actor)"
                    " VALUES (?, 'member_add', ?, ?, 'system-migration')",
                    (r["gat"], r["gname"], canonical_json({"flag": r["fname"]})))
            for r in conn.execute(
                    "SELECT a.*, g.name AS gname, f.name AS fname"
                    " FROM group_assignments a JOIN mutex_groups g ON g.id=a.group_id"
                    " JOIN flags f ON f.id=a.flag_id"):
                conn.execute(
                    "INSERT INTO history_events"
                    " (occurred_at, kind, subject, identity, payload, actor)"
                    " VALUES (?, 'assignment', ?, ?, ?, 'system-migration')",
                    (r["created_at"], r["gname"], r["identity"],
                     canonical_json({"flag": r["fname"]})))
    conn.commit()
    conn.close()


def audit_to(db, actor_name, flag_name, layer, action, detail=""):
    """往指定环境的库落一条审计（跨环境推送要写给目标环境，不走请求环境）。"""
    db.execute(
        "INSERT INTO audit_log (actor, flag_name, layer, action, detail, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (actor_name, flag_name, layer, action, detail, time.time()),
    )


def audit(actor, flag_name, layer, action, detail=""):
    audit_to(get_db(), actor, flag_name, layer, action, detail)


def flag_snapshot_payload(flag_row, dep_name=None):
    """一个开关写完后的求值字段整快照（写进历史流水的 flag_upsert payload）。

    立即生效的每次 PATCH 都存全量快照，历史重放时按时间序最后一条快照即该
    时刻的开关状态；预约 / 发布稿在重放时只覆盖自己改的字段。targeting /
    config 存库里那种 canonical 串（'' = 未定/未挂），depends_on 存名字
    （'' = 不依赖）。
    """
    if dep_name is None:
        dep_name = ""
        if flag_row["depends_on_flag_id"] is not None:
            row = get_db().execute(
                "SELECT name FROM flags WHERE id=?",
                (flag_row["depends_on_flag_id"],)).fetchone()
            dep_name = row["name"] if row else ""
    return {
        "default_enabled": bool(flag_row["default_enabled"]),
        "rollout_percent": flag_row["rollout_percent"],
        "rollout_condition": flag_row["rollout_condition"],
        "rollout_rules": flag_row["rollout_rules"],
        "variants": flag_row["variants"],
        "kill_switch": bool(flag_row["kill_switch"]),
        "targeting": flag_row["targeting_rule"],
        "config": flag_row["flag_config"],
        "depends_on": dep_name,
    }


def record_history(db, occurred_at, kind, subject="", identity="", payload=None,
                   actor_name=None):
    """往 append-only 的历史流水落一条已经生效的变更。"""
    db.execute(
        "INSERT INTO history_events"
        " (occurred_at, kind, subject, identity, payload, actor)"
        " VALUES (?,?,?,?,?,?)",
        (occurred_at, kind, subject, identity,
         canonical_json(payload) if payload is not None else "",
         actor_name if actor_name is not None else actor()),
    )


def flag_to_dict(row, dep_name=None):
    return {
        "name": row["name"],
        "description": row["description"],
        "default_enabled": bool(row["default_enabled"]),
        "rollout_percent": row["rollout_percent"],
        "rollout_condition": (json.loads(row["rollout_condition"])
                              if row["rollout_condition"] else {}),
        "rollout_rules": (json.loads(row["rollout_rules"])
                          if row["rollout_rules"] else []),
        "variants": (json.loads(row["variants"]) if row["variants"] else []),
        "kill_switch": bool(row["kill_switch"]),
        "targeting": json.loads(row["targeting_rule"]) if row["targeting_rule"] else {},
        "config": parse_flag_config(row["flag_config"]),
        "depends_on": dep_name if dep_name is not None else "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------- 身份合并（同一个人）

def canonical_identity(db, identity):
    """把来问的身份归到他所属那拨人的「主身份」。

    没被收进任何一拨人时原样返回；收过之后，用这一拨里任何一个身份来问，
    都返回同一个主身份——单人强制 / 结果冻结 / 组落定 / 分桶 / 整包账本全部
    按主身份算，所以「换一个收在一起的身份来问，每个开关的开/关必须一样」。
    """
    row = db.execute(
        "SELECT m2.identity AS primary_identity FROM identity_members m1"
        " JOIN identity_members m2 ON m2.group_id = m1.group_id AND m2.is_primary=1"
        " WHERE m1.identity=?",
        (identity,),
    ).fetchone()
    return row["primary_identity"] if row is not None else identity


def identity_group_of(db, identity):
    """身份所属那拨人的 (group_id, [主身份, 其余成员…按名字序])；没收过返回 None。"""
    row = db.execute(
        "SELECT group_id FROM identity_members WHERE identity=?", (identity,),
    ).fetchone()
    if row is None:
        return None
    members = [r["identity"] for r in db.execute(
        "SELECT identity FROM identity_members WHERE group_id=?"
        " ORDER BY is_primary DESC, identity", (row["group_id"],))]
    return row["group_id"], members


def list_identity_groups(db):
    """列出每拨人：主身份排在首位，其余按名字序。"""
    out = []
    for g in db.execute("SELECT id, created_by, created_at FROM identity_groups ORDER BY id"):
        members = [r["identity"] for r in db.execute(
            "SELECT identity FROM identity_members WHERE group_id=?"
            " ORDER BY is_primary DESC, identity", (g["id"],))]
        out.append({"id": g["id"], "primary": members[0] if members else "",
                    "identities": members,
                    "created_by": g["created_by"], "created_at": g["created_at"]})
    return out


def _rekey_person_state(db, aliases, primary):
    """把 aliases 各身份名下「按人存的状态」合并迁到主身份 primary 名下。

    合并后一切求值只看主身份，所以老状态必须跟着人一起搬：
      - overrides / freezes：同一开关下多个身份都有记录时主身份优先，
        主身份没有则取成员里名字序最小的那份（确定的次序，与请求书写顺序无关）；
      - group_assignments：同一个互斥组下同理（只留一个落定，避免主键冲突）；
      - bundles：同一个 (身份, 属性) 包同样主身份优先、否则取最近一次来拿的。
    其余别名名下的旧行一律删掉，不留「拆开后复活」的影子状态。
    返回迁移过程中从 (旧身份, 属性) 归到主身份名下的整包行
    [(identity, attrs_hash), …]，供路由决定要不要补失效记录。
    """
    others = [a for a in aliases if a != primary]
    if not others:
        return []
    members = [primary] + sorted(others)

    def merge_flag_scoped(table, value_cols, conflict_order):
        """合并「(flag_id, identity)」作用域的表（overrides / freezes）。"""
        # 先删掉所有别名名下的行，再按 (flag_id) 选出每个开关的唯一赢家写回主身份
        rows = db.execute(
            f"SELECT * FROM {table} WHERE identity IN ({','.join('?' * len(members))})",
            members,
        ).fetchall()
        by_flag = {}
        for r in rows:
            by_flag.setdefault(r["flag_id"], []).append(r)
        db.execute(
            f"DELETE FROM {table} WHERE identity IN ({','.join('?' * len(members))})",
            members,
        )
        for flag_id, rs in by_flag.items():
            winner = conflict_order(rs)
            cols = ["flag_id", "identity"] + value_cols
            placeholders = ",".join("?" * len(cols))
            db.execute(
                f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                [flag_id, primary] + [winner[c] for c in value_cols],
            )

    def priority_winner(rs):
        # 主身份优先；主身份没有则按身份名字序最小（members 即此次序）
        order = {ident: i for i, ident in enumerate(members)}
        return sorted(rs, key=lambda r: order[r["identity"]])[0]

    merge_flag_scoped(
        "overrides",
        ["enabled", "created_at"],
        priority_winner,
    )
    merge_flag_scoped(
        "freezes",
        ["frozen_enabled", "frozen_reason", "frozen_config", "frozen_variant",
         "created_by", "created_at"],
        priority_winner,
    )

    # 组落定：作用域是 (group_id, identity)——同一互斥组内这拨人只能占一个开关
    arows = db.execute(
        "SELECT * FROM group_assignments WHERE identity IN"
        f" ({','.join('?' * len(members))})",
        members,
    ).fetchall()
    by_group = {}
    for r in arows:
        by_group.setdefault(r["group_id"], []).append(r)
    db.execute(
        "DELETE FROM group_assignments WHERE identity IN"
        f" ({','.join('?' * len(members))})",
        members,
    )
    for group_id, rs in by_group.items():
        winner = priority_winner(rs)
        db.execute(
            "INSERT INTO group_assignments (group_id, identity, flag_id, created_at)"
            " VALUES (?,?,?,?)",
            (group_id, primary, winner["flag_id"], winner["created_at"]),
        )

    # 对照组归属也是「按人存的状态」：任一成员在对照组里，合并后这拨人（主
    # 身份）就在；重复行用 INSERT OR IGNORE 收敛成一行。别名名下的旧行删掉，
    # 不留下「拆开后别名还在对照组里」的影子——与强制 / 冻结的迁移口径一致。
    now_ctrl = time.time()
    ctrl_rows = db.execute(
        f"SELECT identity FROM control_group WHERE identity IN"
        f" ({','.join('?' * len(members))})", members).fetchall()
    ctrl_before = {r["identity"] for r in ctrl_rows}
    db.execute(
        f"DELETE FROM control_group WHERE identity IN"
        f" ({','.join('?' * len(members))})", members)
    if ctrl_before:
        db.execute(
            "INSERT OR IGNORE INTO control_group"
            " (identity, created_by, created_at, updated_at) VALUES (?,?,?,?)",
            (primary, "identity-merge", now_ctrl, now_ctrl))

    # 整包账本：作用域是 (identity, attrs_hash)。主身份名下的包优先；都在别名
    # 名下时取最近一次来拿的（updated_at 最大）那行，并把同属性的旧别名行删掉。
    rekeyed = []
    brows = db.execute(
        "SELECT * FROM bundles WHERE identity IN"
        f" ({','.join('?' * len(members))})",
        members,
    ).fetchall()
    by_attrs = {}
    for r in brows:
        by_attrs.setdefault(r["attrs_hash"], []).append(r)
    for attrs_hash, rs in by_attrs.items():
        primary_rows = [r for r in rs if r["identity"] == primary]
        if primary_rows:
            keep = primary_rows[0]
        else:
            keep = sorted(rs, key=lambda r: r["updated_at"])[-1]
        # 删掉这拨人名下该属性的全部旧行，再以主身份写回赢家
        db.execute(
            "DELETE FROM bundles WHERE attrs_hash=? AND identity IN"
            f" ({','.join('?' * len(members))})",
            [attrs_hash] + members,
        )
        for r in rs:
            if r["identity"] != keep["identity"]:
                rekeyed.append((r["identity"], attrs_hash))
        db.execute(
            "INSERT INTO bundles (identity, attrs_hash, attrs_json, version,"
            " content_hash, generation, results, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (primary, keep["attrs_hash"], keep["attrs_json"], keep["version"],
             keep["content_hash"], keep["generation"], keep["results"],
             keep["updated_at"]),
        )
    return rekeyed


# ---------------------------------------------------------------- 对照组（control group）

def in_control_group(db, identity):
    """此人（主身份）此刻是否在管理端点进的对照组里。

    在对照组里的人来问不再按人分开算：冻结 / 依赖 / 单人强制 / 属性条件 /
    互斥组 / 放量 / 定档一律不看，每个开关只走它自己的默认开或关（全关仍压
    过一切）。点名 / 改名单立即生效，所以这里只认 control_group 当前行。
    """
    return db.execute(
        "SELECT 1 FROM control_group WHERE identity=?", (identity,),
    ).fetchone() is not None


def list_control_group(db):
    """列出对照组现在点着哪些人（主身份，名字序）。"""
    return [r["identity"] for r in db.execute(
        "SELECT identity FROM control_group ORDER BY identity")]


# ---------------------------------------------------------------- evaluation

def bucket_of(flag_name, identity):
    """确定性分桶：同一 (flag, identity) 永远得到 0-99 中同一个数。"""
    digest = hashlib.sha256(f"{flag_name}:{identity}".encode("utf-8")).hexdigest()
    return int(digest, 16) % 100


def variant_of(variants_json, flag_name, identity):
    """定档分桶：按档的书写顺序累加比例划区间，返回此人此刻落的档名。

    分桶值只认开关与身份（bucket_of）；档名/比例怎么改，桶值不变，落的区间
    按新比例重划、带回去的名字按新档名给。没定档（''）返回 None。
    """
    if not variants_json:
        return None
    b = bucket_of(flag_name, identity)
    upto = 0
    for v in json.loads(variants_json):
        upto += v["percent"]
        if b < upto:
            return v["name"]
    return None  # 理论不可达：写入时各档比例和已校验为恰好 100


def rollout_layer_verdict(name, identity, rollout_percent, rollout_condition_json,
                          rollout_rules_json, attrs):
    """放量层的定论：命中返回 (enabled, "rollout")；本层不定论（落默认值）返回 None。

    定了有序放量规矩（rollout_rules 非空）时只看规矩：从上往下第一条对上的
    规矩按它的比例分桶，命中开、未命中关，这一层定论；一条都对不上（含没带
    属性）本层不定论——不看任何比例，落默认值。没定规矩时走老路：比例 > 0
    且过了放量条件的门（没定条件 = 对所有人开）时按 rollout_percent 分桶定论，
    否则本层不定论。分桶只认开关与身份，条件只认来问属性——同一个人、同一身
    属性，问多少次结果都不变。evaluate 与 evaluate_at 共用这一段，保证线上
    求值与历史重放 / 预演同一口径。
    """
    if rollout_rules_json:
        for r in json.loads(rollout_rules_json):
            if condition_dict_matches(r["condition"], attrs):
                return bucket_of(name, identity) < r["percent"], "rollout"
        return None
    if rollout_percent > 0 and rollout_gate_open(rollout_condition_json, attrs):
        return bucket_of(name, identity) < rollout_percent, "rollout"
    return None


def group_of(db, flag_id):
    """开关所在的互斥组 id；未进组返回 None。"""
    row = db.execute(
        "SELECT group_id FROM group_members WHERE flag_id=?", (flag_id,),
    ).fetchone()
    return row["group_id"] if row else None


def evaluate(db, flag, identity, attrs=None, _memo=None, _chain=None,
             _skip_freeze_id=None):
    """按固定优先级求值，返回 (enabled, reason, config, variant)。

    config 是「此人此刻拿得到的开关配置」：只看最终开/关，与由哪一层决定
    无关——最终为开且开关挂了配置，就带上该配置（对象/数组/标量原样）；
    最终为关（全关、依赖关、强制关、组内没争到、放量未命中、默认关）或开关
    没挂配置，config 一律为 None（接口里不带这个键）。

    variant 是「此人此刻落的档名」：只看最终开/关，与由哪一层决定无关——
    最终为开且开关定了档，就带上档名（放量定档层按分桶落档；强制开 / 属性
    命中 / 组内争胜这些开的路径也按同一分桶补算此人的档）；最终为关或开关
    没定档，variant 一律为 None（接口里不带这个键）。冻在开且当时定了档的人
    拿冻住那一刻的档名快照。

    attrs 为来问时带的属性（dict）。属性条件命中时这一层直接定论为开；
    对不上 / 没带属性 / 开关没定条件，都按原来的放量 / 默认值算。
    定了档（variants 非空）时，放量层把所有人按分桶落进恰好一档：本层定论
    为开、带档名（reason=variants），不再看单条比例、放量条件、有序规矩与
    默认值；没定档时放量层维持原来的开/关口径。

    结果冻结在全关之后、开关依赖之前：冻住后直接返回冻住那一刻的结果
    （reason=freeze），不再看依赖、强制、属性、组、放量与默认值；因此
    改依赖也救不回/压不掉冻住的值。冻在开的人同时拿到冻住那一刻挂着的
    配置快照（frozen_config）与档名快照（frozen_variant），此后改挂的配置
    或改档都不动他；冻在关则两者都不带。
    被别的开关依赖时，依赖者沿备忘拿到的就是这个冻住的结果。全关仍在更
    前面，冻住也压不过全关（全关一律关、不带配置与档名）。

    对照组排在全关之后、冻结之前：此人在对照组里时，冻结 / 依赖 / 强制 /
    属性 / 组 / 放量 / 定档一概不看，本开关直接给默认值（reason=control，
    不带档名、不写组落定）；离开对照组后这些层立刻恢复。
    _skip_freeze_id 仅供「重新冻住」时顶层使用：按假设此开关没冻的口径
    求它此刻的结果（沿依赖递归时被依赖开关的冻结照常生效）。
    依赖链上的结果在单次求值内备忘，保证链上每个开关只算一次、结果一致；
    成环在管理端写入时已拒绝，运行时再兜一层防环。
    """
    _COMPUTE = object()  # 档名出口哨兵：没显式给档名时按当前分桶补算

    def ret(enabled, reason, config_json=None, variant=_COMPUTE):
        """统一出口：开且挂了配置才随结果带配置；关一律不带。
        开且定了档才带档名；variant 没显式给（哨兵）时按当前分桶补算此人
        的档，显式给 None（如档已清空的冻住结果）则不带。"""
        cfg = parse_flag_config(config_json) if enabled and config_json else None
        if not enabled:
            variant = None
        elif variant is _COMPUTE:
            variant = variant_of(flag["variants"], flag["name"], identity)
        return enabled, reason, cfg, variant

    if flag["kill_switch"]:
        return ret(False, "kill_switch")

    # 对照组：管理端点进对照组的人不再按人分开算——不看冻结 / 依赖 / 强制 /
    # 属性 / 互斥组 / 放量 / 定档，每个开关只走它自己的默认开或关。全关已在
    # 更前面压过；对照组里没有档名（不定论档），也不会写入组落定。
    if in_control_group(db, identity):
        return ret(bool(flag["default_enabled"]), "control",
                   flag["flag_config"], None)

    # 结果冻结：只认冻住那一刻存下的开/关（连同配置与档名快照），此后其他层
    # 怎么改都不影响它。
    if _skip_freeze_id != flag["id"]:
        freeze = db.execute(
            "SELECT frozen_enabled, frozen_config, frozen_variant FROM freezes"
            " WHERE flag_id=? AND identity=?",
            (flag["id"], identity),
        ).fetchone()
        if freeze is not None:
            enabled = bool(freeze["frozen_enabled"])
            if _memo is not None:
                _memo[flag["id"]] = enabled
            # 档名快照只在此开关此刻仍定着档时才带回；档已清空则与「没定档
            # 的开关只回答开/关」一致（快照仍留在库里，历史重放仍可还原）
            frozen_variant = (freeze["frozen_variant"] or None) if flag["variants"] else None
            return ret(enabled, "freeze", freeze["frozen_config"], frozen_variant)

    # 开关依赖：被依赖的开关对此人此身属性不是开，则必须关。
    # 对本开关的强制开排在依赖之后，救不回依赖关着的情形。
    dep_id = flag["depends_on_flag_id"]
    if dep_id is not None:
        if _chain is not None and dep_id in _chain:
            # 防御性兜底：环依赖写入时已拒绝，理论不可达
            raise RuntimeError(f"dependency cycle through flag id {dep_id}")
        dep_flag = db.execute("SELECT * FROM flags WHERE id=?", (dep_id,)).fetchone()
        if dep_flag is None:
            # 被依赖开关已删除（正常路径下外键会把这里置空，这里兜底）
            db.execute("UPDATE flags SET depends_on_flag_id=NULL WHERE id=?",
                       (flag["id"],))
        else:
            dep_enabled = False
            if _memo is not None and dep_id in _memo:
                dep_enabled = _memo[dep_id]
            else:
                next_chain = {flag["id"]} if _chain is None else _chain | {flag["id"]}
                dep_enabled, _, _, _ = evaluate(db, dep_flag, identity, attrs,
                                                _memo, next_chain)
            if not dep_enabled:
                if _memo is not None:
                    _memo[flag["id"]] = False
                return ret(False, "depends_on")

    override = db.execute(
        "SELECT enabled FROM overrides WHERE flag_id=? AND identity=?",
        (flag["id"], identity),
    ).fetchone()
    if override is not None:
        enabled = bool(override["enabled"])
        if _memo is not None:
            _memo[flag["id"]] = enabled
        return ret(enabled, "override", flag["flag_config"])

    # 属性打开条件：来问属性全对上则开，且不落到定档 / 放量 / 默认值
    if targeting_matches(flag["targeting_rule"], attrs):
        natural = True
        reason = "targeting"
    # 定档：定了档时这一层把所有人按分桶落进恰好一档，定论为开并带档名
    # （reason=variants），不再往下落放量 / 默认值。
    elif flag["variants"]:
        natural = True
        reason = "variants"
    else:
        # 放量层：定了有序规矩只看规矩（从上往下第一条对上的定论，都对不上
        # 落默认）；没定规矩走老路（比例 > 0 且过了放量条件的门才定论，
        # 定了条件但没对上的人不看比例，落到默认值）
        verdict = rollout_layer_verdict(
            flag["name"], identity, flag["rollout_percent"],
            flag["rollout_condition"], flag["rollout_rules"], attrs)
        if verdict is not None:
            natural, reason = verdict
        else:
            natural = bool(flag["default_enabled"])
            reason = "default"

    group_id = group_of(db, flag["id"])
    if group_id is None or not natural:
        if _memo is not None:
            _memo[flag["id"]] = natural
        return ret(natural, reason, flag["flag_config"])

    # 互斥组裁决：仅当自然结果为开才参与（属性命中也算）。组内同一身份
    # 最多一个开，先到先得，落定只认身份、与本次带的属性无关。
    winner = db.execute(
        "SELECT flag_id FROM group_assignments WHERE group_id=? AND identity=?",
        (group_id, identity),
    ).fetchone()
    if winner is None:
        # INSERT OR IGNORE + 重读：并发请求下也只有一个人能落定成功
        now = time.time()
        db.execute(
            "INSERT OR IGNORE INTO group_assignments"
            " (group_id, identity, flag_id, created_at) VALUES (?,?,?,?)",
            (group_id, identity, flag["id"], now),
        )
        # 落定是查询的写副作用：同样落一条历史流水，历史重放到「这一刻真来问」
        # 时才能还原这个身份当时在组内落给了谁（重放本身不写库，这里是写库路径）
        gname_row = db.execute("SELECT name FROM mutex_groups WHERE id=?",
                               (group_id,)).fetchone()
        record_history(db, now, "assignment",
                       subject=gname_row["name"] if gname_row else "",
                       identity=identity, payload={"flag": flag["name"]},
                       actor_name="")
        db.commit()
        winner = db.execute(
            "SELECT flag_id FROM group_assignments WHERE group_id=? AND identity=?",
            (group_id, identity),
        ).fetchone()
    won = winner["flag_id"] == flag["id"]
    if _memo is not None:
        _memo[flag["id"]] = won
    return ret(won, "group", flag["flag_config"])


# ---------------------------------------------------------------- bundle

def attrs_hash_of(attrs):
    """一身属性的确定性摘要；空属性（没带属性）记为空串。"""
    if not attrs:
        return ""
    return hashlib.sha256(canonical_json(attrs).encode("utf-8")).hexdigest()[:16]


def bundle_content_hash(db, identity, attrs=None, results=None):
    """求值输入摘要：对「会影响此身份此身属性求值结果的全部输入」做确定性摘要。

    覆盖：所有开关的求值相关字段、此身份的单人强制、此身份的结果冻结、
    互斥组成员关系、此身份在组内的落定记录、开关之间的依赖关系。带属性来拿时，
    某个开关的属性条件只在此人属性对得上（即该条件实际参与了此人求值）时才进
    摘要——改一个此人对不上的条件不影响他的包，对得上的条件增删改必换版本；
    不带属性时摘要与没有「属性条件」这一层时一字不差——管理端怎么增改条件，
    不带属性的老包都不失效。
    开关挂的配置只在「此人此刻对该开关判开」时随结果下发，因此也只在判开时
    进摘要：改挂的配置只让此刻判开的人的包换新版本，判关的人（含全关期间）
    版本一字不变；没挂配置的开关与以前完全一致。results 必须是 compute_bundle
    刚求出的结果（失效扫描与本人来拿走同一套求值，摘要口径才一致）。
    放量条件（rollout_condition）：比例 > 0 且定了条件时，它决定每个人走放量
    层还是默认值层（对上的人被它分进放量，没对上的被它挡回默认），因此是
    所有人（含不带属性的包）的求值输入——改比例或改条件，所有已发整包都换
    新版本；比例为 0 时条件不参与求值，不进摘要（改它不动任何版本）。
    有序放量规矩（rollout_rules）：定了规矩时同理——走哪一层、按哪条的比例
    走都由规矩决定，因此是所有人（含不带属性的包）的求值输入：改任何一条的
    条件或比例（含增删、调序、清空），所有已发整包都换新版本；没定规矩
    （''）时不进摘要（摘要保持没这功能时的原样）。
    定档（variants）：定了档时放量层只看档（对所有人定论为开并带档名），
    单条比例/放量条件/放量规矩与默认值都不参与求值——因此定档开关的默认值、
    放量比例在摘要里恒为中性值，档本身（名字+比例，顺序敏感）是所有人
    （含不带属性的包）的求值输入：改任何一档的名字或比例（含增删、调序、
    清空），所有已发整包都换新版本；没定档（''）时不进摘要。
    冻住的开关对此人只认 freezes 行：它的默认值/放量/档/属性条件/依赖边/
    单人强制在冻住期间都不参与求值，因此这些改动不进此人的摘要（不换版本），
    它挂的配置与档名也只认冻住那一刻的快照 frozen_config / frozen_variant；
    它的全关仍压过冻结，所以全关字段保留，冻住期间开全关仍让包换版本。
    对照组里的人同理且更彻底：每个开关都只走自己的默认值（全关仍压过），
    冻结 / 依赖 / 强制 / 属性 / 组 / 落定 / 放量 / 档一概不参与求值，因此
    这些输入对他都中性化；改名单（进 / 出对照组）以 control_group 标记位
    进摘要，他的包必换新版本，没进对照组的人摘要一字不变。
    与求值无关的字段（如描述、时间戳）不影响摘要；只与别人相关的改动
    （如给他人的单人强制）也不影响此身份的摘要。
    """
    is_control = in_control_group(db, identity)
    flags = db.execute(
        "SELECT name, default_enabled, rollout_percent, rollout_condition,"
        " rollout_rules, variants, kill_switch, targeting_rule, flag_config"
        " FROM flags ORDER BY name"
    ).fetchall()
    overrides = db.execute(
        "SELECT f.name, o.enabled FROM overrides o"
        " JOIN flags f ON f.id = o.flag_id WHERE o.identity=? ORDER BY f.name",
        (identity,),
    ).fetchall()
    freezes = db.execute(
        "SELECT f.name, z.frozen_enabled, z.frozen_config, z.frozen_variant"
        " FROM freezes z"
        " JOIN flags f ON f.id = z.flag_id WHERE z.identity=? ORDER BY f.name",
        (identity,),
    ).fetchall()
    frozen_names = {r["name"] for r in freezes}
    # 对照组里的人：每个开关都被「默认值层」接管，放量 / 档 / 依赖 / 强制 /
    # 组 / 落定一律中性化，与冻住的开关走同一套中性化口径。
    masked_names = set(frozen_names)
    if is_control:
        masked_names.update(r["name"] for r in flags)
    members = db.execute(
        "SELECT g.name AS g, f.name AS f FROM group_members m"
        " JOIN mutex_groups g ON g.id = m.group_id"
        " JOIN flags f ON f.id = m.flag_id ORDER BY g.name, f.name"
    ).fetchall()
    assignments = db.execute(
        "SELECT g.name AS g, f.name AS f FROM group_assignments a"
        " JOIN mutex_groups g ON g.id = a.group_id"
        " JOIN flags f ON f.id = a.flag_id WHERE a.identity=? ORDER BY g.name",
        (identity,),
    ).fetchall()
    payload = {
        "environment": getattr(g, "environment", "") if g else "",
        "identity": identity,
        # 冻住的开关：默认值/放量在冻住期间不参与求值，摘要里恒为中性值，
        # 全关仍压过冻结、照常参与，故保留真实值。
        # 定档开关：默认值/放量比例被档层取代（恒为中性值），档本身在下面
        # 以标记元素追加。
        # 对照组里的人：放量 / 档恒中性（只走默认值），默认值与全关保留真值。
        "flags": [[r["name"],
                   False if (r["name"] in masked_names or r["variants"])
                   else bool(r["default_enabled"]),
                   0 if (r["name"] in masked_names or r["variants"])
                   else r["rollout_percent"],
                   bool(r["kill_switch"])] for r in flags],
        # 冻住的开关不看单人强制，给它的强制不进摘要；对照组里所有强制都不看
        "overrides": [[r["name"], bool(r["enabled"])] for r in overrides
                      if r["name"] not in masked_names],
        # 冻住的开关：第三个元素是冻住那一刻挂着的配置快照（冻在关时为 ''），
        # 第四个元素是冻住那一刻的档名快照（没有档/冻在关时为 ''）
        "freezes": [[r["name"], bool(r["frozen_enabled"]), r["frozen_config"],
                     r["frozen_variant"]]
                    for r in freezes if not is_control],
        # 对照组里的人不看互斥组与落定（每个开关直接走默认值），这两组输入
        # 对他恒为空
        "groups": ([[r["g"], r["f"]] for r in members] if not is_control else []),
        "assignments": ([[r["g"], r["f"]] for r in assignments]
                        if not is_control else []),
        # 只在真在对照组里时出现：改名单（进 / 出对照组）让此人包换新版本，
        # 没进对照组的人的摘要里没有这个键，与上线前一字不差
    }
    if is_control:
        payload["control_group"] = True
    # 开关依赖关系是全局求值输入：改了谁依赖谁（含解除、被依赖开关删除）
    # 相关已发整包都要换新版本。冻住的开关不看自己的出边依赖（冻住在依赖层
    # 之前直接定论），所以它自己那条边不进摘要；别人对它的依赖保留——依赖者
    # 求值时会沿备忘拿到它冻住的结果。按依赖者名字排序，保证摘要确定。
    # 对照组里的人不看任何依赖，所有边都不进摘要。
    dependencies = db.execute(
        "SELECT f.name AS child, p.name AS parent FROM flags f"
        " JOIN flags p ON p.id = f.depends_on_flag_id ORDER BY f.name"
    ).fetchall()
    payload["dependencies"] = [[r["child"], r["parent"]] for r in dependencies
                               if r["child"] not in masked_names]
    # 开关挂的配置只在该开关对此人此刻判开时下发，才是此人整包的求值输入：
    # 判开且挂了配置 -> 配置内容进摘要（改配置必换版本）；判关或没挂 -> 不进
    # （改配置不波及他，全关期间所有开关判关，与「全关后不要带」一致）。
    # 冻住的开关配置走 freezes 里的快照，这里不重复计。
    # 对照组里的人在判开（默认开且没被全关压）时同样带这份配置。
    configs = []
    if results is not None:
        for r in flags:
            if r["name"] in frozen_names:
                continue
            if r["flag_config"] and results.get(r["name"], {}).get("enabled"):
                configs.append([r["name"], json.loads(r["flag_config"])])
    payload["configs"] = configs
    if attrs:
        # 带属性的包：属性本身进摘要；属性条件只在「对此人对得上」时进摘要
        # （实际参与了求值才算求值输入，对不上的条件改动不波及此人）。
        # 冻住的开关连属性条件层也不看，条件永不进摘要。
        # 对照组里的人不看属性条件，属性仍随包记账（同人不同属性是不同的包）。
        payload["attrs"] = attrs
        with_rules = []
        for row, r in zip(payload["flags"], flags):
            if (r["name"] not in masked_names and r["targeting_rule"]
                    and targeting_matches(r["targeting_rule"], attrs)):
                with_rules.append(row + [json.loads(r["targeting_rule"])])
            else:
                with_rules.append(row)
        payload["flags"] = with_rules
    # 放量条件：比例 > 0 且定了条件时进所有人的摘要（含不带属性的包——没对上
    # 的人被它挡去默认值层，走哪一层由它决定，所以它也是这些人的求值输入）。
    # 以标记元素追加在属性条件位之后，与属性打开条件区分开；没定条件、比例为
    # 0、开关被冻住或定了档时不进摘要（定了档时这些字段不参与求值）。
    # 对照组里的人不看放量，一律不进。
    for i, r in enumerate(flags):
        if (r["name"] not in masked_names and not r["variants"]
                and r["rollout_percent"] > 0 and r["rollout_condition"]):
            payload["flags"][i] = payload["flags"][i] + [
                ["rollout_condition", json.loads(r["rollout_condition"])]]
    # 有序放量规矩：定了规矩时进所有人的摘要（含不带属性的包——走哪一层、按
    # 哪条的比例走都由规矩决定）。以标记元素追加在放量条件位之后；没定规矩、
    # 开关被冻住或定了档时不进摘要（定了档时规矩不参与求值）。
    # 对照组里的人不看规矩，一律不进。
    for i, r in enumerate(flags):
        if (r["name"] not in masked_names and not r["variants"]
                and r["rollout_rules"]):
            payload["flags"][i] = payload["flags"][i] + [
                ["rollout_rules", json.loads(r["rollout_rules"])]]
    # 定档：定了档时档是所有人（含不带属性的包）的求值输入——改任何一档的
    # 名字或比例（含增删、调序、清空）都让整包换新版本。以标记元素追加在
    # 放量规矩位之后；没定档或开关被冻住时不进摘要（冻住的人只认 freezes 里
    # 的档名快照）。对照组里的人不落档，一律不进。
    for i, r in enumerate(flags):
        if r["name"] not in masked_names and r["variants"]:
            payload["flags"][i] = payload["flags"][i] + [
                ["variants", json.loads(r["variants"])]]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def make_version(content_hash, generation):
    """整包版本 = 求值输入摘要 + 单调递增的内容序号。

    序号只增不减：配置改回去，摘要会复原，但序号不回头，
    因此改过一次之后旧版本就永远不会再有效。
    """
    return hashlib.sha256(
        f"{content_hash}:{generation}".encode("utf-8")
    ).hexdigest()[:16]


def compute_bundle(db, identity, attrs=None):
    """求出此身份此身属性下所有开关的结果与求值输入摘要。先求值（可能写入
    组落定记录），再取摘要，保证摘要覆盖本次求值产生的落定记录与本次判开
    的开关配置——管理端失效扫描与调用方来拿走同一套求值，记下的版本与本人
    来拿时拿到的才一致。
    整包共用一份依赖备忘：被依赖的开关先算一次，依赖它的开关与单查它时
    拿到的是同一个结果、同一个理由。"""
    flags = db.execute("SELECT * FROM flags ORDER BY name").fetchall()
    results = {}
    memo = {}
    for flag in flags:
        enabled, reason, config, variant = evaluate(db, flag, identity, attrs, memo)
        item = {"enabled": enabled, "reason": reason}
        if config is not None:
            item["config"] = config
        if variant is not None:
            item["variant"] = variant
        results[flag["name"]] = item
    return results, bundle_content_hash(db, identity, attrs, results)


def record_invalidations(db, actor_name, change):
    """配置变更后调用：用与调用方来拿时完全相同的求值重算每个已发整包
    （开关依赖也照新关系算：被依赖开关关着时，依赖它的开关全按 depends_on 关重算）。

    整包按 (身份, 属性) 记账，逐包重算：求值输入变了的包，内容序号 +1、
    记一条失效记录（谁改的、哪次变更、让谁的哪身属性的整包从哪个版本变成
    哪个版本），并把 bundles 行推进到新摘要与新序号——记下的新版本与本人
    带同一身属性再来拿时拿到的是同一个。序号只增不减，配置改回去旧版本
    也不会复活。与某包无关的改动（如改属性条件时的无属性包、改描述）不会
    改变它的摘要，自然不会产生记录。
    """
    rows = db.execute(
        "SELECT identity, attrs_hash, attrs_json, content_hash, generation"
        " FROM bundles"
    ).fetchall()
    if not rows:
        return
    now = time.time()
    for r in rows:
        attrs = json.loads(r["attrs_json"]) if r["attrs_json"] else None
        _, content_hash = compute_bundle(db, r["identity"], attrs)
        if content_hash == r["content_hash"]:
            continue
        new_generation = r["generation"] + 1
        db.execute(
            "INSERT INTO bundle_invalidations"
            " (actor, change, identity, attrs_hash, old_version, new_version, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (actor_name, change, r["identity"], r["attrs_hash"],
             make_version(r["content_hash"], r["generation"]),
             make_version(content_hash, new_generation), now),
        )
        db.execute(
            "UPDATE bundles SET content_hash=?, generation=?"
            " WHERE identity=? AND attrs_hash=?",
            (content_hash, new_generation, r["identity"], r["attrs_hash"]),
        )
    db.commit()


# ---------------------------------------------------------------- 历史时刻重放

# 同一时刻的确定性先后：先管理端立即生效的变更，再预约到点的变更，最后发布稿。
# （真实系统里预约是惰性应用、理论上可能晚于同刻的立即改动；重放取这个固定序，
# 让「那一刻会拿到什么」是一个确定的答案。）
_HIST_RANK = {"imm": 0, "sched": 1, "draft": 2}
_FLAG_DEFAULT = {"default_enabled": False, "rollout_percent": 0,
                 "rollout_condition": "", "rollout_rules": "",
                 "variants": "",
                 "kill_switch": False, "targeting": "",
                 "config": "", "depends_on": ""}


def _normalize_replay_patch(changes):
    """把预约 / 发布稿里存的字段值归一成重放用的覆盖 patch。

    发布稿存的是 canonical 串（与 flags 列同口径，''=清除）；预约存的是
    原始 JSON 值（dict / None，与到点时走 apply_flag_fields 的口径一致），
    这里两种都归一成库里的串形态。depends_on 是名字串（''=解除）。
    """
    patch = {}
    if "kill_switch" in changes:
        patch["kill_switch"] = bool(changes["kill_switch"])
    if "default_enabled" in changes:
        patch["default_enabled"] = bool(changes["default_enabled"])
    if "rollout_percent" in changes:
        patch["rollout_percent"] = int(changes["rollout_percent"])
    if "rollout_condition" in changes:
        v = changes["rollout_condition"]
        patch["rollout_condition"] = (v if isinstance(v, str)
                                      else validate_rollout_condition(v))
    if "rollout_rules" in changes:
        v = changes["rollout_rules"]
        patch["rollout_rules"] = (v if isinstance(v, str)
                                  else validate_rollout_rules(v))
    if "variants" in changes:
        v = changes["variants"]
        patch["variants"] = (v if isinstance(v, str)
                             else validate_variants(v))
    if "targeting" in changes:
        v = changes["targeting"]
        patch["targeting"] = v if isinstance(v, str) else validate_targeting(v)
    if "config" in changes:
        v = changes["config"]
        patch["config"] = v if isinstance(v, str) else validate_config(v)
    if "depends_on" in changes:
        patch["depends_on"] = changes["depends_on"] or ""
    return patch


def _replay_ops(db, at):
    """收集时刻 at 之前「已经生效」的全部变更，按固定先后排好序（纯读，不写库）。

    数据源（与 /api/history 同一口径）：
    - history_events：管理端每一次「已生效」变更的 append-only 流水；
    - scheduled_changes：status='applied' 或 pending 但 effective_at<=at
      （到点了哪怕还没被任何请求惰性触发也算；cancelled/failed 一律不算）；
    - drafts：status='published' 且 published_at<=at 的稿（没发布的稿不算）。
    返回 [(occurred_at, rank, 次序, 类型, 内容), …]，类型为
    "event"/"sched"/"draft"。同一时刻的确定性先后：立即改动 < 预约 < 发布稿。
    """
    ops = []
    for r in db.execute(
            "SELECT * FROM history_events WHERE occurred_at<=?"
            " ORDER BY occurred_at, id", (at,)):
        ops.append((r["occurred_at"], 0, r["id"], "event",
                    (r["kind"], r["subject"], r["identity"],
                     json.loads(r["payload"]) if r["payload"] else {})))

    # 预约：已应用，或到点未取消（删除开关会把 pending 置为 cancelled）
    for r in db.execute(
            "SELECT * FROM scheduled_changes"
            " WHERE ((status='applied') OR (status='pending' AND effective_at<=?))"
            " AND effective_at<=? ORDER BY effective_at, id",
            (at, at)):
        ops.append((r["effective_at"], 1, r["id"], "sched",
                    (r["flag_name"], json.loads(r["changes"]))))

    # 发布稿：稿里每个开关的合并改动在 published_at 一起应用（按开关名定序）
    for d in db.execute(
            "SELECT id, published_at FROM drafts"
            " WHERE status='published' AND published_at<=?"
            " ORDER BY published_at, id", (at,)):
        rows = db.execute(
            "SELECT flag_name, changes FROM draft_changes WHERE draft_id=?"
            " ORDER BY flag_name", (d["id"],)).fetchall()
        for sub, cr in enumerate(rows):
            ops.append((d["published_at"], 2, sub, "draft",
                        (cr["flag_name"], json.loads(cr["changes"]))))

    ops.sort(key=lambda o: (o[0], o[1], o[2]))
    return ops


def replay_flag_state(db, at):
    """重放环境在时刻 at 的**整份开关规矩**（与身份无关，纯读，不写库）。

    只还原开关自身的求值规则，返回 name ->
    {default_enabled, rollout_percent, rollout_condition(串),
     rollout_rules(串), variants(串), kill_switch, targeting(串),
     config(串), depends_on(名字串)}。

    口径与 /api/history 一字不差：
    - 当时还没建、或已删除的开关不在结果里——约了还没到点的改动、没发布的稿
      都不算（到点未触发的预约、已发布的稿照算）；
    - 被依赖开关在那一刻已删除时，依赖边按解除处理（与重放求值同口径）。
    单人强制 / 冻结 / 互斥组 / 对照名单等「按人 / 按组」的状态不在这里。
    """
    flags = {}
    for _, _, _, kind, body in _replay_ops(db, at):
        if kind in ("sched", "draft"):
            name, changes = body
            if name not in flags:
                # 开关当时还没建（或已删）：这条预约/稿对那一刻不生效
                continue
            patch = _normalize_replay_patch(changes)
            dep = patch.get("depends_on")
            if dep and dep not in flags:
                # 到点应用时被依赖开关已删除：线上写不出这条边（按解除处理）；
                # 发布稿在目标缺失时整稿拒绝，但这里只兜已发布稿的极端情形
                patch["depends_on"] = ""
            flags[name].update(patch)
            continue

        ev_kind, subject, _ev_identity, payload = body
        if ev_kind == "flag_upsert":
            snap = dict(_FLAG_DEFAULT)
            if subject in flags:
                snap.update(flags[subject])
            snap.update({k: payload[k] for k in _FLAG_DEFAULT if k in payload})
            flags[subject] = snap
        elif ev_kind == "flag_delete":
            flags.pop(subject, None)
            # 与线上删除路径一致：别人指向它的依赖边自动解除
            for other in flags.values():
                if other["depends_on"] == subject:
                    other["depends_on"] = ""
    return flags


def replay_state(db, identity, at, attrs=None):
    """重放身份 identity 在时刻 at 的完整求值状态（纯函数，不写库）。

    数据源：
    - history_events：管理端每一次「已生效」变更的 append-only 流水；
    - scheduled_changes：status='applied' 或 pending 但 effective_at<=at，
      且开关已删除时被取消的（cancelled/failed 一律不算）——预约到了点，
      哪怕还没有任何请求惰性触发它，那一刻来问也必须按到点后算；
    - drafts：status='published' 且 published_at<=at 的稿（没发布的稿不算）。

    返回 dict：
      flags:  name -> {default_enabled, rollout_percent, rollout_condition(串),
                       rollout_rules(串), variants(串), kill_switch, targeting(串),
                       config(串), depends_on(名字串)}
      overrides/frozen: (flag_name, identity) -> ...
      groups: name -> set(成员 flag)；assignments: 组名 -> 落定 flag
    """
    ops = _replay_ops(db, at)

    # 身份合并要按「那一刻」算：先只扫 identity_merge / identity_split，还原
    # at 时刻还没拆开的合并组。那一刻这几个身份算同一个人，所以他们各自名下的
    # 单人强制 / 冻结 / 组落定都要算进来；合并之前（或拆开之后）他们各算各的，
    # 别人的记录不归他。重放不做线上那种行迁移，这里用主身份（合并时名字序最小）
    # 作桶与落定身份，与「合并那一刻起同一个人」一致。
    person_groups = {}   # identity -> {"primary": p, "members": set()}
    for r in db.execute(
            "SELECT kind, payload FROM history_events"
            " WHERE kind IN ('identity_merge','identity_split')"
            " AND occurred_at<=? ORDER BY occurred_at, id", (at,)):
        payload = json.loads(r["payload"]) if r["payload"] else {}
        idents = payload.get("identities", [])
        if r["kind"] == "identity_merge":
            grp = {"primary": payload.get("primary") or min(idents),
                   "members": set(idents)}
            for ident in idents:
                person_groups[ident] = grp
        else:  # identity_split：这几个身份从这一刻起各算各的
            for ident in idents:
                person_groups.pop(ident, None)
    grp = person_groups.get(identity)
    if grp is None:
        person = identity
        person_members = {identity}
    else:
        person = grp["primary"]
        person_members = grp["members"]

    # 对照组同样按「那一刻」还原：只扫 at 之前的 control_add / control_remove
    # 流水（名单是整拨替换语义，重放时按增删事件回放即可）。事件里的身份是
    # 当时的主身份，与单人强制 / 冻结同口径——那一刻这个人收在合并拨里时，
    # 拨内任一身份来问都要按同一个人的对照归属算。
    control = False
    for r in db.execute(
            "SELECT kind, identity FROM history_events"
            " WHERE kind IN ('control_add','control_remove')"
            " AND occurred_at<=? ORDER BY occurred_at, id", (at,)):
        if r["identity"] in person_members:
            control = r["kind"] == "control_add"

    flags = {}
    overrides = {}
    frozen = {}
    groups = {}
    assignments = {}

    for _, _, _, kind, body in ops:
        if kind in ("sched", "draft"):
            name, changes = body
            if name not in flags:
                # 开关当时还没建（或已删）：这条预约/稿对那一刻不生效
                continue
            patch = _normalize_replay_patch(changes)
            dep = patch.get("depends_on")
            if dep and dep not in flags:
                # 到点应用时被依赖开关已删除：线上写不出这条边（按解除处理）；
                # 发布稿在目标缺失时整稿拒绝，但这里只兜已发布稿的极端情形
                patch["depends_on"] = ""
            flags[name].update(patch)
            continue

        ev_kind, subject, ev_identity, payload = body

        if ev_kind == "flag_upsert":
            snap = dict(_FLAG_DEFAULT)
            if subject in flags:
                snap.update(flags[subject])
            snap.update({k: payload[k] for k in _FLAG_DEFAULT if k in payload})
            flags[subject] = snap
        elif ev_kind == "flag_delete":
            flags.pop(subject, None)
            overrides.pop((subject, person), None)
            frozen.pop((subject, person), None)
            # 与线上删除路径一致：别人指向它的依赖边自动解除
            for other in flags.values():
                if other["depends_on"] == subject:
                    other["depends_on"] = ""
            for gname, members in list(groups.items()):
                members.discard(subject)
                if assignments.get(gname) == subject:
                    assignments.pop(gname, None)
        elif ev_kind == "override" and ev_identity in person_members:
            overrides[(subject, person)] = bool(payload["enabled"])
        elif ev_kind == "override_delete" and ev_identity in person_members:
            overrides.pop((subject, person), None)
        elif ev_kind == "freeze" and ev_identity in person_members:
            frozen[(subject, person)] = {
                "enabled": bool(payload["enabled"]),
                "config": payload.get("config", ""),
                "variant": payload.get("variant", ""),
            }
        elif ev_kind == "freeze_delete" and ev_identity in person_members:
            frozen.pop((subject, person), None)
        elif ev_kind in ("identity_merge", "identity_split"):
            continue  # 组成员关系已在循环前按 at 时刻还原
        elif ev_kind == "group_upsert":
            groups.setdefault(subject, set())
        elif ev_kind == "group_delete":
            groups.pop(subject, None)
            assignments.pop(subject, None)
        elif ev_kind == "member_add":
            groups.setdefault(subject, set()).add(payload["flag"])
        elif ev_kind == "member_remove":
            groups.get(subject, set()).discard(payload["flag"])
            if assignments.get(subject) == payload["flag"]:
                assignments.pop(subject, None)
        elif ev_kind == "assignment" and ev_identity in person_members:
            members = groups.get(subject)
            # 落定只在「组还在、该开关当时仍在组内」时有效
            if members and payload["flag"] in members:
                assignments.setdefault(subject, payload["flag"])

    state = {"flags": flags, "overrides": overrides, "frozen": frozen,
             "groups": groups, "assignments": assignments}
    state["person"] = person
    state["person_members"] = person_members
    state["control"] = control
    return state


def evaluate_at(state, name, identity, attrs=None, _memo=None, _chain=None):
    """按固定优先级在重放状态上求一个开关的值，返回 (enabled, reason, config, variant)。

    与线上 evaluate 同一套口径（全关 > 冻结 > 依赖 > 强制 > 定档 > 属性 >
    组 > 放量 > 默认），区别只是输入来自 replay_state 的内存状态、且组落定不
    写库：那一刻已经落定过（assginments 里有）就按落定算；没落定过就按名字
    序模拟——与「那一刻整包来问」时 compute_bundle 按 name 序求值、首个自然
    开的组内开关落定的行为一字不差。
    """
    _COMPUTE = object()  # 与线上 evaluate 同口径的档名出口哨兵

    def ret(enabled, reason, config_json="", variant=_COMPUTE):
        cfg = parse_flag_config(config_json) if enabled and config_json else None
        if not enabled:
            variant = None
        elif variant is _COMPUTE:
            variant = variant_of(flag["variants"], name, identity)
        return enabled, reason, cfg, variant

    flag = state["flags"].get(name)
    if flag is None:
        return False, "default", None, None  # 理论不可达：整包只遍历当时存在的开关

    if flag["kill_switch"]:
        return ret(False, "kill_switch")

    # 对照组：那一刻这个人在对照组里时，不看冻结 / 依赖 / 强制 / 属性 / 组 /
    # 放量 / 定档，每个开关只走自己的默认值（reason=control，不带档名），
    # 与线上 evaluate 同一口径；全关已在更前面压过。
    if state.get("control"):
        return ret(bool(flag["default_enabled"]), "control", flag["config"], None)

    fz = state["frozen"].get((name, identity))
    if fz is not None:
        if _memo is not None:
            _memo[name] = fz["enabled"]
        # 与线上 evaluate 同一口径：档在那一刻已清空则不带档名
        fz_variant = fz.get("variant") or None if flag.get("variants") else None
        return ret(fz["enabled"], "freeze", fz["config"], fz_variant)

    dep = flag["depends_on"]
    if dep and dep in state["flags"]:
        if _chain is not None and dep in _chain:
            raise RuntimeError(f"dependency cycle through {dep}")
        if _memo is not None and dep in _memo:
            dep_enabled = _memo[dep]
        else:
            next_chain = {name} if _chain is None else _chain | {name}
            dep_enabled, _, _, _ = evaluate_at(state, dep, identity, attrs,
                                               _memo, next_chain)
        if not dep_enabled:
            if _memo is not None:
                _memo[name] = False
            return ret(False, "depends_on")
    # dep 指向当时不存在的开关：删除路径已把边清掉，这里兜底按无依赖继续

    if (name, identity) in state["overrides"]:
        enabled = state["overrides"][(name, identity)]
        if _memo is not None:
            _memo[name] = enabled
        return ret(enabled, "override", flag["config"])

    if targeting_matches(flag["targeting"], attrs):
        natural, reason = True, "targeting"
    elif flag.get("variants"):
        natural, reason = True, "variants"
    else:
        verdict = rollout_layer_verdict(
            name, identity, flag["rollout_percent"], flag["rollout_condition"],
            flag["rollout_rules"], attrs)
        if verdict is not None:
            natural, reason = verdict
        else:
            natural, reason = bool(flag["default_enabled"]), "default"

    group_name = next((g for g, members in state["groups"].items()
                       if name in members), None)
    if group_name is None or not natural:
        if _memo is not None:
            _memo[name] = natural
        return ret(natural, reason, flag["config"])

    # 组裁决复刻线上「先到先得、只落定一次」：这一刻已落定过（真实查询写过
    # 落定记录）就按落定算；没落定过则模拟 INSERT OR IGNORE——本次名字序求值
    # 链上首个自然开的组内开关落定。与 evaluate 的写库版唯一区别是不写库。
    winner = state["assignments"].get(group_name)
    if winner is None:
        state["assignments"][group_name] = name
        winner = name
    won = winner == name
    if _memo is not None:
        _memo[name] = won
    return ret(won, "group", flag["config"])


def compute_history_bundle(db, identity, at, attrs=None):
    """求该身份在时刻 at 所有「当时存在」的开关结果（按名字序，与线上整包同口径）。

    合并 / 拆开按 at 那一刻的归属算：那一刻收在一起的身份算同一个人，分桶与
    落定都用那拨人的主身份。返回 (results, person)。"""
    state = replay_state(db, identity, at, attrs)
    person = state.get("person", identity)
    results = {}
    memo = {}
    for name in sorted(state["flags"]):
        enabled, reason, config, variant = evaluate_at(
            state, name, person, attrs, memo)
        item = {"enabled": enabled, "reason": reason}
        if config is not None:
            item["config"] = config
        if variant is not None:
            item["variant"] = variant
        results[name] = item
    return results, person


@app.get("/api/history")
def history_bundle():
    """调用方接口：问某个人在过去某个时刻，每个开关当时开还是关。

    GET /api/history?identity=<身份>&at=<unix 秒>[&attrs=<URL编码 JSON>]
    开着的开关带当时那份配置（冻住的带冻住那一刻那份）；当时全关 / 判关的
    不带 config。约了时间还没到点的改动、没发布的稿都不算；结果与那一刻
    真来问（整包口径）拿到的一字不差。at 必须是不晚于现在的 unix 秒。
    """
    identity = request.args.get("identity")
    if not identity:
        return jsonify({"error": "identity is required"}), 400
    raw_at = request.args.get("at")
    if raw_at is None or raw_at == "":
        return jsonify({"error": "at is required (unix timestamp in seconds)"}), 400
    try:
        at = float(raw_at)
    except (TypeError, ValueError):
        return jsonify({"error": "at must be a unix timestamp in seconds"}), 400
    if not math.isfinite(at) or at > time.time():
        return jsonify({"error": "at must be a past unix timestamp (not in the future)"}), 400
    try:
        attrs = parse_attrs(request.args.get("attrs"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    results, _person = compute_history_bundle(get_db(), identity, at, attrs)
    body = {"identity": identity, "at": at, "flags": results}
    if attrs is not None:
        body["attrs"] = attrs
    return jsonify(body)


# ---------------------------------------------------------------- admin auth

def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-Admin-Token", "") != ADMIN_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


def actor():
    return request.headers.get("X-Actor", "unknown").strip() or "unknown"


@app.get("/api/environments")
@app.get("/api/environment")
@app.get("/api/envs")
@require_admin
def list_environments():
    db = get_catalog_db()
    rows = db.execute(
        "SELECT name, created_by, created_at FROM environments ORDER BY name"
    ).fetchall()
    return jsonify({"environments": [
        {"name": r["name"], "created_by": r["created_by"],
         "created_at": r["created_at"]} for r in rows]})


@app.post("/api/environments")
@app.post("/api/environment")
@app.post("/api/envs")
@require_admin
def create_environment():
    body = request.get_json(force=True)
    raw = body.get("name") if isinstance(body, dict) else None
    try:
        name = validate_environment_name(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if get_environment_record(name) is not None:
        return jsonify({"error": f"environment already exists: {name}"}), 409
    init_environment_db(name, actor())
    return jsonify({"ok": True, "environment": name}), 201


# ---------------------------------------------------------------- 跨环境推送

def push_env_field(body, canonical, alias):
    """读取推送的方向字段（source/from、target/to）：缺了报错；两种写法都给
    且不一致时报错。返回 (环境名, 错误串)。"""
    values = []
    for k in (canonical, alias):
        v = body.get(k)
        if v is None or v == "":
            continue
        if not isinstance(v, str):
            return None, f"{canonical} must be a string (environment name)"
        values.append(v)
    if not values:
        what = ("the environment to push from" if canonical == "source"
                else "the environment to push to")
        return None, f"{canonical} is required ({what})"
    if len(set(values)) > 1:
        return None, f"{canonical} and {alias} must match when both are given"
    try:
        return validate_environment_name(values[0]), None
    except ValueError as e:
        return None, str(e)


@app.post("/api/push")
@require_admin
def push_flags():
    """管理端：把源环境此刻的开关规则一次性推到目标环境。

    POST /api/push  {"source": "staging", "target": "prod", "flags": ["a", "b"]}
    （source/target 也可写成 from/to）

    - 少写了（source/target/flags 缺一）、环境名非法、flags 不是非空的开关
      名单：400，这次推不成；
    - 源或目标环境不存在：404 并指明是哪一边，这次推不成；
    - 点名的开关在源里不存在：404 列出缺的，一个都不推；
    - 推成后，目标环境里这些开关按源此刻的规则算（默认值 / 全关 / 放量比例 /
      放量条件 / 放量规矩 / 属性打开条件 / 挂的配置 / 依赖，连同描述）；目标里
      没有的开关就地新建；没点名的开关一律还是目标自己的。目标本地的单人强制、
      结果冻结、互斥组与落定、定时变更、发布稿与整包账本都是目标自己的状态，
      推送不抄也不清（与「在目标上直接改这些开关」同一口径）；
    - 依赖按名字落到目标：被依赖的开关必须在推送后的目标里存在（目标已有或
      这次一起推），否则这次一个都不推，返回 400 并说明；
    - 若这么推会让目标里的开关互相绕着依赖（成环 / 自依赖），这一次一个开关
      都不改，目标保持推之前的规则，返回 400 并说明；
    - 源环境全程只读（连接上开了 query_only）：这次推送不改源的任何规则、
      审计、历史与整包账本。
    推送是目标环境上的一次原子变更：全部校验通过后一个事务写入，记审计与
    历史流水，目标已发整包统一重算、换新版本。
    """
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "body must be a JSON object"
                                 " ({source, target, flags})"}), 400
    source, err = push_env_field(body, "source", "from")
    if err:
        return jsonify({"error": err}), 400
    target, err = push_env_field(body, "target", "to")
    if err:
        return jsonify({"error": err}), 400
    raw_flags = body.get("flags")
    if raw_flags is None:
        return jsonify({"error": "flags is required"
                                 " (a non-empty list of flag names to push)"}), 400
    if not isinstance(raw_flags, list) or not raw_flags:
        return jsonify({"error": "flags must be a non-empty list of flag names"}), 400
    names = []
    for f in raw_flags:
        if not isinstance(f, str) or f == "":
            return jsonify({"error": "flags must be non-empty strings"
                                     " (flag names)"}), 400
        if f not in names:
            names.append(f)
    if source == target:
        return jsonify({"error": "source and target must be different"
                                 " environments"}), 400
    src_rec = get_environment_record(source)
    if src_rec is None:
        return jsonify({"error": f"source environment not found: {source}"}), 404
    tgt_rec = get_environment_record(target)
    if tgt_rec is None:
        return jsonify({"error": f"target environment not found: {target}"}), 404

    # 源环境全程只读（query_only：任何误写都直接报错而不是落库）。读出点名
    # 开关此刻的规则后即关闭——这次推送不改源的一分一毫。
    src = connect_sqlite(src_rec["db_file"])
    try:
        src.execute("PRAGMA query_only=ON")
        src_name_of_id = {r["id"]: r["name"]
                          for r in src.execute("SELECT id, name FROM flags")}
        src_flags = {}
        for n in names:
            row = src.execute("SELECT * FROM flags WHERE name=?", (n,)).fetchone()
            if row is not None:
                src_flags[n] = row
    finally:
        src.close()
    missing = [n for n in names if n not in src_flags]
    if missing:
        return jsonify({"error": "flags not found in source environment"
                                 f" '{source}': {', '.join(missing)}"}), 404

    # 每个点名开关要落到目标的规则；依赖按名字带过去，稍后解析成目标的 id
    staged = {}
    for n in names:
        s = src_flags[n]
        dep = ""
        if s["depends_on_flag_id"] is not None:
            dep = src_name_of_id.get(s["depends_on_flag_id"], "")
        staged[n] = {"depends_on": dep}

    # 以下是目标环境上的一次变更：与任何访问目标环境的请求一样，先惰性应用
    # 到点的定时变更，再在「此刻」的目标上做校验与写入
    g.environment = target
    apply_due_scheduled_changes()
    db = get_db()

    # 被依赖的开关必须在推送后的目标里存在（目标已有，或这次一起推）
    tgt_names = {r["name"] for r in db.execute("SELECT name FROM flags")}
    for n in sorted(staged):
        dep = staged[n]["depends_on"]
        if dep and dep not in tgt_names and dep not in staged:
            msg = (f"flag '{n}' depends on '{dep}' in source environment"
                   f" '{source}', but '{dep}' does not exist in target"
                   f" environment '{target}'"
                   " (push it too or create it there first)")
            audit_to(db, actor(), f"push:{source}->{target}", "push",
                     "push_rejected", msg)
            db.commit()
            return jsonify({"error": msg}), 400
    # 合并后的目标依赖图不能成环：成环则这一次一个开关都不改
    cycle_err = merged_dependency_cycle(
        db, staged, what=f"pushing into environment '{target}'")
    if cycle_err is not None:
        audit_to(db, actor(), f"push:{source}->{target}", "push",
                 "push_rejected", cycle_err)
        db.commit()
        return jsonify({"error": cycle_err}), 400

    now = time.time()
    created, updated = [], []
    try:
        # 先写各开关自身的规则（目标没有的就地新建），再统一落依赖边
        for n in sorted(staged):
            s = src_flags[n]
            cur = db.execute(
                "UPDATE flags SET description=?, default_enabled=?,"
                " rollout_percent=?, rollout_condition=?, rollout_rules=?,"
                " variants=?, kill_switch=?, targeting_rule=?, flag_config=?,"
                " updated_at=?"
                " WHERE name=?",
                (s["description"], s["default_enabled"], s["rollout_percent"],
                 s["rollout_condition"], s["rollout_rules"], s["variants"],
                 s["kill_switch"],
                 s["targeting_rule"], s["flag_config"], now, n))
            if cur.rowcount == 0:
                db.execute(
                    "INSERT INTO flags (name, description, default_enabled,"
                    " rollout_percent, rollout_condition, rollout_rules,"
                    " variants, kill_switch, targeting_rule, flag_config,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (n, s["description"], s["default_enabled"],
                     s["rollout_percent"], s["rollout_condition"],
                     s["rollout_rules"], s["variants"], s["kill_switch"],
                     s["targeting_rule"], s["flag_config"], now, now))
                created.append(n)
            else:
                updated.append(n)
        for n in sorted(staged):
            dep = staged[n]["depends_on"]
            dep_id = None
            if dep:
                dep_id = db.execute("SELECT id FROM flags WHERE name=?",
                                    (dep,)).fetchone()["id"]
            db.execute("UPDATE flags SET depends_on_flag_id=? WHERE name=?",
                       (dep_id, n))
        for n in sorted(staged):
            fresh = db.execute("SELECT * FROM flags WHERE name=?", (n,)).fetchone()
            record_history(db, now, "flag_upsert", subject=n,
                           payload=flag_snapshot_payload(fresh),
                           actor_name=actor())
            audit_to(db, actor(), n, "push", "push_flag",
                     f"from {source}" + (" (created)" if n in created else ""))
        audit_to(db, actor(), f"push:{source}->{target}", "push", "push_flags",
                 "flags=" + ",".join(sorted(staged)))
    except Exception:
        db.rollback()
        raise
    db.commit()
    record_invalidations(
        db, actor(), f"push {source} -> {target}: {','.join(sorted(staged))}")
    return jsonify({"ok": True, "source": source, "target": target,
                    "pushed": sorted(staged), "created": created,
                    "updated": updated})


# ---------------------------------------------------------------- 整份规矩还原

@app.post("/api/restore")
@require_admin
def restore_flags():
    """管理端：把本环境此刻「已经生效」的整份开关规矩，换成过去某一刻当时
    已经生效的那份。

    POST /api/restore  {"at": <unix 秒>}（环境仍走 ?environment= / X-Environment）

    - 少写了环境 / 时刻、环境不存在、at 不是数字或在未来：这次换不成（400/404），
      一个字段都不动；
    - 还原的那份与 /api/history 同一口径：只取 at 那一刻存在且已生效的开关规则
      （默认值 / 全关 / 放量比例 / 放量条件 / 放量规矩 / 定档 / 属性打开条件 /
      挂的配置 / 依赖，描述不参与求值，保留现网不动）；当时还没有（或已删除）的
      开关这次整个删掉，不会再按现在的规矩混在来问的结果里；
    - 约了还没到点的改动不算进那份，且会随这次还原全部取消——到点也不会再生效；
      没发布的稿本来就不算（原样保留在稿里，不发布就一字不生效，已发布且
      published_at<=at 的稿已经算在那份里）；
    - 单人强制 / 结果冻结 / 互斥组与落定 / 对照名单 / 身份合并是环境自己的
      「按人 / 按组」状态，不是开关规矩，这次不抄也不清（与跨环境推送同口径）；
    - 还原是该环境上的一次原子变更：一个事务写入，记审计与历史流水，已发整包
      统一重算、换新版本（拿旧包来问得到 valid=false）。此后这个环境里来问，
      一律按换过去的那份规矩算。
    """
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "body must be a JSON object ({\"at\": <unix seconds>})"}), 400
    raw_at = body.get("at")
    if raw_at is None or raw_at == "":
        return jsonify({"error": "at is required (unix timestamp in seconds)"}), 400
    if isinstance(raw_at, bool) or not isinstance(raw_at, (int, float, str)):
        return jsonify({"error": "at must be a unix timestamp in seconds"}), 400
    try:
        at = float(raw_at)
    except (TypeError, ValueError):
        return jsonify({"error": "at must be a unix timestamp in seconds"}), 400
    if not math.isfinite(at):
        return jsonify({"error": "at must be a unix timestamp in seconds"}), 400
    now = time.time()
    if at > now:
        return jsonify({"error": "at must be a past unix timestamp (not in the future)"}), 400

    # 与任何写操作一样：先把此刻已到点的预约应用掉，「现在」与 at 都是确定状态
    db = get_db()
    # 那一刻整份已生效的开关规矩（与 /api/history 同一口径的纯读重放）
    wanted = replay_flag_state(db, at)
    current = db.execute("SELECT * FROM flags").fetchall()
    cur_by_name = {r["name"]: r for r in current}
    name_of_dep_id = {r["id"]: r["name"] for r in current}

    def current_rule_tuple(r):
        dep = name_of_dep_id.get(r["depends_on_flag_id"], "") \
            if r["depends_on_flag_id"] is not None else ""
        return (1 if r["default_enabled"] else 0, r["rollout_percent"],
                r["rollout_condition"], r["rollout_rules"], r["variants"],
                r["kill_switch"], r["targeting_rule"], r["flag_config"], dep)

    def wanted_rule_tuple(snap):
        return (1 if snap["default_enabled"] else 0, snap["rollout_percent"],
                snap["rollout_condition"], snap["rollout_rules"],
                snap["variants"], 1 if snap["kill_switch"] else 0,
                snap["targeting"], snap["config"], snap["depends_on"])

    created, updated, unchanged, deleted = [], [], [], []
    at_str = f"{at:g}"
    try:
        # 还活着的开关：规矩与那一刻不同才整份换（描述不参与求值，保留现网）；
        # 一字不差的不动库、不记历史、不进审计（幂等再还原一次是 no-op）。
        for name in sorted(wanted):
            snap = wanted[name]
            cur = cur_by_name.get(name)
            if cur is not None:
                if current_rule_tuple(cur) == wanted_rule_tuple(snap):
                    unchanged.append(name)
                    continue
                db.execute(
                    "UPDATE flags SET default_enabled=?, rollout_percent=?,"
                    " rollout_condition=?, rollout_rules=?, variants=?,"
                    " kill_switch=?, targeting_rule=?, flag_config=?, updated_at=?"
                    " WHERE name=?",
                    (1 if snap["default_enabled"] else 0, snap["rollout_percent"],
                     snap["rollout_condition"], snap["rollout_rules"],
                     snap["variants"], 1 if snap["kill_switch"] else 0,
                     snap["targeting"], snap["config"], now, name))
                updated.append(name)
            else:
                # 那一刻有、现在没有的开关：按那一刻的规矩重新建出来
                db.execute(
                    "INSERT INTO flags (name, description, default_enabled,"
                    " rollout_percent, rollout_condition, rollout_rules, variants,"
                    " kill_switch, targeting_rule, flag_config, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (name, "", 1 if snap["default_enabled"] else 0,
                     snap["rollout_percent"], snap["rollout_condition"],
                     snap["rollout_rules"], snap["variants"],
                     1 if snap["kill_switch"] else 0,
                     snap["targeting"], snap["config"], now, now))
                created.append(name)

        # 依赖边按名字整体重落（那一刻不存在的被依赖者：快照里已是 ''，落 NULL）。
        # 只重落这次规矩变了或新建的；没变的开关边也必然没变，一字不动。
        for name in sorted(set(created) | set(updated)):
            dep = wanted[name]["depends_on"]
            dep_id = None
            if dep:
                row = db.execute("SELECT id FROM flags WHERE name=?", (dep,)).fetchone()
                dep_id = row["id"] if row else None
            db.execute("UPDATE flags SET depends_on_flag_id=? WHERE name=?",
                       (dep_id, name))

        # 那一刻还没有（或后来被删）的开关整个删掉：先解除别人对它的依赖，
        # 再取消它未生效的预约并删除（强制 / 冻结 / 组成员关系随外键级联清掉）。
        for name in sorted(cur_by_name):
            if name in wanted:
                continue
            fid = cur_by_name[name]["id"]
            db.execute("UPDATE flags SET depends_on_flag_id=NULL"
                       " WHERE depends_on_flag_id=?", (fid,))
            db.execute("UPDATE scheduled_changes SET status='cancelled'"
                       " WHERE flag_id=? AND status='pending'", (fid,))
            db.execute("DELETE FROM flags WHERE id=?", (fid,))
            deleted.append(name)

        # 约了还没到点的改动不算进那份：全部取消，到点也不会再改规矩
        cancelled = db.execute(
            "UPDATE scheduled_changes SET status='cancelled' WHERE status='pending'"
        ).rowcount

        # 历史流水（append-only）：只把这次**真变了**的开关记成「此刻」的一批
        # 变更——更新的记全量快照、删除的记 flag_delete；没变的不补事件
        # （幂等再还原一次不产生流水）。
        for name in sorted(set(created) | set(updated)):
            fresh = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
            record_history(db, now, "flag_upsert", subject=name,
                           payload=flag_snapshot_payload(fresh), actor_name=actor())
        for name in deleted:
            record_history(db, now, "flag_delete", subject=name,
                           actor_name=actor())

        for name in created:
            audit_to(db, actor(), name, "restore", "restore_flag",
                     f"restored as of {at_str} (recreated)")
        for name in updated:
            audit_to(db, actor(), name, "restore", "restore_flag",
                     f"restored as of {at_str}")
        for name in deleted:
            audit_to(db, actor(), name, "restore", "restore_delete",
                     f"absent as of {at_str}")
        audit_to(db, actor(), f"restore@{at_str}", "restore", "restore_flags",
                 f"created={len(created)} updated={len(updated)}"
                 f" unchanged={len(unchanged)} deleted={len(deleted)}"
                 f" cancelled_scheduled={cancelled}")
    except Exception:
        db.rollback()
        raise
    db.commit()
    record_invalidations(db, actor(),
                         f"restore_flags as of {at_str}: "
                         f"created={len(created)},updated={len(updated)},"
                         f"deleted={len(deleted)}")
    return jsonify({"ok": True, "restored": True, "at": at,
                    "flags": sorted(wanted),
                    "created": created, "updated": updated,
                    "unchanged": unchanged, "deleted": deleted,
                    "cancelled_scheduled": cancelled})


# ---------------------------------------------------------------- public API

@app.get("/api/flags/<name>/check")
def check(name):
    """调用方接口：带身份（可再带一身属性 attrs），得到 开/关。

    identity 走查询参数（调用方需做 URL 编码），允许包含斜杠、空格、
    引号等任意字符；不做 strip，首尾空格也是身份的一部分。
    attrs 为 URL 编码的扁平 JSON 对象，如 attrs=%7B%22plan%22%3A%22pro%22%7D；
    带了属性且对上管理端定的打开条件时，结果由属性条件层决定。
    若开关配了依赖，会用同一身份同一身属性先求被依赖的开关；
    被依赖的不开时，本开关返回 enabled=false、reason=depends_on。
    """
    identity = request.args.get("identity")
    if not identity:
        return jsonify({"error": "identity is required"}), 400
    try:
        attrs = parse_attrs(request.args.get("attrs"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db = get_db()
    flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    person = canonical_identity(db, identity)
    enabled, reason, config, variant = evaluate(db, flag, person, attrs)
    body = {
        "flag": name,
        "identity": identity,
        "enabled": enabled,
        "reason": reason,
    }
    # 开才带开关挂的配置（冻住的人拿冻住那一刻那份）；关不带这个键
    if config is not None:
        body["config"] = config
    # 定了档且最终为开才带档名（没定档的开关只回答开/关，没有这个键）
    if variant is not None:
        body["variant"] = variant
    if attrs is not None:
        body["attrs"] = attrs
    return jsonify(body)


@app.get("/api/bundle")
def bundle():
    """调用方整包接口：带身份（可再带一身属性 attrs），一次拿走所有开关的
    开/关与整包版本。

    整包按 (身份, 属性) 分别记账：同一人带不同属性是不同的包、各自有版本；
    配置不变时同一 (身份, 属性) 多次来拿，每个开关的结果与版本都不变。
    带上 version 参数可校验手中的包是否仍然有效：valid=false 即已过期，
    响应里同时附带按当前规则重算的新版本与新结果。注意版本必须是拿着
    同一身属性领到的，属性不同的两个包互不算过期。
    """
    identity = request.args.get("identity")
    if not identity:
        return jsonify({"error": "identity is required"}), 400
    try:
        attrs = parse_attrs(request.args.get("attrs"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db = get_db()
    # 收在一起的身份按同一个人算：求值、账本与版本都记在主身份名下，所以这一拨
    # 里任何一个身份来拿，拿到的是同一个包、同一个版本。
    person = canonical_identity(db, identity)
    results, content_hash = compute_bundle(db, person, attrs)
    a_hash = attrs_hash_of(attrs)
    a_json = canonical_json(attrs) if attrs else ""
    row = db.execute(
        "SELECT content_hash, generation FROM bundles"
        " WHERE identity=? AND attrs_hash=?",
        (person, a_hash),
    ).fetchone()
    if row is None:
        generation = 1
    elif row["content_hash"] == content_hash:
        generation = row["generation"]
    else:
        # 兜底：内容变了但没经过 record_invalidations 推进（不应发生），
        # 仍保证版本向前、不与任何已发版本重复
        generation = row["generation"] + 1
    version = make_version(content_hash, generation)
    db.execute(
        "INSERT INTO bundles"
        " (identity, attrs_hash, attrs_json, version, content_hash, generation,"
        " results, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(identity, attrs_hash) DO UPDATE SET attrs_json=excluded.attrs_json,"
        " version=excluded.version, content_hash=excluded.content_hash,"
        " generation=excluded.generation, results=excluded.results,"
        " updated_at=excluded.updated_at",
        (person, a_hash, a_json, version, content_hash, generation,
         json.dumps(results, ensure_ascii=False), time.time()),
    )
    db.commit()
    # 响应仍回显调用方来问时写的那个身份（账本内部记主身份）
    body = {"identity": identity, "version": version, "flags": results}
    if attrs is not None:
        body["attrs"] = attrs
    held = request.args.get("version")
    if held is not None:
        body["valid"] = held == version
    return jsonify(body)


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})




# ---------------------------------------------------------------- admin API

@app.get("/api/flags")
@require_admin
def list_flags():
    db = get_db()
    rows = db.execute("SELECT * FROM flags ORDER BY name").fetchall()
    groups = {
        r["flag_id"]: r["name"]
        for r in db.execute(
            "SELECT m.flag_id, g.name FROM group_members m"
            " JOIN mutex_groups g ON g.id = m.group_id"
        )
    }
    dep_names = {
        r["child_id"]: r["name"]
        for r in db.execute(
            "SELECT c.id AS child_id, p.name AS name FROM flags c"
            " JOIN flags p ON p.id = c.depends_on_flag_id"
        )
    }
    pending = {}
    for r in db.execute(
            "SELECT * FROM scheduled_changes WHERE status='pending'"
            " ORDER BY effective_at, id"):
        pending.setdefault(r["flag_id"], []).append(scheduled_to_dict(r))
    result = []
    for row in rows:
        item = flag_to_dict(row, dep_names.get(row["id"], ""))
        item["override_count"] = db.execute(
            "SELECT COUNT(*) c FROM overrides WHERE flag_id=?", (row["id"],)
        ).fetchone()["c"]
        item["freeze_count"] = db.execute(
            "SELECT COUNT(*) c FROM freezes WHERE flag_id=?", (row["id"],)
        ).fetchone()["c"]
        item["group"] = groups.get(row["id"])
        item["scheduled_changes"] = pending.get(row["id"], [])
        result.append(item)
    return jsonify(result)


@app.post("/api/flags")
@require_admin
def create_flag():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not all(c.isalnum() or c in "-_." for c in name):
        return jsonify({"error": "name may only contain letters, digits, - _ ."}), 400
    db = get_db()
    if db.execute("SELECT 1 FROM flags WHERE name=?", (name,)).fetchone():
        return jsonify({"error": "flag already exists"}), 409
    try:
        targeting_json = validate_targeting(body.get("targeting"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        rollout_condition_json = validate_rollout_condition(
            body.get("rollout_condition"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        rollout_rules_json = validate_rollout_rules(body.get("rollout_rules"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        variants_json = validate_variants(body.get("variants"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        config_json = validate_config(body.get("config"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    now = time.time()
    default_enabled = 1 if body.get("default_enabled") else 0
    cur = db.execute(
        "INSERT INTO flags (name, description, default_enabled, targeting_rule,"
        " rollout_condition, rollout_rules, variants, flag_config,"
        " created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, body.get("description", ""), default_enabled, targeting_json,
         rollout_condition_json, rollout_rules_json, variants_json, config_json,
         now, now),
    )
    audit(actor(), name, "default", "create_flag",
          f"default_enabled={bool(default_enabled)}"
          + (f" targeting={targeting_json}" if targeting_json else "")
          + (f" rollout_condition={rollout_condition_json}"
             if rollout_condition_json else "")
          + (f" rollout_rules={rollout_rules_json}" if rollout_rules_json else "")
          + (f" variants={variants_json}" if variants_json else "")
          + (f" config={config_json}" if config_json else ""))
    # 历史流水：新建即一条全量快照（不依赖任何开关）
    record_history(
        db, now, "flag_upsert", subject=name, actor_name=actor(),
        payload={"default_enabled": bool(default_enabled), "rollout_percent": 0,
                 "rollout_condition": rollout_condition_json,
                 "rollout_rules": rollout_rules_json,
                 "variants": variants_json,
                 "kill_switch": False, "targeting": targeting_json,
                 "config": config_json, "depends_on": ""})
    db.commit()
    record_invalidations(db, actor(), f"create_flag {name}")
    return jsonify({"ok": True}), 201


# 可预约定时生效的开关字段（与 PATCH 立即生效支持的字段一致）
SCHEDULABLE_FIELDS = ("kill_switch", "default_enabled", "rollout_percent",
                      "rollout_condition", "rollout_rules", "variants",
                      "description",
                      "targeting", "depends_on", "config")


def validate_depends_on(db, flag, raw):
    """校验管理端要设置的依赖开关，返回 (depends_on_name, error)。

    None / "" / 空串表示解除依赖（返回 ("", None)）。否则必须是另一个
    已存在的开关，且加上「本开关 -> 该开关」这条边后不能成环（自依赖
    同样拒绝）。error 非空时调用方返回 400，不落库。
    """
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return None, "depends_on must be a flag name (string), or null/empty to clear"
    dep_name = raw.strip()
    if dep_name == "":
        return "", None
    if dep_name == flag["name"]:
        return None, "a flag cannot depend on itself"
    dep = db.execute("SELECT id FROM flags WHERE name=?", (dep_name,)).fetchone()
    if dep is None:
        return None, f"depends_on flag '{dep_name}' not found"
    # 沿现有的依赖边往下走：若能从被依赖者走回本开关，加上新边就成环
    seen = set()
    cursor = dep["id"]
    while cursor is not None and cursor not in seen:
        if cursor == flag["id"]:
            return None, "circular dependency rejected"
        seen.add(cursor)
        row = db.execute(
            "SELECT depends_on_flag_id FROM flags WHERE id=?", (cursor,),
        ).fetchone()
        cursor = row["depends_on_flag_id"] if row else None
    return dep_name, None


def apply_flag_fields(db, flag, body):
    """把 kill_switch / default_enabled / rollout_percent / rollout_condition /
    rollout_rules / variants / description / targeting / depends_on / config
    写到开关上。

    立即生效与定时生效到点应用共用这一段。返回 (changes, error)：
    changes 是 (layer, action, detail) 列表（值没变的字段不在列）；
    error 非空表示校验失败，调用方不应提交事务。
    """
    changes = []

    if "depends_on" in body:
        dep_name, err = validate_depends_on(db, flag, body["depends_on"])
        if err:
            return None, err
        old_id = flag["depends_on_flag_id"]
        old_name = ""
        if old_id is not None:
            row = db.execute("SELECT name FROM flags WHERE id=?", (old_id,)).fetchone()
            old_name = row["name"] if row else ""
        if dep_name != old_name:
            new_id = None
            if dep_name:
                new_id = db.execute(
                    "SELECT id FROM flags WHERE name=?", (dep_name,),
                ).fetchone()["id"]
            db.execute("UPDATE flags SET depends_on_flag_id=?, updated_at=? WHERE id=?",
                       (new_id, time.time(), flag["id"]))
            if new_id is None:
                changes.append(("depends_on", "clear_dependency",
                                f"depends_on removed (was {old_name})"))
            else:
                detail = f"depends_on={dep_name}"
                if old_name:
                    detail = f"depends_on: {old_name} -> {dep_name}"
                changes.append(("depends_on", "set_dependency", detail))
    if "targeting" in body:
        try:
            new_rule = validate_targeting(body["targeting"])
        except ValueError as e:
            return None, str(e)
        if new_rule != flag["targeting_rule"]:
            db.execute("UPDATE flags SET targeting_rule=?, updated_at=? WHERE id=?",
                       (new_rule, time.time(), flag["id"]))
            if new_rule:
                changes.append(("targeting", "set_targeting", f"targeting={new_rule}"))
            else:
                changes.append(("targeting", "clear_targeting",
                                f"targeting removed (was {flag['targeting_rule']})"))
    if "rollout_condition" in body:
        try:
            new_cond = validate_rollout_condition(body["rollout_condition"])
        except ValueError as e:
            return None, str(e)
        if new_cond != flag["rollout_condition"]:
            db.execute("UPDATE flags SET rollout_condition=?, updated_at=? WHERE id=?",
                       (new_cond, time.time(), flag["id"]))
            if new_cond:
                changes.append(("rollout", "set_rollout_condition",
                                f"rollout_condition={new_cond}"))
            else:
                changes.append(("rollout", "clear_rollout_condition",
                                "rollout_condition removed"
                                f" (was {flag['rollout_condition']})"))
    if "rollout_rules" in body:
        try:
            new_rules = validate_rollout_rules(body["rollout_rules"])
        except ValueError as e:
            return None, str(e)
        if new_rules != flag["rollout_rules"]:
            db.execute("UPDATE flags SET rollout_rules=?, updated_at=? WHERE id=?",
                       (new_rules, time.time(), flag["id"]))
            if new_rules:
                changes.append(("rollout", "set_rollout_rules",
                                f"rollout_rules={new_rules}"))
            else:
                changes.append(("rollout", "clear_rollout_rules",
                                "rollout_rules removed"
                                f" (was {flag['rollout_rules']})"))
    if "variants" in body:
        try:
            new_variants = validate_variants(body["variants"])
        except ValueError as e:
            return None, str(e)
        if new_variants != flag["variants"]:
            db.execute("UPDATE flags SET variants=?, updated_at=? WHERE id=?",
                       (new_variants, time.time(), flag["id"]))
            if new_variants:
                changes.append(("rollout", "set_variants",
                                f"variants={new_variants}"))
            else:
                changes.append(("rollout", "clear_variants",
                                f"variants removed (was {flag['variants']})"))
    if "config" in body:
        try:
            new_config = validate_config(body["config"])
        except ValueError as e:
            return None, str(e)
        if new_config != flag["flag_config"]:
            db.execute("UPDATE flags SET flag_config=?, updated_at=? WHERE id=?",
                       (new_config, time.time(), flag["id"]))
            if new_config:
                changes.append(("config", "set_config", f"config={new_config}"))
            else:
                changes.append(("config", "clear_config",
                                f"config removed (was {flag['flag_config']})"))
    if "kill_switch" in body:
        new = 1 if body["kill_switch"] else 0
        if new != flag["kill_switch"]:
            db.execute("UPDATE flags SET kill_switch=?, updated_at=? WHERE id=?",
                       (new, time.time(), flag["id"]))
            changes.append(("kill_switch",
                            "enable_kill_switch" if new else "disable_kill_switch",
                            f"kill_switch={bool(new)}"))
    if "default_enabled" in body:
        new = 1 if body["default_enabled"] else 0
        if new != flag["default_enabled"]:
            db.execute("UPDATE flags SET default_enabled=?, updated_at=? WHERE id=?",
                       (new, time.time(), flag["id"]))
            changes.append(("default", "set_default", f"default_enabled={bool(new)}"))
    if "rollout_percent" in body:
        new = body["rollout_percent"]
        if not isinstance(new, int) or not 0 <= new <= 100:
            return None, "rollout_percent must be an int in [0,100]"
        if new != flag["rollout_percent"]:
            db.execute("UPDATE flags SET rollout_percent=?, updated_at=? WHERE id=?",
                       (new, time.time(), flag["id"]))
            changes.append(("rollout", "set_rollout",
                            f"rollout_percent: {flag['rollout_percent']} -> {new}"))
    if "description" in body:
        db.execute("UPDATE flags SET description=?, updated_at=? WHERE id=?",
                   (body["description"], time.time(), flag["id"]))
    return changes, None


def schedule_flag_change(db, flag, body, effective_at):
    """把一次开关配置变更约到 effective_at 生效：校验后落库，到点前不影响
    求值与整包版本，由 apply_due_scheduled_changes 到点应用。"""
    if isinstance(effective_at, bool) or not isinstance(effective_at, (int, float)):
        return jsonify({"error": "effective_at must be a unix timestamp in seconds"}), 400
    if effective_at <= time.time():
        return jsonify({"error": "effective_at must be in the future"
                                 " (omit it to apply immediately)"}), 400
    payload = {k: body[k] for k in SCHEDULABLE_FIELDS if k in body}
    if not payload:
        return jsonify({"error": "nothing to schedule"
                                 " (no kill_switch/default_enabled/rollout_percent"
                                 "/rollout_condition/rollout_rules/variants"
                                 "/description"
                                 "/depends_on/config given)"}), 400
    # 与立即生效同一套校验，避免约了一个到点应用不了的值
    if "rollout_percent" in payload:
        v = payload["rollout_percent"]
        if not isinstance(v, int) or not 0 <= v <= 100:
            return jsonify({"error": "rollout_percent must be an int in [0,100]"}), 400
    if "targeting" in payload:
        try:
            validate_targeting(payload["targeting"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    if "rollout_condition" in payload:
        try:
            validate_rollout_condition(payload["rollout_condition"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    if "rollout_rules" in payload:
        try:
            validate_rollout_rules(payload["rollout_rules"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    if "variants" in payload:
        try:
            validate_variants(payload["variants"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    if "config" in payload:
        try:
            # 预约表里存规范化后的值：None 表示清除（与立即生效 PATCH 同口径），
            # 到点应用时 apply_flag_fields 再走同一套写入
            payload["config"] = (json.loads(validate_config(payload["config"]))
                                 if payload["config"] is not None else None)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    if "depends_on" in payload:
        # 环校验按「预约到点那一刻」之前的当前依赖图做；到点应用时若图已变
        # 形成环（理论上每次写入都拒绝成环，不可达），该条预约会记为 failed
        _, err = validate_depends_on(db, flag, payload["depends_on"])
        if err:
            return jsonify({"error": err}), 400
    cur = db.execute(
        "INSERT INTO scheduled_changes"
        " (flag_id, flag_name, changes, effective_at, created_by, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (flag["id"], flag["name"], json.dumps(payload, ensure_ascii=False),
         effective_at, actor(), time.time()))
    audit(actor(), flag["name"], "schedule", "schedule_change",
          f"effective_at={effective_at}"
          f" changes={json.dumps(payload, ensure_ascii=False)}")
    db.commit()
    return jsonify({"ok": True, "scheduled": True,
                    "change_id": cur.lastrowid, "effective_at": effective_at}), 202


def apply_due_scheduled_changes():
    """应用所有已到点的定时变更（惰性：随任意请求触发，无需后台线程）。

    到点前：定时变更只在 scheduled_changes 表里，不参与求值，整包版本不变。
    到点后：第一个进来的请求把变更写到开关上，并像管理端手动改动一样重算
    已发整包、记失效——此后调用方按新规则求值、整包换新版本，到点前发出
    的旧版本随之过期。操作人记预约时的人，而不是触发这次应用的请求方。
    """
    db = get_db()
    now = time.time()
    rows = db.execute(
        "SELECT * FROM scheduled_changes"
        " WHERE status='pending' AND effective_at<=? ORDER BY effective_at, id",
        (now,),
    ).fetchall()
    for r in rows:
        # 先抢占再应用：并发请求下只有一个请求会真正执行这次变更
        cur = db.execute(
            "UPDATE scheduled_changes SET status='applied', applied_at=?"
            " WHERE id=? AND status='pending'",
            (now, r["id"]))
        if cur.rowcount == 0:
            continue
        flag = db.execute("SELECT * FROM flags WHERE id=?", (r["flag_id"],)).fetchone()
        if flag is None:  # 开关已删（正常路径下删除时会取消预约，这里兜底）
            db.execute("UPDATE scheduled_changes SET status='failed' WHERE id=?",
                       (r["id"],))
            audit(r["created_by"], r["flag_name"], "schedule",
                  "scheduled_change_failed", "flag no longer exists")
            db.commit()
            continue
        changes, err = apply_flag_fields(db, flag, json.loads(r["changes"]))
        if err is not None:  # 落库前已校验，理论不可达；防御性处理
            db.execute("UPDATE scheduled_changes SET status='failed' WHERE id=?",
                       (r["id"],))
            audit(r["created_by"], r["flag_name"], "schedule",
                  "scheduled_change_failed", err)
            db.commit()
            continue
        for layer, action, detail in changes:
            audit(r["created_by"], r["flag_name"], layer, action,
                  detail + "（定时生效）")
        audit(r["created_by"], r["flag_name"], "schedule", "apply_scheduled",
              f"change_id={r['id']} effective_at={r['effective_at']}")
        db.commit()
        desc = f"scheduled_change {r['flag_name']}"
        if changes:
            desc += ": " + ", ".join(d for _, _, d in changes)
        record_invalidations(db, r["created_by"], desc)


@app.before_request
def _select_environment_and_apply_due_changes():
    if request.method == "OPTIONS" or request.url_rule is None or request.endpoint in {
        "healthz", "list_environments", "create_environment", "admin_page",
        "push_flags"}:
        return
    name, err = require_request_environment()
    if err is not None:
        return err
    g.environment = name
    apply_due_scheduled_changes()


@app.patch("/api/flags/<name>")
@require_admin
def update_flag(name):
    db = get_db()
    flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    body = request.get_json(force=True)

    # 约了生效时间：落库为定时变更，到点前求值与整包版本都不变
    effective_at = body.get("effective_at")
    if effective_at is not None:
        return schedule_flag_change(db, flag, body, effective_at)

    changes, err = apply_flag_fields(db, flag, body)
    if err is not None:
        return jsonify({"error": err}), 400
    for layer, action, detail in changes:
        audit(actor(), name, layer, action, detail)
    if changes:
        # 历史流水：立即生效的改动落一条「写完后」全量快照（description-only
        # 不改求值，不落流水）
        fresh = db.execute("SELECT * FROM flags WHERE id=?", (flag["id"],)).fetchone()
        record_history(db, time.time(), "flag_upsert", subject=name,
                       payload=flag_snapshot_payload(fresh), actor_name=actor())
    db.commit()
    desc = f"update_flag {name}"
    if changes:
        desc += ": " + ", ".join(d for _, _, d in changes)
    record_invalidations(db, actor(), desc)
    return jsonify({"ok": True, "changed": len(changes)})


@app.delete("/api/flags/<name>")
@require_admin
def delete_flag(name):
    db = get_db()
    flag = db.execute("SELECT id FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    db.execute("DELETE FROM flags WHERE id=?", (flag["id"],))
    # 还没到点的定时变更随开关一起取消
    db.execute("UPDATE scheduled_changes SET status='cancelled'"
               " WHERE flag_id=? AND status='pending'", (flag["id"],))
    # 新库由 ON DELETE SET NULL 解除「别人对它」的依赖；老库迁移出来的外键
    # 没有该动作，这里显式兜底，避免留下指向已删开关的依赖
    db.execute("UPDATE flags SET depends_on_flag_id=NULL"
               " WHERE depends_on_flag_id=?", (flag["id"],))
    # 历史流水：删除事件（重放时该开关及其 override/freeze/组成员/落定从这一刻消失）
    record_history(db, time.time(), "flag_delete", subject=name,
                   actor_name=actor())
    audit(actor(), name, "default", "delete_flag")
    db.commit()
    record_invalidations(db, actor(), f"delete_flag {name}")
    return jsonify({"ok": True})


@app.get("/api/flags/<name>/overrides")
@require_admin
def list_overrides(name):
    db = get_db()
    flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    rows = db.execute(
        "SELECT identity, enabled, created_at FROM overrides"
        " WHERE flag_id=? ORDER BY identity", (flag["id"],),
    ).fetchall()
    return jsonify([
        {"identity": r["identity"], "enabled": bool(r["enabled"]),
         "created_at": r["created_at"]}
        for r in rows
    ])


@app.put("/api/flags/<name>/overrides")
@require_admin
def put_override(name):
    """设置单人强制。identity 放在 JSON body 里，因此可以包含
    斜杠、空格、引号等任意字符，不受 URL 路径限制。"""
    body = request.get_json(force=True, silent=True) or {}
    identity = body.get("identity")
    if not isinstance(identity, str) or identity == "":
        return jsonify({"error": "identity is required (non-empty string)"}), 400
    if "enabled" not in body:
        return jsonify({"error": "enabled is required"}), 400
    db = get_db()
    flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    person = canonical_identity(db, identity)
    enabled = 1 if body["enabled"] else 0
    now = time.time()
    db.execute(
        "INSERT INTO overrides (flag_id, identity, enabled, created_at) VALUES (?,?,?,?)"
        " ON CONFLICT(flag_id, identity) DO UPDATE SET enabled=excluded.enabled",
        (flag["id"], person, enabled, now),
    )
    audit(actor(), name, "override", "set_override",
          f"identity={identity} enabled={bool(enabled)}"
          + (f" person={person}" if person != identity else ""))
    record_history(db, now, "override", subject=name, identity=person,
                   payload={"enabled": bool(enabled)}, actor_name=actor())
    db.commit()
    record_invalidations(db, actor(),
                         f"set_override {name} identity={person}")
    return jsonify({"ok": True})


@app.delete("/api/flags/<name>/overrides")
@require_admin
def delete_override(name):
    """移除单人强制。identity 放在 JSON body 里。"""
    body = request.get_json(force=True, silent=True) or {}
    identity = body.get("identity")
    if not isinstance(identity, str) or identity == "":
        return jsonify({"error": "identity is required (non-empty string)"}), 400
    db = get_db()
    flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    person = canonical_identity(db, identity)
    db.execute("DELETE FROM overrides WHERE flag_id=? AND identity=?",
               (flag["id"], person))
    audit(actor(), name, "override", "remove_override", f"identity={identity}")
    record_history(db, time.time(), "override_delete", subject=name,
                   identity=person, payload={}, actor_name=actor())
    db.commit()
    record_invalidations(db, actor(),
                         f"remove_override {name} identity={person}")
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 结果冻结（freeze）

def freeze_result(db, flag, identity):
    """冻住时按当前完整规则求一次「此刻」的结果（不带属性口径），
    返回 (enabled, reason, config, variant)。跳过本开关已有的冻结行——重新冻
    同一个人时，冻住的是「假如现在解冻会算出的结果」；被依赖开关的冻结照常
    生效。config 即此人此刻判开时开关挂的配置（冻住后按这份快照下发），
    判关为 None；variant 同理为此刻落的档名，判关或没定档为 None。"""
    return evaluate(db, flag, identity, None,
                    _skip_freeze_id=flag["id"])


@app.get("/api/flags/<name>/freezes")
@require_admin
def list_freezes(name):
    """管理端：列出某开关被冻住结果的人及其冻住的值、冻住时的理由。"""
    db = get_db()
    flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    rows = db.execute(
        "SELECT identity, frozen_enabled, frozen_reason, frozen_config,"
        " frozen_variant, created_by, created_at"
        " FROM freezes WHERE flag_id=? ORDER BY identity", (flag["id"],),
    ).fetchall()
    return jsonify([
        {"identity": r["identity"], "enabled": bool(r["frozen_enabled"]),
         "frozen_reason": r["frozen_reason"],
         "config": json.loads(r["frozen_config"]) if r["frozen_config"] else None,
         "variant": r["frozen_variant"] or None,
         "created_by": r["created_by"],
         "created_at": r["created_at"]}
        for r in rows
    ])


@app.put("/api/flags/<name>/freezes")
@require_admin
def put_freeze(name):
    """把某人对此开关此刻的结果冻住。

    冻住时先按当前全部规则（不带属性口径）求一次值，把开/关连同当时的
    理由存下来。此后该人来问一律给冻住的值（全关仍压过），改默认值、放量、
    属性条件、依赖、单人强制都不动它。重复冻同一个人 = 按此刻规则重新冻
    （值没变就是 no-op，不产生失效记录）。冻住让该身份的已发整包换新版本。
    """
    body = request.get_json(force=True, silent=True) or {}
    identity = body.get("identity")
    if not isinstance(identity, str) or identity == "":
        return jsonify({"error": "identity is required (non-empty string)"}), 400
    db = get_db()
    flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    person = canonical_identity(db, identity)
    enabled, reason, config, variant = freeze_result(db, flag, person)
    now = time.time()
    existing = db.execute(
        "SELECT frozen_enabled, frozen_config, frozen_variant FROM freezes"
        " WHERE flag_id=? AND identity=?",
        (flag["id"], person),
    ).fetchone()
    if existing is not None and bool(existing["frozen_enabled"]) == enabled:
        # 冻住的开/关没变就是 no-op：值、理由、配置与档名快照都不刷新（与
        # 「冻住后雷打不动」一致，想拿新配置/新档名需先解冻），不产生失效
        # 记录。
        return jsonify({"ok": True, "changed": False, "enabled": enabled,
                        "reason": reason})
    # 第一次冻，或重新冻把结果从开冻成关 / 从关冻成开：此刻挂着的配置与
    # 档名一并快照——冻在开带这两份（没挂 / 没定档为 ''），冻在关为 ''
    # （关不带配置与档名）。
    config_json = flag["flag_config"] if enabled else ""
    variant_name = variant if enabled and variant is not None else ""
    db.execute(
        "INSERT INTO freezes (flag_id, identity, frozen_enabled, frozen_reason,"
        " frozen_config, frozen_variant, created_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(flag_id, identity) DO UPDATE SET"
        " frozen_enabled=excluded.frozen_enabled,"
        " frozen_reason=excluded.frozen_reason,"
        " frozen_config=excluded.frozen_config,"
        " frozen_variant=excluded.frozen_variant,"
        " created_by=excluded.created_by,"
        " created_at=excluded.created_at",
        (flag["id"], person, 1 if enabled else 0, reason, config_json,
         variant_name, actor(), now),
    )
    audit(actor(), name, "freeze", "freeze_result",
          f"identity={identity} enabled={enabled} reason={reason}"
          + (f" variant={variant_name}" if variant_name else "")
          + (f" person={person}" if person != identity else ""))
    record_history(db, now, "freeze", subject=name, identity=person,
                   payload={"enabled": bool(enabled), "reason": reason,
                            "config": config_json,
                            "variant": variant_name},
                   actor_name=actor())
    db.commit()
    record_invalidations(db, actor(),
                         f"freeze_result {name} identity={person} -> {enabled}")
    body = {"ok": True, "changed": True, "enabled": enabled, "reason": reason}
    if config is not None:
        body["config"] = config
    if variant is not None:
        body["variant"] = variant
    return jsonify(body)


@app.delete("/api/flags/<name>/freezes")
@require_admin
def delete_freeze(name):
    """解冻：移除某人对此开关的结果冻结，此后该人立即按解冻当时的规则求值。

    解冻让该身份的已发整包（含各身属性包）换新版本；解冻本身不改任何
    开关配置。identity 放在 JSON body 里。
    """
    body = request.get_json(force=True, silent=True) or {}
    identity = body.get("identity")
    if not isinstance(identity, str) or identity == "":
        return jsonify({"error": "identity is required (non-empty string)"}), 400
    db = get_db()
    flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    person = canonical_identity(db, identity)
    cur = db.execute("DELETE FROM freezes WHERE flag_id=? AND identity=?",
                     (flag["id"], person))
    if cur.rowcount == 0:
        return jsonify({"ok": True, "changed": False})
    audit(actor(), name, "freeze", "unfreeze_result", f"identity={identity}")
    record_history(db, time.time(), "freeze_delete", subject=name,
                   identity=person, payload={}, actor_name=actor())
    db.commit()
    record_invalidations(db, actor(),
                         f"unfreeze_result {name} identity={person}")
    return jsonify({"ok": True, "changed": True})


# ---------------------------------------------------------------- scheduled changes

def scheduled_to_dict(r):
    return {
        "id": r["id"],
        "flag": r["flag_name"],
        "changes": json.loads(r["changes"]),
        "effective_at": r["effective_at"],
        "created_by": r["created_by"],
        "created_at": r["created_at"],
    }


@app.get("/api/scheduled-changes")
@require_admin
def list_scheduled_changes():
    """管理端：所有还没到点的定时变更（已生效/已取消的进审计日志）。"""
    rows = get_db().execute(
        "SELECT * FROM scheduled_changes WHERE status='pending'"
        " ORDER BY effective_at, id"
    ).fetchall()
    return jsonify([scheduled_to_dict(r) for r in rows])


@app.delete("/api/scheduled-changes/<int:change_id>")
@require_admin
def cancel_scheduled_change(change_id):
    """管理端：取消一个还没到点的定时变更；取消后到点也不会生效。"""
    db = get_db()
    r = db.execute(
        "SELECT * FROM scheduled_changes WHERE id=?", (change_id,),
    ).fetchone()
    if r is None:
        return jsonify({"error": "scheduled change not found"}), 404
    if r["status"] != "pending":
        return jsonify({"error": f"cannot cancel a change that is {r['status']}"}), 409
    db.execute("UPDATE scheduled_changes SET status='cancelled' WHERE id=?",
               (change_id,))
    audit(actor(), r["flag_name"], "schedule", "cancel_scheduled",
          f"change_id={change_id} changes={r['changes']}")
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- drafts（发布稿）

# 稿里允许收的开关字段（与立即生效 PATCH 支持的字段一致，但不含 effective_at：
# 稿在发布的一刻统一生效，不能再约时间）
DRAFT_FIELDS = ("kill_switch", "default_enabled", "rollout_percent",
                "rollout_condition", "rollout_rules", "variants", "description",
                "targeting", "depends_on", "config")


def normalize_patch(db, body):
    """校验并规范化「对一个开关的一次改动」，返回规范化后的 dict。

    收入发布稿时用：只做字段级校验与归一化，**不判依赖环**——单条改动是否
    会让依赖图成环，只有发布时把整稿与现网配置合并后才知道（稿里 A→B、B→A
    各自单看都合法，得在发布时一起拒绝）。depends_on 这里只查目标开关是否
    存在；沿链成环由发布前的整稿合并图校验负责。
    无任何可应用字段抛 ValueError("nothing to apply")；字段非法抛 ValueError。
    """
    out = {}
    if "depends_on" in body:
        raw = body["depends_on"]
        if not isinstance(raw, str):
            raise ValueError("depends_on must be a flag name (string),"
                             " or null/empty to clear")
        dep = raw.strip()
        if dep:
            if db.execute("SELECT 1 FROM flags WHERE name=?", (dep,)).fetchone() is None:
                raise ValueError(f"depends_on flag '{dep}' not found")
        out["depends_on"] = dep  # "" = 解除依赖
    if "targeting" in body:
        out["targeting"] = validate_targeting(body["targeting"])  # "" = 清除条件
    if "rollout_condition" in body:
        out["rollout_condition"] = validate_rollout_condition(
            body["rollout_condition"])  # "" = 清除放量条件
    if "rollout_rules" in body:
        out["rollout_rules"] = validate_rollout_rules(
            body["rollout_rules"])  # "" = 清除放量规矩
    if "variants" in body:
        out["variants"] = validate_variants(body["variants"])  # "" = 清除档
    if "config" in body:
        out["config"] = validate_config(body["config"])  # "" = 清除配置
    if "kill_switch" in body:
        out["kill_switch"] = 1 if body["kill_switch"] else 0
    if "default_enabled" in body:
        out["default_enabled"] = 1 if body["default_enabled"] else 0
    if "rollout_percent" in body:
        v = body["rollout_percent"]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100:
            raise ValueError("rollout_percent must be an int in [0,100]")
        out["rollout_percent"] = v
    if "description" in body:
        if not isinstance(body["description"], str):
            raise ValueError("description must be a string")
        out["description"] = body["description"]
    return out


def draft_change_to_dict(ch):
    """稿内改动存储格式 -> 对外 JSON（布尔还原；targeting/rollout_condition/config
    的 '' 还原为 None 或 {}，rollout_rules 的 '' 还原为 []）。"""
    out = dict(ch)
    if "targeting" in out:
        out["targeting"] = json.loads(out["targeting"]) if out["targeting"] else {}
    if "rollout_condition" in out:
        out["rollout_condition"] = (json.loads(out["rollout_condition"])
                                    if out["rollout_condition"] else {})
    if "rollout_rules" in out:
        out["rollout_rules"] = (json.loads(out["rollout_rules"])
                                if out["rollout_rules"] else [])
    if "variants" in out:
        out["variants"] = json.loads(out["variants"]) if out["variants"] else []
    if "config" in out:
        out["config"] = json.loads(out["config"]) if out["config"] else None
    for k in ("kill_switch", "default_enabled"):
        if k in out:
            out[k] = bool(out[k])
    return out


def draft_to_dict(db, r):
    changes = {}
    for cr in db.execute(
            "SELECT flag_name, changes, updated_by, updated_at FROM draft_changes"
            " WHERE draft_id=? ORDER BY flag_name", (r["id"],)):
        item = draft_change_to_dict(json.loads(cr["changes"]))
        item["updated_by"] = cr["updated_by"]
        item["updated_at"] = cr["updated_at"]
        changes[cr["flag_name"]] = item
    return {
        "id": r["id"],
        "note": r["note"],
        "status": r["status"],
        "created_by": r["created_by"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "published_at": r["published_at"],
        "flags": changes,
    }


def merged_dependency_cycle(db, staged, what="the draft as a whole"):
    """合并校验：现网依赖图叠加一批改动后是否成环。

    staged 为 {flag_name: 合并后的改动 dict}。改动里改了 depends_on 的边以改动
    为准（"" = 解除），其余边沿用现网。返回错误串；None 表示图无环、所有
    被依赖目标都存在。成环 / 自依赖 / 指向不存在的开关都会让这批改动不能生效。
    发布稿与跨环境推送共用这段；what 是这批改动的说法，只用于成环报错文案。
    """
    name_of_dep_id = {r["id"]: r["name"]
                      for r in db.execute("SELECT id, name FROM flags")}
    graph = {}
    for r in db.execute("SELECT name, depends_on_flag_id FROM flags"):
        did = r["depends_on_flag_id"]
        graph[r["name"]] = name_of_dep_id.get(did) if did is not None else None
    for name, ch in staged.items():
        if "depends_on" in ch:
            graph[name] = ch["depends_on"] or None
    for name, dep in graph.items():
        if dep is not None and dep != name and dep not in graph:
            return f"depends_on flag '{dep}' not found"
    # 递归 DFS 三色判环（自依赖也在这里被抓：访问自己时自己还是灰色）
    color = {}

    def visit(node):
        color[node] = 1
        dep = graph[node]
        if dep is not None:
            if color.get(dep, 0) == 1:
                return True
            if color.get(dep, 0) == 0 and visit(dep):
                return True
        color[node] = 2
        return False

    for n in graph:
        if color.get(n, 0) == 0 and visit(n):
            return f"circular dependency rejected: {what} would form a cycle"
    return None


def apply_patch_to_flag(db, flag, ch, now):
    """发布时把稿内对一个开关的改动直接写到行上，返回 (layer, action, detail)
    列表（值没变的字段不列；description 照写但不计变更）。

    与 apply_flag_fields 的区别：不再单条做沿链环校验——目标存在与整图无环
    已由发布前的 merged_dependency_cycle 统一保证；逐条按现网图校验会把
    「边整体转向」这种最终无环的整稿误判成环。
    """
    changes = []
    fid = flag["id"]
    if "depends_on" in ch:
        dep = ch["depends_on"]
        old_id = flag["depends_on_flag_id"]
        old_name = ""
        if old_id is not None:
            row = db.execute("SELECT name FROM flags WHERE id=?", (old_id,)).fetchone()
            old_name = row["name"] if row else ""
        if dep != old_name:
            new_id = None
            if dep:
                row = db.execute("SELECT id FROM flags WHERE name=?", (dep,)).fetchone()
                new_id = row["id"] if row else None
            db.execute("UPDATE flags SET depends_on_flag_id=?, updated_at=? WHERE id=?",
                       (new_id, now, fid))
            if new_id is None:
                changes.append(("depends_on", "clear_dependency",
                                f"depends_on removed (was {old_name})"))
            else:
                detail = f"depends_on={dep}"
                if old_name:
                    detail = f"depends_on: {old_name} -> {dep}"
                changes.append(("depends_on", "set_dependency", detail))
    if "targeting" in ch:
        new_rule = ch["targeting"]
        if new_rule != flag["targeting_rule"]:
            db.execute("UPDATE flags SET targeting_rule=?, updated_at=? WHERE id=?",
                       (new_rule, now, fid))
            if new_rule:
                changes.append(("targeting", "set_targeting", f"targeting={new_rule}"))
            else:
                changes.append(("targeting", "clear_targeting",
                                f"targeting removed (was {flag['targeting_rule']})"))
    if "rollout_condition" in ch:
        new_cond = ch["rollout_condition"]
        if new_cond != flag["rollout_condition"]:
            db.execute("UPDATE flags SET rollout_condition=?, updated_at=? WHERE id=?",
                       (new_cond, now, fid))
            if new_cond:
                changes.append(("rollout", "set_rollout_condition",
                                f"rollout_condition={new_cond}"))
            else:
                changes.append(("rollout", "clear_rollout_condition",
                                "rollout_condition removed"
                                f" (was {flag['rollout_condition']})"))
    if "rollout_rules" in ch:
        new_rules = ch["rollout_rules"]
        if new_rules != flag["rollout_rules"]:
            db.execute("UPDATE flags SET rollout_rules=?, updated_at=? WHERE id=?",
                       (new_rules, now, fid))
            if new_rules:
                changes.append(("rollout", "set_rollout_rules",
                                f"rollout_rules={new_rules}"))
            else:
                changes.append(("rollout", "clear_rollout_rules",
                                "rollout_rules removed"
                                f" (was {flag['rollout_rules']})"))
    if "variants" in ch:
        new_variants = ch["variants"]
        if new_variants != flag["variants"]:
            db.execute("UPDATE flags SET variants=?, updated_at=? WHERE id=?",
                       (new_variants, now, fid))
            if new_variants:
                changes.append(("rollout", "set_variants",
                                f"variants={new_variants}"))
            else:
                changes.append(("rollout", "clear_variants",
                                f"variants removed (was {flag['variants']})"))
    if "config" in ch:
        new_config = ch["config"]
        if new_config != flag["flag_config"]:
            db.execute("UPDATE flags SET flag_config=?, updated_at=? WHERE id=?",
                       (new_config, now, fid))
            if new_config:
                changes.append(("config", "set_config", f"config={new_config}"))
            else:
                changes.append(("config", "clear_config",
                                f"config removed (was {flag['flag_config']})"))
    if "kill_switch" in ch:
        new = ch["kill_switch"]
        if new != flag["kill_switch"]:
            db.execute("UPDATE flags SET kill_switch=?, updated_at=? WHERE id=?",
                       (new, now, fid))
            changes.append(("kill_switch",
                            "enable_kill_switch" if new else "disable_kill_switch",
                            f"kill_switch={bool(new)}"))
    if "default_enabled" in ch:
        new = ch["default_enabled"]
        if new != flag["default_enabled"]:
            db.execute("UPDATE flags SET default_enabled=?, updated_at=? WHERE id=?",
                       (new, now, fid))
            changes.append(("default", "set_default", f"default_enabled={bool(new)}"))
    if "rollout_percent" in ch:
        new = ch["rollout_percent"]
        if new != flag["rollout_percent"]:
            db.execute("UPDATE flags SET rollout_percent=?, updated_at=? WHERE id=?",
                       (new, now, fid))
            changes.append(("rollout", "set_rollout",
                            f"rollout_percent: {flag['rollout_percent']} -> {new}"))
    if "description" in ch:
        db.execute("UPDATE flags SET description=?, updated_at=? WHERE id=?",
                   (ch["description"], now, fid))
    return changes


def get_open_draft(db, draft_id):
    """取稿并要求是 open；返回 (row, error_response)。"""
    row = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if row is None:
        return None, (jsonify({"error": "draft not found"}), 404)
    if row["status"] != "open":
        return None, (jsonify({"error": f"draft is {row['status']}, not open"}), 409)
    return row, None


@app.post("/api/drafts")
@require_admin
def create_draft():
    """管理端：开一个发布稿（空稿），之后往里收改动，攒够一起发布。"""
    body = request.get_json(force=True, silent=True) or {}
    note = body.get("note", "")
    if not isinstance(note, str):
        return jsonify({"error": "note must be a string"}), 400
    db = get_db()
    now = time.time()
    cur = db.execute(
        "INSERT INTO drafts (note, status, created_by, created_at, updated_at)"
        " VALUES (?, 'open', ?, ?, ?)",
        (note, actor(), now, now))
    audit(actor(), f"draft:{cur.lastrowid}", "draft", "create_draft",
          f"note={note}" if note else "")
    db.commit()
    return jsonify({"ok": True, "draft_id": cur.lastrowid}), 201


@app.get("/api/drafts")
@require_admin
def list_drafts():
    """管理端：发布稿列表。默认只列未发布（open）的；all=1 连已发布/已丢弃一起列。"""
    db = get_db()
    if request.args.get("all") in ("1", "true"):
        rows = db.execute("SELECT * FROM drafts ORDER BY id DESC").fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM drafts WHERE status='open' ORDER BY id DESC").fetchall()
    return jsonify([draft_to_dict(db, r) for r in rows])


@app.get("/api/drafts/<int:draft_id>")
@require_admin
def get_draft(draft_id):
    db = get_db()
    r = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if r is None:
        return jsonify({"error": "draft not found"}), 404
    return jsonify(draft_to_dict(db, r))


@app.put("/api/drafts/<int:draft_id>/flags/<name>")
@require_admin
def stage_draft_change(draft_id, name):
    """把对一个开关的改动收进稿：按字段并入该稿对此开关已收的改动
    （同一字段后收的覆盖先收的）。稿不发布就一字不生效。"""
    body = request.get_json(force=True, silent=True) or {}
    if body.get("effective_at") is not None:
        return jsonify({"error": "drafts do not support effective_at:"
                                 " publishing applies the whole draft at once"}), 400
    db = get_db()
    draft, err = get_open_draft(db, draft_id)
    if err:
        return err
    flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    try:
        patch = normalize_patch(db, body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not patch:
        return jsonify({"error": "nothing to stage"
                                 " (kill_switch/default_enabled/rollout_percent"
                                 "/rollout_condition/rollout_rules/variants"
                                 "/description"
                                 "/targeting/depends_on/config)"}), 400
    row = db.execute(
        "SELECT changes FROM draft_changes WHERE draft_id=? AND flag_name=?",
        (draft_id, name)).fetchone()
    merged = json.loads(row["changes"]) if row else {}
    merged.update(patch)
    now = time.time()
    db.execute(
        "INSERT INTO draft_changes (draft_id, flag_name, changes, updated_by, updated_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(draft_id, flag_name) DO UPDATE SET changes=excluded.changes,"
        " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
        (draft_id, name, canonical_json(merged), actor(), now))
    db.execute("UPDATE drafts SET updated_at=? WHERE id=?", (now, draft_id))
    audit(actor(), name, "draft", "stage_draft_change",
          f"draft=#{draft_id} fields={','.join(patch)}")
    db.commit()
    return jsonify({"ok": True, "draft_id": draft_id, "flag": name,
                    "changes": draft_change_to_dict(merged)})


@app.delete("/api/drafts/<int:draft_id>/flags/<name>")
@require_admin
def unstage_draft_change(draft_id, name):
    """把某个开关的整段改动从稿里摘掉；稿与其他开关的改动不受影响。"""
    db = get_db()
    _, err = get_open_draft(db, draft_id)
    if err:
        return err
    cur = db.execute("DELETE FROM draft_changes WHERE draft_id=? AND flag_name=?",
                     (draft_id, name))
    if cur.rowcount == 0:
        return jsonify({"error": "no staged change for that flag in the draft"}), 404
    db.execute("UPDATE drafts SET updated_at=? WHERE id=?", (time.time(), draft_id))
    audit(actor(), name, "draft", "unstage_draft_change", f"draft=#{draft_id}")
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/drafts/<int:draft_id>/publish")
@require_admin
def publish_draft(draft_id):
    """发布整稿：合并校验通过才一次性应用所有改动（原子，要么全生效要么不动）。

    校验：稿里每个开关都还在；合并现网配置后的依赖图不成环、依赖目标都存在。
    任一不过：整稿不生效，一个字段都不写，稿仍是 open（可改可丢）。
    通过：所有改动一次事务写入，随后像普通改动一样统一重算已发整包、记一次
    失效——稿里的改动一起生效，整包换新版本，发布前发出的包随之过期。
    """
    db = get_db()
    draft, err = get_open_draft(db, draft_id)
    if err:
        return err
    staged_rows = db.execute(
        "SELECT flag_name, changes FROM draft_changes WHERE draft_id=?",
        (draft_id,)).fetchall()
    staged = {r["flag_name"]: json.loads(r["changes"]) for r in staged_rows}
    if not staged:
        return jsonify({"error": "draft is empty"}), 400
    missing = [n for n in staged
               if db.execute("SELECT 1 FROM flags WHERE name=?", (n,)).fetchone() is None]
    if missing:
        audit(actor(), f"draft:{draft_id}", "draft", "publish_rejected",
              f"flags missing: {','.join(missing)}")
        db.commit()
        return jsonify({"error": "draft targets flags that no longer exist"
                                 f" ({', '.join(missing)}); remove them from the draft"
                                 " before publishing"}), 400
    cycle_err = merged_dependency_cycle(db, staged)
    if cycle_err is not None:
        audit(actor(), f"draft:{draft_id}", "draft", "publish_rejected", cycle_err)
        db.commit()
        return jsonify({"error": cycle_err}), 400
    # 先抢占稿状态：并发发布只有一个能往下走
    now = time.time()
    cur = db.execute(
        "UPDATE drafts SET status='published', published_at=?, updated_at=?"
        " WHERE id=? AND status='open'",
        (now, now, draft_id))
    if cur.rowcount == 0:
        return jsonify({"error": "draft is no longer open"}), 409
    try:
        applied = []
        for name in sorted(staged):
            flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
            changes = apply_patch_to_flag(db, flag, staged[name], now)
            for layer, action, detail in changes:
                audit(actor(), name, layer, action,
                      detail + f"（发布稿 #{draft_id}）")
            applied.append((name, changes))
    except Exception:
        db.rollback()
        raise
    audit(actor(), f"draft:{draft_id}", "draft", "publish_draft",
          "flags=" + ",".join(sorted(staged)))
    db.commit()
    n_changed = sum(len(cs) for _, cs in applied)
    desc = f"publish_draft #{draft_id}"
    details = [d for _, cs in applied for _, _, d in cs]
    if details:
        desc += ": " + ", ".join(details)
    record_invalidations(db, actor(), desc)
    return jsonify({"ok": True, "published": True, "draft_id": draft_id,
                    "flags": sorted(staged), "changed": n_changed})


@app.delete("/api/drafts/<int:draft_id>")
@require_admin
def discard_draft(draft_id):
    """丢弃一个未发布的稿：稿里的改动全部作废，不影响任何求值与整包版本。"""
    db = get_db()
    draft, err = get_open_draft(db, draft_id)
    if err:
        return err
    db.execute("UPDATE drafts SET status='discarded', updated_at=? WHERE id=?",
               (time.time(), draft_id))
    audit(actor(), f"draft:{draft_id}", "draft", "discard_draft", "")
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 预演（preview）

def snapshot_current_state(db, identity):
    """把「现在」线上的完整求值状态读成与 replay_state 相同的内存结构（纯读，不写库）。

    预演专用：在这份内存状态上套用假想改动后用 evaluate_at 求值——全关、冻结、
    依赖、单人强制、属性条件、互斥组、放量、默认全按现在的规矩。还没到点的定时
    变更只躺在 scheduled_changes 里、没发布的稿只躺在 drafts 里，本来就没写进
    这些表，自然不进预演。
    """
    flags = {}
    rows = db.execute("SELECT * FROM flags").fetchall()
    name_of_id = {r["id"]: r["name"] for r in rows}
    for r in rows:
        dep = ""
        if r["depends_on_flag_id"] is not None:
            dep = name_of_id.get(r["depends_on_flag_id"], "")
        flags[r["name"]] = {
            "default_enabled": bool(r["default_enabled"]),
            "rollout_percent": r["rollout_percent"],
            "rollout_condition": r["rollout_condition"],
            "rollout_rules": r["rollout_rules"],
            "variants": r["variants"],
            "kill_switch": bool(r["kill_switch"]),
            "targeting": r["targeting_rule"],
            "config": r["flag_config"],
            "depends_on": dep,
        }
    overrides = {}
    for r in db.execute(
            "SELECT f.name AS fname, o.enabled FROM overrides o"
            " JOIN flags f ON f.id=o.flag_id WHERE o.identity=?", (identity,)):
        overrides[(r["fname"], identity)] = bool(r["enabled"])
    frozen = {}
    for r in db.execute(
            "SELECT f.name AS fname, z.frozen_enabled, z.frozen_config,"
            " z.frozen_variant FROM freezes z"
            " JOIN flags f ON f.id=z.flag_id WHERE z.identity=?", (identity,)):
        frozen[(r["fname"], identity)] = {
            "enabled": bool(r["frozen_enabled"]),
            "config": r["frozen_config"],
            "variant": r["frozen_variant"],
        }
    groups = {}
    for r in db.execute(
            "SELECT g.name AS gname, f.name AS fname FROM group_members m"
            " JOIN mutex_groups g ON g.id=m.group_id"
            " JOIN flags f ON f.id=m.flag_id"):
        groups.setdefault(r["gname"], set()).add(r["fname"])
    assignments = {}
    for r in db.execute(
            "SELECT g.name AS gname, f.name AS fname FROM group_assignments a"
            " JOIN mutex_groups g ON g.id=a.group_id"
            " JOIN flags f ON f.id=a.flag_id WHERE a.identity=?", (identity,)):
        assignments[r["gname"]] = r["fname"]
    # 预演只改开关配置、不改对照组名单：此人此刻在不在对照组里，current 与
    # preview 同一口径（evaluate_at 看 state["control"]）。
    control = in_control_group(db, identity)
    return {"flags": flags, "overrides": overrides, "frozen": frozen,
            "groups": groups, "assignments": assignments,
            "control": control}


def evaluate_state_bundle(state, identity, attrs=None):
    """在内存状态上求全部开关（按名字序，与整包同口径），开着的带上配置。

    纯函数：组内落定只在内存里模拟（与 evaluate_at 的历史重放同一套口径），
    不写库——预演不会留下任何落定记录。
    """
    results = {}
    memo = {}
    for name in sorted(state["flags"]):
        enabled, reason, config, variant = evaluate_at(
            state, name, identity, attrs, memo)
        item = {"enabled": enabled, "reason": reason}
        if config is not None:
            item["config"] = config
        if variant is not None:
            item["variant"] = variant
        results[name] = item
    return results


def apply_patch_to_state(state, flag_name, patch):
    """把一段规范化后的改动（normalize_patch 口径）套到内存状态里的开关上。

    只动内存、不落库。依赖指向不存在的开关时按解除处理（与历史重放、线上
    「被依赖开关删除自动解除」同一口径）。
    """
    flag = state["flags"].get(flag_name)
    if flag is None:
        return
    if "kill_switch" in patch:
        flag["kill_switch"] = bool(patch["kill_switch"])
    if "default_enabled" in patch:
        flag["default_enabled"] = bool(patch["default_enabled"])
    if "rollout_percent" in patch:
        flag["rollout_percent"] = int(patch["rollout_percent"])
    if "rollout_condition" in patch:
        flag["rollout_condition"] = patch["rollout_condition"]
    if "rollout_rules" in patch:
        flag["rollout_rules"] = patch["rollout_rules"]
    if "variants" in patch:
        flag["variants"] = patch["variants"]
    if "targeting" in patch:
        flag["targeting"] = patch["targeting"]
    if "config" in patch:
        flag["config"] = patch["config"]
    if "depends_on" in patch:
        dep = patch["depends_on"] or ""
        flag["depends_on"] = dep if dep in state["flags"] else ""


@app.post("/api/preview")
@require_admin
def preview():
    """管理端预演：改前先看一眼——真的开关、稿、整包版本、组落定一概不动。

    POST /api/preview
      {
        "identities": ["u1", "u2", …],            // 点名要看的人（必填，≤100 个）
        "attrs": {"plan": "pro"},                  // 可选：假设带这身属性来问
        "flag": "new-checkout", "changes": {…},    // 情形一：要是现在就改完
        "draft_id": 3                              // 情形二：要是现在就把这稿发出去
      }
      （flag+changes 与 draft_id 二选一）

    对每个人返回两份全量结果：current（现在每个开关开没开）与 preview（改完 /
    发布后会变成什么样），开着的开关带配置；changed 列出结果会变（开/关、理由
    或配置不同）的开关名。预演全程只读：不落库、不进审计与历史流水、不产生
    失效记录；全关 / 冻结 / 依赖按现在的规矩算，还没到点的定时变更与别的没
    发布的稿不算进去。稿若现在发不出去（目标开关没了 / 合并成环），
    publishable=false 并带原因，preview 与 current 相同（发不出去=什么都不会变）。
    """
    body = request.get_json(force=True, silent=True) or {}
    identities = body.get("identities")
    if not isinstance(identities, list) or not identities:
        return jsonify({"error": "identities is required"
                                 " (non-empty list of strings)"}), 400
    if len(identities) > 100:
        return jsonify({"error": "at most 100 identities per preview"}), 400
    for ident in identities:
        if not isinstance(ident, str) or ident == "":
            return jsonify({"error": "identities must be non-empty strings"}), 400
    attrs = body.get("attrs")
    if attrs is not None:
        try:
            validate_attrs(attrs)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if not attrs:
            attrs = None

    has_changes = body.get("flag") is not None or body.get("changes") is not None
    has_draft = body.get("draft_id") is not None
    if has_changes == has_draft:
        return jsonify({"error": "give exactly one of (flag + changes) or draft_id"}), 400

    db = get_db()
    publish_error = None
    if has_changes:
        flag_name = body.get("flag")
        changes_body = body.get("changes")
        if not isinstance(flag_name, str) or not flag_name:
            return jsonify({"error": "flag is required with changes"}), 400
        if not isinstance(changes_body, dict) or not changes_body:
            return jsonify({"error": "changes is required (non-empty object)"}), 400
        if changes_body.get("effective_at") is not None:
            return jsonify({"error": "preview applies the change as of now;"
                                     " effective_at is not supported"}), 400
        flag = db.execute("SELECT * FROM flags WHERE name=?", (flag_name,)).fetchone()
        if flag is None:
            return jsonify({"error": "flag not found"}), 404
        try:
            patch = normalize_patch(db, changes_body)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if not patch:
            return jsonify({"error": "nothing to preview (no kill_switch"
                                     "/default_enabled/rollout_percent"
                                     "/rollout_condition/rollout_rules/variants"
                                     "/description"
                                     "/targeting/depends_on/config given)"}), 400
        # 与立即生效 PATCH 同一口径：依赖目标要存在、自依赖与成环直接拒
        if "depends_on" in patch:
            _, dep_err = validate_depends_on(db, flag, patch["depends_on"])
            if dep_err:
                return jsonify({"error": dep_err}), 400
        staged = {flag_name: patch}
        mode = {"mode": "changes", "flag": flag_name,
                "changes": draft_change_to_dict(patch)}
    else:
        draft_id = body.get("draft_id")
        if isinstance(draft_id, bool) or not isinstance(draft_id, int):
            return jsonify({"error": "draft_id must be an integer"}), 400
        draft, err = get_open_draft(db, draft_id)
        if err:
            return err
        staged = {r["flag_name"]: json.loads(r["changes"])
                  for r in db.execute(
                      "SELECT flag_name, changes FROM draft_changes WHERE draft_id=?",
                      (draft_id,))}
        if not staged:
            return jsonify({"error": "draft is empty"}), 400
        # 与发布同一套校验：发不出去的稿，预演就是「什么都不会变」
        missing = [n for n in staged if db.execute(
            "SELECT 1 FROM flags WHERE name=?", (n,)).fetchone() is None]
        if missing:
            publish_error = ("draft targets flags that no longer exist"
                             f" ({', '.join(missing)})")
        else:
            publish_error = merged_dependency_cycle(db, staged)
        mode = {"mode": "draft", "draft_id": draft_id,
                "publishable": publish_error is None}
        if publish_error is not None:
            mode["error"] = publish_error

    people = {}
    for ident in identities:
        # 收在一起的身份按同一个人算：预演也归到主身份，别名之间结果一致
        person = canonical_identity(db, ident)
        current = evaluate_state_bundle(snapshot_current_state(db, person),
                                        person, attrs)
        if publish_error is not None:
            after = current
        else:
            state = snapshot_current_state(db, person)
            for fname, pch in staged.items():
                apply_patch_to_state(state, fname, pch)
            after = evaluate_state_bundle(state, person, attrs)
        changed = [n for n in current
                   if current[n]["enabled"] != after[n]["enabled"]
                   or current[n]["reason"] != after[n]["reason"]
                   or current[n].get("config") != after[n].get("config")
                   or current[n].get("variant") != after[n].get("variant")]
        people[ident] = {"current": current, "preview": after, "changed": changed}

    out = dict(mode)
    out["identities"] = people
    if attrs is not None:
        out["attrs"] = attrs
    return jsonify(out)


# ---------------------------------------------------------------- mutex groups

def group_to_dict(db, row):
    members = db.execute(
        "SELECT f.name FROM group_members m JOIN flags f ON f.id = m.flag_id"
        " WHERE m.group_id=? ORDER BY f.name", (row["id"],),
    ).fetchall()
    return {
        "name": row["name"],
        "description": row["description"],
        "flags": [r["name"] for r in members],
        "updated_by": row["updated_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def touch_group(db, group_id):
    db.execute("UPDATE mutex_groups SET updated_by=?, updated_at=? WHERE id=?",
               (actor(), time.time(), group_id))


@app.get("/api/groups")
@require_admin
def list_groups():
    db = get_db()
    rows = db.execute("SELECT * FROM mutex_groups ORDER BY name").fetchall()
    return jsonify([group_to_dict(db, r) for r in rows])


@app.post("/api/groups")
@require_admin
def create_group():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not all(c.isalnum() or c in "-_." for c in name):
        return jsonify({"error": "name may only contain letters, digits, - _ ."}), 400
    db = get_db()
    if db.execute("SELECT 1 FROM mutex_groups WHERE name=?", (name,)).fetchone():
        return jsonify({"error": "group already exists"}), 409
    now = time.time()
    db.execute(
        "INSERT INTO mutex_groups (name, description, updated_by, created_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        (name, body.get("description", ""), actor(), now, now),
    )
    audit(actor(), name, "group", "create_group")
    record_history(db, now, "group_upsert", subject=name, payload={},
                   actor_name=actor())
    db.commit()
    record_invalidations(db, actor(), f"create_group {name}")
    return jsonify({"ok": True}), 201


@app.delete("/api/groups/<name>")
@require_admin
def delete_group(name):
    db = get_db()
    cur = db.execute("DELETE FROM mutex_groups WHERE name=?", (name,))
    if cur.rowcount == 0:
        return jsonify({"error": "group not found"}), 404
    # 成员关系与落定记录随组级联删除，组内开关恢复按原规则求值
    audit(actor(), name, "group", "delete_group")
    record_history(db, time.time(), "group_delete", subject=name, payload={},
                   actor_name=actor())
    db.commit()
    record_invalidations(db, actor(), f"delete_group {name}")
    return jsonify({"ok": True})


@app.put("/api/groups/<name>/flags")
@require_admin
def add_flag_to_group(name):
    body = request.get_json(force=True, silent=True) or {}
    flag_name = body.get("flag")
    if not isinstance(flag_name, str) or not flag_name:
        return jsonify({"error": "flag is required"}), 400
    db = get_db()
    group = db.execute("SELECT * FROM mutex_groups WHERE name=?", (name,)).fetchone()
    if group is None:
        return jsonify({"error": "group not found"}), 404
    flag = db.execute("SELECT * FROM flags WHERE name=?", (flag_name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    existing = db.execute(
        "SELECT g.name FROM group_members m JOIN mutex_groups g ON g.id = m.group_id"
        " WHERE m.flag_id=?", (flag["id"],),
    ).fetchone()
    if existing is not None:
        return jsonify({"error": f"flag already in group '{existing['name']}'"
                                 " (a flag can join at most one group)"}), 409
    db.execute("INSERT INTO group_members (group_id, flag_id) VALUES (?,?)",
               (group["id"], flag["id"]))
    touch_group(db, group["id"])
    audit(actor(), name, "group", "add_flag", f"flag={flag_name}")
    record_history(db, time.time(), "member_add", subject=name,
                   payload={"flag": flag_name}, actor_name=actor())
    db.commit()
    record_invalidations(db, actor(), f"add_flag_to_group {name} flag={flag_name}")
    return jsonify({"ok": True})


@app.delete("/api/groups/<name>/flags")
@require_admin
def remove_flag_from_group(name):
    body = request.get_json(force=True, silent=True) or {}
    flag_name = body.get("flag")
    if not isinstance(flag_name, str) or not flag_name:
        return jsonify({"error": "flag is required"}), 400
    db = get_db()
    group = db.execute("SELECT * FROM mutex_groups WHERE name=?", (name,)).fetchone()
    if group is None:
        return jsonify({"error": "group not found"}), 404
    flag = db.execute("SELECT * FROM flags WHERE name=?", (flag_name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    cur = db.execute("DELETE FROM group_members WHERE group_id=? AND flag_id=?",
                     (group["id"], flag["id"]))
    if cur.rowcount == 0:
        return jsonify({"error": "flag not in group"}), 404
    # 释放该开关在组内占有的落定记录，相关身份之后可重新落定组内其他开关
    db.execute("DELETE FROM group_assignments WHERE group_id=? AND flag_id=?",
               (group["id"], flag["id"]))
    touch_group(db, group["id"])
    audit(actor(), name, "group", "remove_flag", f"flag={flag_name}")
    record_history(db, time.time(), "member_remove", subject=name,
                   payload={"flag": flag_name}, actor_name=actor())
    db.commit()
    record_invalidations(db, actor(), f"remove_flag_from_group {name} flag={flag_name}")
    return jsonify({"ok": True})


@app.get("/api/audit")
@require_admin
def list_audit():
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = get_db().execute(
        "SELECT actor, flag_name, layer, action, detail, created_at"
        " FROM audit_log ORDER BY id DESC LIMIT ?", (limit,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/bundles")
@require_admin
def list_bundles():
    """管理端：已发出的整包清单，按 (身份, 属性) 逐行列出；
    stale=true 表示配置已变、此人带这身属性还没来拿新包。"""
    db = get_db()
    rows = db.execute(
        "SELECT identity, attrs_hash, attrs_json, version, content_hash,"
        " generation, updated_at FROM bundles"
        " ORDER BY updated_at DESC LIMIT 500"
    ).fetchall()
    return jsonify([
        {"identity": r["identity"],
         "attrs_hash": r["attrs_hash"],
         "attrs": json.loads(r["attrs_json"]) if r["attrs_json"] else {},
         "version": r["version"],
         "stale": make_version(r["content_hash"], r["generation"]) != r["version"],
         "updated_at": r["updated_at"]}
        for r in rows
    ])


@app.get("/api/bundles/invalidations")
@require_admin
def list_bundle_invalidations():
    """管理端：整包失效记录——谁改了什么、让哪些人哪身属性的整包过期了。"""
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = get_db().execute(
        "SELECT actor, change, identity, attrs_hash, old_version, new_version, created_at"
        " FROM bundle_invalidations ORDER BY id DESC LIMIT ?", (limit,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------- 身份合并（同一个人）

@app.get("/api/identities/merges")
@require_admin
def list_identity_merges():
    """管理端：列出每拨「收成同一个人」的身份。主身份排在首位。"""
    return jsonify(list_identity_groups(get_db()))


@app.post("/api/identities/merges")
@require_admin
def merge_identities():
    """把几个身份收成同一个人。

    body: {"identities": ["a", "b", "c"]}

    - 至少要写两个身份；少写了、写成非字符串列表、有重复，这次收不成（400）；
    - 这几个身份里只要有一个**已经在另一拨人里**，这次一个都不收（409），
      并在错误里说清是哪些身份、分别在哪一拨。想改谁跟谁是同一个人，先拆开
      （DELETE）再重新收；整拨原封不动再收一次是幂等的 no-op；
    - 收完之后，用其中任何一个身份来问（单查 / 整包 / 历史 / 预演），每个开关
      的开/关都按同一个主身份算，结果一字不差；管理端再给这些身份下强制 /
      冻结也落在同一个人身上。
    合并把这几个身份名下既有的单人强制 / 冻结 / 组落定 / 整包账本一起迁到主
    身份（同一开关 / 同一组冲突时主身份优先，否则取成员名字序最小者），随后
    统一重算已发整包、换新版本。
    """
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "body must be a JSON object"
                                 ' ({"identities": [...]})'}), 400
    raw = body.get("identities")
    if not isinstance(raw, list):
        return jsonify({"error": "identities is required"
                                 " (a list of at least two identities)"}), 400
    if len(raw) < 2:
        return jsonify({"error": "at least two identities are required"
                                 " to merge into one person"}), 400
    for ident in raw:
        if not isinstance(ident, str) or ident == "":
            return jsonify({"error": "identities must be non-empty strings"}), 400
    if len(set(raw)) != len(raw):
        dup = sorted({x for x in raw if raw.count(x) > 1})
        return jsonify({"error": f"identities must be unique"
                                 f" (duplicated: {', '.join(dup)})"}), 400

    db = get_db()
    idents = list(dict.fromkeys(raw))  # 保序去重（前面已拦重复，这里仅兜底）

    # 每个身份各自所属的拨（group_id -> 成员列表，按主身份在前的次序）
    grp_of_ident = {}
    memberships = {}
    for ident in idents:
        grp = identity_group_of(db, ident)
        if grp is not None:
            grp_of_ident[ident] = grp[0]
            memberships[grp[0]] = grp[1]

    if len(memberships) > 1:
        # 横跨两拨或更多：不允许把已有的几拨整拨合并，得先拆开再重新收
        detail = "；".join(
            f"{ident} 已在另一拨人里 [{'、'.join(memberships[grp_of_ident[ident]])}]"
            for ident in idents if ident in grp_of_ident)
        return jsonify({"error": "one or more identities already belong to another"
                                 " person group; split them first, nothing was"
                                 f" merged：{detail}"}), 409

    if len(memberships) == 1:
        existing_gid, existing_members = next(iter(memberships.items()))
        if set(existing_members) == set(idents):
            # 整拨原封不动再收一次：幂等 no-op，什么都不迁、不换版本
            return jsonify({"ok": True, "changed": False, "id": existing_gid,
                            "primary": existing_members[0],
                            "identities": existing_members})
        # 这一拨里有人，但请求还混进了拨外的新身份——同样收不成（拨不扩编，
        # 想换人先拆开）
        conflict = [i for i in idents if i in set(existing_members)]
        return jsonify({"error": "one or more identities are already merged in"
                                 " another person group"
                                 f" [{'、'.join(existing_members)}]; split that"
                                 " group first if you want to change who is the"
                                 f" same person（已在拨内：{'、'.join(conflict)}）"}), 409

    # 全新的一拨：主身份取成员里名字序最小者（与请求书写顺序无关，确定且稳定）
    primary = min(idents)
    now = time.time()
    cur = db.execute(
        "INSERT INTO identity_groups (created_by, created_at) VALUES (?,?)",
        (actor(), now))
    gid = cur.lastrowid
    for ident in sorted(idents):
        db.execute(
            "INSERT INTO identity_members (group_id, identity, is_primary)"
            " VALUES (?,?,?)",
            (gid, ident, 1 if ident == primary else 0))

    rekeyed = _rekey_person_state(db, idents, primary)

    audit(actor(), primary, "identity", "merge_identities",
          "identities=" + ",".join(sorted(idents)))
    record_history(db, now, "identity_merge", subject=primary,
                   payload={"identities": sorted(idents), "primary": primary},
                   actor_name=actor())
    # 对照组归属随人迁到主身份：把「合并这一刻起对照归属按主身份算」补进流水，
    # 历史重放合并期时拨内任一身份才能还原同一个对照归属（与强制 / 冻结迁移
    # 同口径；主身份本就在对照组里时无需重复加）。
    ctrl_members = [i for i in idents
                    if db.execute("SELECT 1 FROM control_group WHERE identity=?",
                                  (i,)).fetchone()]
    if ctrl_members:
        if not db.execute(
                "SELECT 1 FROM control_group WHERE identity=?",
                (primary,)).fetchone():
            record_history(db, now, "control_add", identity=primary,
                           payload={}, actor_name=actor())
        for ident in idents:
            if ident != primary:
                record_history(db, now, "control_remove", identity=ident,
                               payload={}, actor_name=actor())
    db.commit()

    # 统一重算已发整包：合并是求值输入（身份→主身份）的变化，相关的人都要换
    # 新版本；随后给从别名迁到主身份名下的旧包补一条失效可见记录。
    record_invalidations(db, actor(),
                         "merge_identities " + ",".join(sorted(idents)))
    _record_rekeyed_invalidations(db, rekeyed, actor(),
                                  "merge_identities " + ",".join(sorted(idents)),
                                  primary)
    db.commit()
    return jsonify({"ok": True, "changed": True, "id": gid, "primary": primary,
                    "identities": sorted(idents)}), 201


def _record_rekeyed_invalidations(db, rekeyed, actor_name, change, primary):
    """合并把别名整包行迁到主身份后，给被迁走的旧 (身份, 属性) 补一条失效记录：
    旧版本 -> 主身份此刻的新版本（让「谁的包因这次合并失效」在失效记录里可见）。"""
    if not rekeyed:
        return
    now = time.time()
    for old_identity, attrs_hash in rekeyed:
        new_row = db.execute(
            "SELECT version FROM bundles WHERE identity=? AND attrs_hash=?",
            (primary, attrs_hash)).fetchone()
        if new_row is None:
            continue
        db.execute(
            "INSERT INTO bundle_invalidations"
            " (actor, change, identity, attrs_hash, old_version, new_version,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (actor_name, change, old_identity, attrs_hash,
             "(rekeyed into person)", new_row["version"], now))


@app.delete("/api/identities/merges/<int:group_id>")
@require_admin
def split_identities(group_id):
    """拆开一拨人：拆开后这几个身份各算各的（按人存的状态不再共享）。

    拆开不迁移、不删除任何状态：之前合并迁到主身份名下的强制 / 冻结 / 落定 /
    整包仍挂在主身份身上，其余身份回到「自己名下没有这些状态」的独立状态——
    与「拆开以后各算各的」一致。对照组归属同理：行留在主身份名下，但要给每个
    旧别名补一条 control_remove 流水，历史重放拆开后这些别名不再算对照组里的
    人。整包随后统一重算、换新版本。
    """
    db = get_db()
    row = db.execute("SELECT * FROM identity_groups WHERE id=?",
                     (group_id,)).fetchone()
    if row is None:
        return jsonify({"error": "identity group not found"}), 404
    members = [r["identity"] for r in db.execute(
        "SELECT identity FROM identity_members WHERE group_id=?"
        " ORDER BY is_primary DESC, identity", (group_id,))]
    now = time.time()
    db.execute("DELETE FROM identity_groups WHERE id=?", (group_id,))
    audit(actor(), members[0] if members else "", "identity",
          "split_identities", "identities=" + ",".join(members))
    record_history(db, now, "identity_split",
                   subject=members[0] if members else "",
                   payload={"identities": members}, actor_name=actor())
    # 主身份在对照组里时，旧别名从这一刻起不再随主身份算对照（行本身留在
    # 主身份名下，与强制 / 冻结迁移后留在主身份名下同一口径）。
    primary = members[0] if members else ""
    if primary and db.execute(
            "SELECT 1 FROM control_group WHERE identity=?",
            (primary,)).fetchone():
        for ident in members[1:]:
            record_history(db, now, "control_remove", identity=ident,
                           payload={}, actor_name=actor())
    db.commit()
    record_invalidations(db, actor(),
                         "split_identities " + ",".join(members))
    return jsonify({"ok": True, "changed": True, "identities": members})


# ---------------------------------------------------------------- 对照组（control group）

@app.get("/api/control-group")
@require_admin
def get_control_group():
    """管理端：查看对照组现在点着哪些人（主身份，名字序）。"""
    db = get_db()
    rows = db.execute(
        "SELECT identity, created_by, created_at, updated_at FROM control_group"
        " ORDER BY identity").fetchall()
    return jsonify({"identities": [r["identity"] for r in rows],
                    "members": [{"identity": r["identity"],
                                 "created_by": r["created_by"],
                                 "created_at": r["created_at"],
                                 "updated_at": r["updated_at"]} for r in rows]})


def _parse_roster(raw, min_count):
    """校验「一拨人」名单：非空字符串列表、至少 min_count 个、不能重复。

    与身份合并同一口径：少写了 / 不是列表 / 成员不是非空字符串 / 有重复，
    这次都点不成——ValueError 文案直接说清楚，调用方转 400，一个人都不写库。
    返回去重保序后的名单。"""
    if not isinstance(raw, list):
        raise ValueError("identities is required (a list of non-empty identity"
                         " strings)")
    if len(raw) < min_count:
        raise ValueError(f"at least {min_count} identit{'y' if min_count == 1 else 'ies'}"
                         f" {'is' if min_count == 1 else 'are'} required")
    for ident in raw:
        if not isinstance(ident, str) or ident == "":
            raise ValueError("identities must be non-empty strings")
    if len(set(raw)) != len(raw):
        dup = sorted({x for x in raw if raw.count(x) > 1})
        raise ValueError(f"identities must be unique (duplicated: {', '.join(dup)})")
    return list(dict.fromkeys(raw))


@app.put("/api/control-group")
@require_admin
def replace_control_group():
    """点名一拨人进对照组（整拨替换为新名单）。

    body: {"identities": ["u1", "u2", …]}，至少一个；少写了 / 不是非空字符串
    列表 / 有重复，这次点不成（400）并说清楚，一个人都不写库。

    进了对照组的人来问不再按人分开算：冻结 / 依赖 / 强制 / 属性 / 组 / 放量 /
    定档一概不看，每个开关只走它自己的默认开或关（reason=control），全关仍
    压过；没进的人照旧走各开关原来的算法。身份按主身份记名（与强制 / 冻结同
    口径）；同一份名单再点一次是幂等 no-op，什么都不换。改了谁在对照组里
    （加入 / 移出）立即生效，相关已发整包统一重算、换新版本。
    """
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "body must be a JSON object"
                                 ' ({"identities": [...]})'}), 400
    try:
        idents = _parse_roster(body.get("identities"), 1)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    db = get_db()
    # 归主身份：写进合并拨里某个别名 = 点的是这拨人（与单人强制 / 冻结同口径）。
    # 归并后撞到同一个主身份也算重复，这次点不成并说清楚。
    people = []
    for ident in idents:
        person = canonical_identity(db, ident)
        if person in people:
            return jsonify({"error": "identities must be unique after merging"
                                     f" aliases ('{ident}' is the same person as"
                                     f" '{people[-1]}' via identity merge)"}), 400
        people.append(person)

    current = list_control_group(db)
    if set(current) == set(people):
        # 同一拨人再点一次（主身份集合一致，书写顺序无关）：幂等 no-op
        return jsonify({"ok": True, "changed": False, "identities": current})

    now = time.time()
    cur_people = set(current)
    new_people = set(people)
    added = sorted(new_people - cur_people)
    removed = sorted(cur_people - new_people)
    # 整拨替换在一个事务里：删掉不在新名单里的，插入新进来的（老行的
    # created_at / created_by 保留，updated_at 更新）
    db.execute("DELETE FROM control_group WHERE identity NOT IN"
               f" ({','.join('?' * len(people))})", people)
    for person in people:
        db.execute(
            "INSERT INTO control_group (identity, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(identity) DO UPDATE SET updated_at=excluded.updated_at",
            (person, actor(), now, now))
    audit(actor(), "", "control", "replace_control_group",
          "identities=" + ",".join(people)
          + (f" added={','.join(added)}" if added else "")
          + (f" removed={','.join(removed)}" if removed else ""))
    # 历史重放按增删事件还原「那一刻谁在对照组里」
    for person in added:
        record_history(db, now, "control_add", subject="", identity=person,
                       payload={}, actor_name=actor())
    for person in removed:
        record_history(db, now, "control_remove", subject="", identity=person,
                       payload={}, actor_name=actor())
    db.commit()
    record_invalidations(db, actor(),
                         "replace_control_group"
                         + (f" added={','.join(added)}" if added else "")
                         + (f" removed={','.join(removed)}" if removed else ""))
    db.commit()
    return jsonify({"ok": True, "changed": True, "identities": people,
                    "added": added, "removed": removed})


@app.delete("/api/control-group")
@require_admin
def remove_from_control_group():
    """把人移出对照组；不带 body 或 identities 为空 = 清空整拨。

    移出后该人立即按各开关原来的算法算，相关已发整包换新版本。带 body
    {"identities": [...]} 时只移点名的人（成员必须是非空字符串；写了重复
    这次移不成并说清楚），没在对照组里的点出来但不报错；一个都没移掉是
    no-op（changed=false）。
    """
    body = request.get_json(force=True, silent=True)
    idents = None
    if body is not None:
        if not isinstance(body, dict):
            return jsonify({"error": "body must be a JSON object"
                                     ' ({"identities": [...]}) or empty'}), 400
        if "identities" in body and body["identities"] is not None:
            try:
                idents = _parse_roster(body["identities"], 1)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

    db = get_db()
    current = set(list_control_group(db))
    if idents is None:
        removed = sorted(current)
    else:
        people = []
        for ident in idents:
            people.append(canonical_identity(db, ident))
        if len(set(people)) != len(people):
            dup = sorted({x for x in people if people.count(x) > 1})
            return jsonify({"error": "identities must be unique after merging"
                                     f" aliases (duplicated: {', '.join(dup)})"}), 400
        removed = sorted(set(people) & current)

    if not removed:
        return jsonify({"ok": True, "changed": False, "removed": []})
    now = time.time()
    db.execute(f"DELETE FROM control_group WHERE identity IN"
               f" ({','.join('?' * len(removed))})", removed)
    audit(actor(), "", "control", "remove_control_group",
          "identities=" + ",".join(removed))
    for person in removed:
        record_history(db, now, "control_remove", subject="", identity=person,
                       payload={}, actor_name=actor())
    db.commit()
    record_invalidations(db, actor(),
                         "remove_control_group " + ",".join(removed))
    db.commit()
    return jsonify({"ok": True, "changed": True, "removed": removed})


# ---------------------------------------------------------------- admin page

@app.get("/")
def admin_page():
    return render_template("index.html")


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
