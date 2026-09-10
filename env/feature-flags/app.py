"""特性开关服务：管理端 + 调用方查询接口。

求值优先级（固定，不可配置）：
    1. 全关（kill switch）        -> 一律关
    2. 结果冻结（freeze）          -> 此人对本开关冻住的那一刻的结果，与之后的一切配置改动无关
    3. 开关依赖（depends_on）     -> 依赖的开关对此人此身属性不是开，本开关必须关
    4. 单人强制（override）        -> 强制开 / 强制关
    5. 属性打开条件（targeting）   -> 来问带的属性全对上则开；对不上落到下面
    6. 互斥组（group）            -> 组内同一身份最多一个开
    7. 比例放量（rollout_percent） -> 比例 > 0 时此层定论：命中开、未命中关
    8. 默认值（default_enabled）   -> 仅当放量比例为 0（未启用放量）时兜底

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
"""

import hashlib
import json
import math
import os
import sqlite3
import time
from functools import wraps

from flask import Flask, g, jsonify, render_template, request

DB_PATH = os.environ.get("FLAG_DB", "/data/flags.db")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin-token")

app = Flask(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS flags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL DEFAULT '',
    default_enabled INTEGER NOT NULL DEFAULT 0,
    rollout_percent INTEGER NOT NULL DEFAULT 0,
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
"""

LAYERS = ("kill_switch", "freeze", "depends_on", "override", "targeting",
          "group", "rollout", "default")


# ---------------------------------------------------------------- attrs / targeting

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
    if not isinstance(attrs, dict):
        raise ValueError("attrs must be a JSON object")
    for k, v in attrs.items():
        if not isinstance(k, str) or k == "":
            raise ValueError("attr keys must be non-empty strings")
        if isinstance(v, bool) or v is None or isinstance(v, (str, int, float)):
            continue
        raise ValueError(f"attr '{k}' must be a scalar (string/number/bool/null)")
    return attrs


def scalar_equal(a, b):
    """属性值精确相等：bool 与 number 不互等（True ≠ 1），其余按 JSON 语义。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def validate_targeting(rule):
    """校验管理端定的属性打开条件，返回规范化 JSON 串。

    形式：{键: 标量} 或 {键: [标量, …]}；多个键之间是 AND，
    一个键给多个值时是 OR。None / {} 表示清除条件。非法抛 ValueError。
    """
    if rule is None:
        return ""
    if not isinstance(rule, dict):
        raise ValueError("targeting must be a JSON object"
                         " (use {} or null to clear)")
    if not rule:
        return ""  # 空对象 = 清除条件

    def check_scalar(v, where):
        if isinstance(v, bool) or v is None or isinstance(v, (str, int, float)):
            return
        raise ValueError(f"targeting value at {where} must be a scalar"
                         " (string/number/bool/null)")

    norm = {}
    for k, v in rule.items():
        if not isinstance(k, str) or k == "":
            raise ValueError("targeting keys must be non-empty strings")
        if isinstance(v, list):
            if not v:
                raise ValueError(f"targeting list for '{k}' must not be empty")
            for item in v:
                check_scalar(item, f"'{k}'")
            norm[k] = v
        else:
            check_scalar(v, f"'{k}'")
            norm[k] = v
    return canonical_json(norm)


def targeting_matches(rule_json, attrs):
    """属性对没对上条件：条件每个键都在属性里且值相等（列表值任一即可）。"""
    if not rule_json or not attrs:
        return False
    rule = json.loads(rule_json)
    for k, wanted in rule.items():
        if k not in attrs:
            return False
        got = attrs[k]
        values = wanted if isinstance(wanted, list) else [wanted]
        if not any(scalar_equal(got, w) for w in values):
            return False
    return True


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


# ---------------------------------------------------------------- db helpers

def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
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
                                     "config": r["frozen_config"]}),
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


def audit(actor, flag_name, layer, action, detail=""):
    get_db().execute(
        "INSERT INTO audit_log (actor, flag_name, layer, action, detail, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (actor, flag_name, layer, action, detail, time.time()),
    )


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
        "kill_switch": bool(row["kill_switch"]),
        "targeting": json.loads(row["targeting_rule"]) if row["targeting_rule"] else {},
        "config": parse_flag_config(row["flag_config"]),
        "depends_on": dep_name if dep_name is not None else "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------- evaluation

def bucket_of(flag_name, identity):
    """确定性分桶：同一 (flag, identity) 永远得到 0-99 中同一个数。"""
    digest = hashlib.sha256(f"{flag_name}:{identity}".encode("utf-8")).hexdigest()
    return int(digest, 16) % 100


def group_of(db, flag_id):
    """开关所在的互斥组 id；未进组返回 None。"""
    row = db.execute(
        "SELECT group_id FROM group_members WHERE flag_id=?", (flag_id,),
    ).fetchone()
    return row["group_id"] if row else None


def evaluate(db, flag, identity, attrs=None, _memo=None, _chain=None,
             _skip_freeze_id=None):
    """按固定优先级求值，返回 (enabled, reason, config)。

    config 是「此人此刻拿得到的开关配置」：只看最终开/关，与由哪一层决定
    无关——最终为开且开关挂了配置，就带上该配置（对象/数组/标量原样）；
    最终为关（全关、依赖关、强制关、组内没争到、放量未命中、默认关）或开关
    没挂配置，config 一律为 None（接口里不带这个键）。

    attrs 为来问时带的属性（dict）。属性条件命中时这一层直接定论为开；
    对不上 / 没带属性 / 开关没定条件，都按原来的放量 / 默认值算。

    结果冻结在全关之后、开关依赖之前：冻住后直接返回冻住那一刻的结果
    （reason=freeze），不再看依赖、强制、属性、组、放量与默认值；因此
    改依赖也救不回/压不掉冻住的值。冻在开的人同时拿到冻住那一刻挂着的
    配置快照（frozen_config），此后改挂的配置不动他；冻在关则不带。
    被别的开关依赖时，依赖者沿备忘拿到的就是这个冻住的结果。全关仍在更
    前面，冻住也压不过全关（全关一律关、不带配置）。
    _skip_freeze_id 仅供「重新冻住」时顶层使用：按假设此开关没冻的口径
    求它此刻的结果（沿依赖递归时被依赖开关的冻结照常生效）。
    依赖链上的结果在单次求值内备忘，保证链上每个开关只算一次、结果一致；
    成环在管理端写入时已拒绝，运行时再兜一层防环。
    """
    def ret(enabled, reason, config_json=None):
        """统一出口：开且挂了配置才随结果带配置；关一律不带。"""
        cfg = parse_flag_config(config_json) if enabled and config_json else None
        return enabled, reason, cfg

    if flag["kill_switch"]:
        return ret(False, "kill_switch")

    # 结果冻结：只认冻住那一刻存下的开/关（连同配置快照），此后其他层
    # 怎么改都不影响它。
    if _skip_freeze_id != flag["id"]:
        freeze = db.execute(
            "SELECT frozen_enabled, frozen_config FROM freezes"
            " WHERE flag_id=? AND identity=?",
            (flag["id"], identity),
        ).fetchone()
        if freeze is not None:
            enabled = bool(freeze["frozen_enabled"])
            if _memo is not None:
                _memo[flag["id"]] = enabled
            return ret(enabled, "freeze", freeze["frozen_config"])

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
                dep_enabled, _, _ = evaluate(db, dep_flag, identity, attrs,
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

    # 属性打开条件：来问属性全对上则开，且不落到放量 / 默认值
    if targeting_matches(flag["targeting_rule"], attrs):
        natural = True
        reason = "targeting"
    # 放量比例 > 0 时这一层直接定论：命中开、未命中关，不再落到默认值
    elif flag["rollout_percent"] > 0:
        natural = bucket_of(flag["name"], identity) < flag["rollout_percent"]
        reason = "rollout"
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
    冻住的开关对此人只认 freezes 行：它的默认值/放量/属性条件/依赖边/单人强制
    在冻住期间都不参与求值，因此这些改动不进此人的摘要（不换版本），它挂的
    配置也只认冻住那一刻的快照 frozen_config；它的全关仍压过冻结，所以全关
    字段保留，冻住期间开全关仍让包换版本。
    与求值无关的字段（如描述、时间戳）不影响摘要；只与别人相关的改动
    （如给他人的单人强制）也不影响此身份的摘要。
    """
    flags = db.execute(
        "SELECT name, default_enabled, rollout_percent, kill_switch, targeting_rule,"
        " flag_config FROM flags ORDER BY name"
    ).fetchall()
    overrides = db.execute(
        "SELECT f.name, o.enabled FROM overrides o"
        " JOIN flags f ON f.id = o.flag_id WHERE o.identity=? ORDER BY f.name",
        (identity,),
    ).fetchall()
    freezes = db.execute(
        "SELECT f.name, z.frozen_enabled, z.frozen_config FROM freezes z"
        " JOIN flags f ON f.id = z.flag_id WHERE z.identity=? ORDER BY f.name",
        (identity,),
    ).fetchall()
    frozen_names = {r["name"] for r in freezes}
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
        "identity": identity,
        # 冻住的开关：默认值/放量在冻住期间不参与求值，摘要里恒为中性值，
        # 全关仍压过冻结、照常参与，故保留真实值。
        "flags": [[r["name"],
                   False if r["name"] in frozen_names else bool(r["default_enabled"]),
                   0 if r["name"] in frozen_names else r["rollout_percent"],
                   bool(r["kill_switch"])] for r in flags],
        # 冻住的开关不看单人强制，给它的强制不进摘要
        "overrides": [[r["name"], bool(r["enabled"])] for r in overrides
                      if r["name"] not in frozen_names],
        # 冻住的开关：第三个元素是冻住那一刻挂着的配置快照（冻在关时为 ''）
        "freezes": [[r["name"], bool(r["frozen_enabled"]), r["frozen_config"]]
                    for r in freezes],
        "groups": [[r["g"], r["f"]] for r in members],
        "assignments": [[r["g"], r["f"]] for r in assignments],
    }
    # 开关依赖关系是全局求值输入：改了谁依赖谁（含解除、被依赖开关删除）
    # 相关已发整包都要换新版本。冻住的开关不看自己的出边依赖（冻住在依赖层
    # 之前直接定论），所以它自己那条边不进摘要；别人对它的依赖保留——依赖者
    # 求值时会沿备忘拿到它冻住的结果。按依赖者名字排序，保证摘要确定。
    dependencies = db.execute(
        "SELECT f.name AS child, p.name AS parent FROM flags f"
        " JOIN flags p ON p.id = f.depends_on_flag_id ORDER BY f.name"
    ).fetchall()
    payload["dependencies"] = [[r["child"], r["parent"]] for r in dependencies
                               if r["child"] not in frozen_names]
    # 开关挂的配置只在该开关对此人此刻判开时下发，才是此人整包的求值输入：
    # 判开且挂了配置 -> 配置内容进摘要（改配置必换版本）；判关或没挂 -> 不进
    # （改配置不波及他，全关期间所有开关判关，与「全关后不要带」一致）。
    # 冻住的开关配置走 freezes 里的快照，这里不重复计。
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
        payload["attrs"] = attrs
        with_rules = []
        for row, r in zip(payload["flags"], flags):
            if (r["name"] not in frozen_names and r["targeting_rule"]
                    and targeting_matches(r["targeting_rule"], attrs)):
                with_rules.append(row + [json.loads(r["targeting_rule"])])
            else:
                with_rules.append(row)
        payload["flags"] = with_rules
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
        enabled, reason, config = evaluate(db, flag, identity, attrs, memo)
        item = {"enabled": enabled, "reason": reason}
        if config is not None:
            item["config"] = config
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
                 "kill_switch": False, "targeting": "", "config": "",
                 "depends_on": ""}


def _normalize_replay_patch(changes):
    """把预约 / 发布稿里存的字段值归一成重放用的覆盖 patch。

    targeting / config 存的是 canonical 串（与 flags 列同口径，''=清除）；
    布尔与比例还原成重放状态里的类型；depends_on 是名字串（''=解除）。
    """
    patch = {}
    if "kill_switch" in changes:
        patch["kill_switch"] = bool(changes["kill_switch"])
    if "default_enabled" in changes:
        patch["default_enabled"] = bool(changes["default_enabled"])
    if "rollout_percent" in changes:
        patch["rollout_percent"] = int(changes["rollout_percent"])
    if "targeting" in changes:
        patch["targeting"] = changes["targeting"] if changes["targeting"] else ""
    if "config" in changes:
        patch["config"] = changes["config"] if changes["config"] else ""
    if "depends_on" in changes:
        patch["depends_on"] = changes["depends_on"] or ""
    return patch


def replay_state(db, identity, at):
    """重放身份 identity 在时刻 at 的完整求值状态（纯函数，不写库）。

    数据源：
    - history_events：管理端每一次「已生效」变更的 append-only 流水；
    - scheduled_changes：status='applied' 或 pending 但 effective_at<=at，
      且开关已删除时被取消的（cancelled/failed 一律不算）——预约到了点，
      哪怕还没有任何请求惰性触发它，那一刻来问也必须按到点后算；
    - drafts：status='published' 且 published_at<=at 的稿（没发布的稿不算）。

    返回 dict：
      flags:  name -> {default_enabled, rollout_percent, kill_switch,
                       targeting(串), config(串), depends_on(名字串)}
      overrides/frozen: (flag_name, identity) -> ...
      groups: name -> set(成员 flag)；assignments: 组名 -> 落定 flag
    """
    ops = []  # (occurred_at, rank, 次序, 类型, 内容)

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
            flags[name].update(_normalize_replay_patch(changes))
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
            overrides.pop((subject, identity), None)
            frozen.pop((subject, identity), None)
            for gname, members in list(groups.items()):
                members.discard(subject)
                if assignments.get(gname) == subject:
                    assignments.pop(gname, None)
        elif ev_kind == "override" and ev_identity == identity:
            overrides[(subject, identity)] = bool(payload["enabled"])
        elif ev_kind == "override_delete" and ev_identity == identity:
            overrides.pop((subject, identity), None)
        elif ev_kind == "freeze" and ev_identity == identity:
            frozen[(subject, identity)] = {
                "enabled": bool(payload["enabled"]),
                "config": payload.get("config", ""),
            }
        elif ev_kind == "freeze_delete" and ev_identity == identity:
            frozen.pop((subject, identity), None)
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
        elif ev_kind == "assignment" and ev_identity == identity:
            members = groups.get(subject)
            # 落定只在「组还在、该开关当时仍在组内」时有效
            if members and payload["flag"] in members:
                assignments.setdefault(subject, payload["flag"])

    return {"flags": flags, "overrides": overrides, "frozen": frozen,
            "groups": groups, "assignments": assignments}


def evaluate_at(state, name, identity, attrs=None, _memo=None, _chain=None):
    """按固定优先级在重放状态上求一个开关的值，返回 (enabled, reason, config)。

    与线上 evaluate 同一套口径（全关 > 冻结 > 依赖 > 强制 > 属性 > 组 >
    放量 > 默认），区别只是输入来自 replay_state 的内存状态、且组落定不写库：
    那一刻已经落定过（assginments 里有）就按落定算；没落定过就按名字序模拟
    ——与「那一刻整包来问」时 compute_bundle 按 name 序求值、首个自然开的
    组内开关落定的行为一字不差。
    """
    def ret(enabled, reason, config_json=""):
        cfg = parse_flag_config(config_json) if enabled and config_json else None
        return enabled, reason, cfg

    flag = state["flags"].get(name)
    if flag is None:
        return False, "default", None  # 理论不可达：整包只遍历当时存在的开关

    if flag["kill_switch"]:
        return ret(False, "kill_switch")

    fz = state["frozen"].get((name, identity))
    if fz is not None:
        if _memo is not None:
            _memo[name] = fz["enabled"]
        return ret(fz["enabled"], "freeze", fz["config"])

    dep = flag["depends_on"]
    if dep:
        if _chain is not None and dep in _chain:
            raise RuntimeError(f"dependency cycle through {dep}")
        dep_enabled = False
        if _memo is not None and dep in _memo:
            dep_enabled = _memo[dep]
        elif dep in state["flags"]:
            next_chain = {name} if _chain is None else _chain | {name}
            dep_enabled, _, _ = evaluate_at(state, dep, identity, attrs,
                                            _memo, next_chain)
        if not dep_enabled:
            if _memo is not None:
                _memo[name] = False
            return ret(False, "depends_on")

    if (name, identity) in state["overrides"]:
        enabled = state["overrides"][(name, identity)]
        if _memo is not None:
            _memo[name] = enabled
        return ret(enabled, "override", flag["config"])

    if targeting_matches(flag["targeting"], attrs):
        natural, reason = True, "targeting"
    elif flag["rollout_percent"] > 0:
        natural, reason = bucket_of(name, identity) < flag["rollout_percent"], "rollout"
    else:
        natural, reason = bool(flag["default_enabled"]), "default"

    group_name = next((g for g, members in state["groups"].items()
                       if name in members), None)
    if group_name is None or not natural:
        if _memo is not None:
            _memo[name] = natural
        return ret(natural, reason, flag["config"])

    won = state["assignments"].get(group_name) == name
    if _memo is not None:
        _memo[name] = won
    return ret(won, "group", flag["config"])


def compute_history_bundle(db, identity, at, attrs=None):
    """求该身份在时刻 at 所有「当时存在」的开关结果（按名字序，与线上整包同口径）。"""
    state = replay_state(db, identity, at)
    results = {}
    memo = {}
    for name in sorted(state["flags"]):
        enabled, reason, config = evaluate_at(state, name, identity, attrs, memo)
        item = {"enabled": enabled, "reason": reason}
        if config is not None:
            item["config"] = config
        results[name] = item
    return results


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
    results = compute_history_bundle(get_db(), identity, at, attrs)
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
    enabled, reason, config = evaluate(db, flag, identity, attrs)
    body = {
        "flag": name,
        "identity": identity,
        "enabled": enabled,
        "reason": reason,
    }
    # 开才带开关挂的配置（冻住的人拿冻住那一刻那份）；关不带这个键
    if config is not None:
        body["config"] = config
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
    results, content_hash = compute_bundle(db, identity, attrs)
    a_hash = attrs_hash_of(attrs)
    a_json = canonical_json(attrs) if attrs else ""
    row = db.execute(
        "SELECT content_hash, generation FROM bundles"
        " WHERE identity=? AND attrs_hash=?",
        (identity, a_hash),
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
        (identity, a_hash, a_json, version, content_hash, generation,
         json.dumps(results, ensure_ascii=False), time.time()),
    )
    db.commit()
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
        config_json = validate_config(body.get("config"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    now = time.time()
    default_enabled = 1 if body.get("default_enabled") else 0
    cur = db.execute(
        "INSERT INTO flags (name, description, default_enabled, targeting_rule,"
        " flag_config, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (name, body.get("description", ""), default_enabled, targeting_json,
         config_json, now, now),
    )
    audit(actor(), name, "default", "create_flag",
          f"default_enabled={bool(default_enabled)}"
          + (f" targeting={targeting_json}" if targeting_json else "")
          + (f" config={config_json}" if config_json else ""))
    # 历史流水：新建即一条全量快照（不依赖任何开关）
    record_history(
        db, now, "flag_upsert", subject=name, actor_name=actor(),
        payload={"default_enabled": bool(default_enabled), "rollout_percent": 0,
                 "kill_switch": False, "targeting": targeting_json,
                 "config": config_json, "depends_on": ""})
    db.commit()
    record_invalidations(db, actor(), f"create_flag {name}")
    return jsonify({"ok": True}), 201


# 可预约定时生效的开关字段（与 PATCH 立即生效支持的字段一致）
SCHEDULABLE_FIELDS = ("kill_switch", "default_enabled", "rollout_percent",
                      "description", "targeting", "depends_on", "config")


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
    """把 kill_switch / default_enabled / rollout_percent / description /
    targeting / depends_on / config 写到开关上。

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
                                 "/description/depends_on/config given)"}), 400
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
    if "config" in payload:
        try:
            # 预约表里存规范化后的串，到点应用时不再产生歧义
            payload["config"] = json.loads(validate_config(payload["config"]))
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
def _apply_due_scheduled_changes():
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
    enabled = 1 if body["enabled"] else 0
    now = time.time()
    db.execute(
        "INSERT INTO overrides (flag_id, identity, enabled, created_at) VALUES (?,?,?,?)"
        " ON CONFLICT(flag_id, identity) DO UPDATE SET enabled=excluded.enabled",
        (flag["id"], identity, enabled, now),
    )
    audit(actor(), name, "override", "set_override",
          f"identity={identity} enabled={bool(enabled)}")
    record_history(db, now, "override", subject=name, identity=identity,
                   payload={"enabled": bool(enabled)}, actor_name=actor())
    db.commit()
    record_invalidations(db, actor(),
                         f"set_override {name} identity={identity}")
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
    db.execute("DELETE FROM overrides WHERE flag_id=? AND identity=?",
               (flag["id"], identity))
    audit(actor(), name, "override", "remove_override", f"identity={identity}")
    record_history(db, time.time(), "override_delete", subject=name,
                   identity=identity, payload={}, actor_name=actor())
    db.commit()
    record_invalidations(db, actor(),
                         f"remove_override {name} identity={identity}")
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 结果冻结（freeze）

def freeze_result(db, flag, identity):
    """冻住时按当前完整规则求一次「此刻」的结果（不带属性口径），
    返回 (enabled, reason, config)。跳过本开关已有的冻结行——重新冻同一个人时，
    冻住的是「假如现在解冻会算出的结果」；被依赖开关的冻结照常生效。
    config 即此人此刻判开时开关挂的配置（冻住后按这份快照下发），判关为 None。"""
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
        "SELECT identity, frozen_enabled, frozen_reason, frozen_config, created_by, created_at"
        " FROM freezes WHERE flag_id=? ORDER BY identity", (flag["id"],),
    ).fetchall()
    return jsonify([
        {"identity": r["identity"], "enabled": bool(r["frozen_enabled"]),
         "frozen_reason": r["frozen_reason"],
         "config": json.loads(r["frozen_config"]) if r["frozen_config"] else None,
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
    enabled, reason, config = freeze_result(db, flag, identity)
    now = time.time()
    existing = db.execute(
        "SELECT frozen_enabled, frozen_config FROM freezes"
        " WHERE flag_id=? AND identity=?",
        (flag["id"], identity),
    ).fetchone()
    if existing is not None and bool(existing["frozen_enabled"]) == enabled:
        # 冻住的开/关没变就是 no-op：值、理由、配置快照都不刷新（与「冻住后
        # 雷打不动」一致，想拿新配置需先解冻），不产生失效记录。
        return jsonify({"ok": True, "changed": False, "enabled": enabled,
                        "reason": reason})
    # 第一次冻，或重新冻把结果从开冻成关 / 从关冻成开：此刻挂着的配置一并
    # 快照——冻在开带这份（没挂为 ''），冻在关为 ''（关不带配置）。
    config_json = flag["flag_config"] if enabled else ""
    db.execute(
        "INSERT INTO freezes (flag_id, identity, frozen_enabled, frozen_reason,"
        " frozen_config, created_by, created_at) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(flag_id, identity) DO UPDATE SET"
        " frozen_enabled=excluded.frozen_enabled,"
        " frozen_reason=excluded.frozen_reason,"
        " frozen_config=excluded.frozen_config,"
        " created_by=excluded.created_by,"
        " created_at=excluded.created_at",
        (flag["id"], identity, 1 if enabled else 0, reason, config_json,
         actor(), now),
    )
    audit(actor(), name, "freeze", "freeze_result",
          f"identity={identity} enabled={enabled} reason={reason}")
    record_history(db, now, "freeze", subject=name, identity=identity,
                   payload={"enabled": bool(enabled), "reason": reason,
                            "config": config_json},
                   actor_name=actor())
    db.commit()
    record_invalidations(db, actor(),
                         f"freeze_result {name} identity={identity} -> {enabled}")
    body = {"ok": True, "changed": True, "enabled": enabled, "reason": reason}
    if config is not None:
        body["config"] = config
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
    cur = db.execute("DELETE FROM freezes WHERE flag_id=? AND identity=?",
                     (flag["id"], identity))
    if cur.rowcount == 0:
        return jsonify({"ok": True, "changed": False})
    audit(actor(), name, "freeze", "unfreeze_result", f"identity={identity}")
    record_history(db, time.time(), "freeze_delete", subject=name,
                   identity=identity, payload={}, actor_name=actor())
    db.commit()
    record_invalidations(db, actor(),
                         f"unfreeze_result {name} identity={identity}")
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
                "description", "targeting", "depends_on", "config")


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
    """稿内改动存储格式 -> 对外 JSON（布尔还原；targeting/config 的 '' 还原为 None）。"""
    out = dict(ch)
    if "targeting" in out:
        out["targeting"] = json.loads(out["targeting"]) if out["targeting"] else {}
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


def merged_dependency_cycle(db, staged):
    """发布前整稿校验：现网依赖图叠加整稿改动后是否成环。

    staged 为 {flag_name: 合并后的改动 dict}。稿里改了 depends_on 的边以稿
    为准（"" = 解除），其余边沿用现网。返回错误串；None 表示图无环、所有
    被依赖目标都存在。成环 / 自依赖 / 指向不存在的开关都会让整稿不能发布。
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
            return "circular dependency rejected: the draft as a whole would form a cycle"
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
                                 "/description/targeting/depends_on)"}), 400
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


# ---------------------------------------------------------------- admin page

@app.get("/")
def admin_page():
    return render_template("index.html")


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
