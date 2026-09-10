"""特性开关服务：管理端 + 调用方查询接口。

求值优先级（固定，不可配置）：
    1. 全关（kill switch）        -> 一律关
    2. 开关依赖（depends_on）     -> 依赖的开关对此人此身属性不是开，本开关必须关
    3. 单人强制（override）        -> 强制开 / 强制关
    4. 属性打开条件（targeting）   -> 来问带的属性全对上则开；对不上落到下面
    5. 互斥组（group）            -> 组内同一身份最多一个开
    6. 比例放量（rollout_percent） -> 比例 > 0 时此层定论：命中开、未命中关
    7. 默认值（default_enabled）   -> 仅当放量比例为 0（未启用放量）时兜底

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
"""

import hashlib
import json
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
"""

LAYERS = ("kill_switch", "depends_on", "override", "targeting", "group",
          "rollout", "default")


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
    conn.commit()
    conn.close()


def audit(actor, flag_name, layer, action, detail=""):
    get_db().execute(
        "INSERT INTO audit_log (actor, flag_name, layer, action, detail, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (actor, flag_name, layer, action, detail, time.time()),
    )


def flag_to_dict(row, dep_name=None):
    return {
        "name": row["name"],
        "description": row["description"],
        "default_enabled": bool(row["default_enabled"]),
        "rollout_percent": row["rollout_percent"],
        "kill_switch": bool(row["kill_switch"]),
        "targeting": json.loads(row["targeting_rule"]) if row["targeting_rule"] else {},
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


def evaluate(db, flag, identity, attrs=None, _memo=None, _chain=None):
    """按固定优先级求值，返回 (enabled, reason)。

    attrs 为来问时带的属性（dict）。属性条件命中时这一层直接定论为开；
    对不上 / 没带属性 / 开关没定条件，都按原来的放量 / 默认值算。

    开关依赖在全关之后、单人强制之前：用同一身份同一身属性把被依赖的
    开关完整求值一遍，被依赖的不开，本开关一律关（reason=depends_on）。
    依赖链上的结果在单次求值内备忘，保证链上每个开关只算一次、结果一致；
    成环在管理端写入时已拒绝，运行时再兜一层防环。
    """
    if flag["kill_switch"]:
        return False, "kill_switch"

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
                dep_enabled, _ = evaluate(db, dep_flag, identity, attrs,
                                          _memo, next_chain)
            if not dep_enabled:
                if _memo is not None:
                    _memo[flag["id"]] = False
                return False, "depends_on"

    override = db.execute(
        "SELECT enabled FROM overrides WHERE flag_id=? AND identity=?",
        (flag["id"], identity),
    ).fetchone()
    if override is not None:
        result = (bool(override["enabled"]), "override")
        if _memo is not None:
            _memo[flag["id"]] = result[0]
        return result

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
        return natural, reason

    # 互斥组裁决：仅当自然结果为开才参与（属性命中也算）。组内同一身份
    # 最多一个开，先到先得，落定只认身份、与本次带的属性无关。
    winner = db.execute(
        "SELECT flag_id FROM group_assignments WHERE group_id=? AND identity=?",
        (group_id, identity),
    ).fetchone()
    if winner is None:
        # INSERT OR IGNORE + 重读：并发请求下也只有一个人能落定成功
        db.execute(
            "INSERT OR IGNORE INTO group_assignments"
            " (group_id, identity, flag_id, created_at) VALUES (?,?,?,?)",
            (group_id, identity, flag["id"], time.time()),
        )
        db.commit()
        winner = db.execute(
            "SELECT flag_id FROM group_assignments WHERE group_id=? AND identity=?",
            (group_id, identity),
        ).fetchone()
    won = winner["flag_id"] == flag["id"]
    if _memo is not None:
        _memo[flag["id"]] = won
    return won, "group"


# ---------------------------------------------------------------- bundle

def attrs_hash_of(attrs):
    """一身属性的确定性摘要；空属性（没带属性）记为空串。"""
    if not attrs:
        return ""
    return hashlib.sha256(canonical_json(attrs).encode("utf-8")).hexdigest()[:16]


def bundle_content_hash(db, identity, attrs=None):
    """求值输入摘要：对「会影响此身份此身属性求值结果的全部输入」做确定性摘要。

    覆盖：所有开关的求值相关字段、此身份的单人强制、互斥组成员关系、
    此身份在组内的落定记录、开关之间的依赖关系。带属性来拿时，某个
    开关的属性条件只在此人属性对得上（即该条件实际参与了此人求值）时
    才进摘要——改一个此人对不上的条件不影响他的包，对得上的条件增删改
    必换版本；不带属性时摘要与没有「属性条件」这一层时一字不差——
    管理端怎么增改条件，不带属性的老包都不失效。
    与求值无关的字段（如描述、时间戳）不影响摘要；只与别人相关的改动
    （如给他人的单人强制）也不影响此身份的摘要。
    """
    flags = db.execute(
        "SELECT name, default_enabled, rollout_percent, kill_switch, targeting_rule"
        " FROM flags ORDER BY name"
    ).fetchall()
    overrides = db.execute(
        "SELECT f.name, o.enabled FROM overrides o"
        " JOIN flags f ON f.id = o.flag_id WHERE o.identity=? ORDER BY f.name",
        (identity,),
    ).fetchall()
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
        "flags": [[r["name"], bool(r["default_enabled"]), r["rollout_percent"],
                   bool(r["kill_switch"])] for r in flags],
        "overrides": [[r["name"], bool(r["enabled"])] for r in overrides],
        "groups": [[r["g"], r["f"]] for r in members],
        "assignments": [[r["g"], r["f"]] for r in assignments],
    }
    # 开关依赖关系是全局求值输入：改了谁依赖谁（含解除、被依赖开关删除）
    # 所有已发整包都要换新版本。按依赖者名字排序，保证摘要确定。
    dependencies = db.execute(
        "SELECT f.name AS child, p.name AS parent FROM flags f"
        " JOIN flags p ON p.id = f.depends_on_flag_id ORDER BY f.name"
    ).fetchall()
    payload["dependencies"] = [[r["child"], r["parent"]] for r in dependencies]
    if attrs:
        # 带属性的包：属性本身进摘要；属性条件只在「对此人对得上」时进摘要
        # （实际参与了求值才算求值输入，对不上的条件改动不波及此人）
        payload["attrs"] = attrs
        with_rules = []
        for row, r in zip(payload["flags"], flags):
            if r["targeting_rule"] and targeting_matches(r["targeting_rule"], attrs):
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
    组落定记录），再取摘要，保证摘要覆盖本次求值产生的落定记录——管理端
    失效扫描与调用方来拿走同一套求值，记下的版本与本人来拿时拿到的才一致。
    整包共用一份依赖备忘：被依赖的开关先算一次，依赖它的开关与单查它时
    拿到的是同一个结果、同一个理由。"""
    flags = db.execute("SELECT * FROM flags ORDER BY name").fetchall()
    results = {}
    memo = {}
    for flag in flags:
        enabled, reason = evaluate(db, flag, identity, attrs, memo)
        results[flag["name"]] = {"enabled": enabled, "reason": reason}
    return results, bundle_content_hash(db, identity, attrs)


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
    enabled, reason = evaluate(db, flag, identity, attrs)
    body = {
        "flag": name,
        "identity": identity,
        "enabled": enabled,
        "reason": reason,
    }
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
    now = time.time()
    default_enabled = 1 if body.get("default_enabled") else 0
    db.execute(
        "INSERT INTO flags (name, description, default_enabled, targeting_rule,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (name, body.get("description", ""), default_enabled, targeting_json, now, now),
    )
    audit(actor(), name, "default", "create_flag",
          f"default_enabled={bool(default_enabled)}"
          + (f" targeting={targeting_json}" if targeting_json else ""))
    db.commit()
    record_invalidations(db, actor(), f"create_flag {name}")
    return jsonify({"ok": True}), 201


# 可预约定时生效的开关字段（与 PATCH 立即生效支持的字段一致）
SCHEDULABLE_FIELDS = ("kill_switch", "default_enabled", "rollout_percent",
                      "description", "targeting", "depends_on")


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
    targeting / depends_on 写到开关上。

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
                                 "/description/depends_on given)"}), 400
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
    db.execute(
        "INSERT INTO overrides (flag_id, identity, enabled, created_at) VALUES (?,?,?,?)"
        " ON CONFLICT(flag_id, identity) DO UPDATE SET enabled=excluded.enabled",
        (flag["id"], identity, enabled, time.time()),
    )
    audit(actor(), name, "override", "set_override",
          f"identity={identity} enabled={bool(enabled)}")
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
    db.commit()
    record_invalidations(db, actor(),
                         f"remove_override {name} identity={identity}")
    return jsonify({"ok": True})


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
