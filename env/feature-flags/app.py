"""特性开关服务：管理端 + 调用方查询接口。

求值优先级（固定，不可配置）：
    1. 全关（kill switch）        -> 一律关
    2. 单人强制（override）        -> 强制开 / 强制关
    3. 比例放量（rollout_percent） -> 比例 > 0 时此层定论：命中开、未命中关
    4. 默认值（default_enabled）   -> 仅当放量比例为 0（未启用放量）时兜底

互斥组（mutex group）：管理端可把多个开关编入同一组，一个开关最多进一组。
对同一身份，组内最多一个开关为开：第 3/4 层判开后，若开关在组内，则由
组规则裁决——该身份在组内已落定过别的开关则判关，否则落定为本开关并
写库；之后身份不变，落定的开关不变。全关与单人强制优先于组规则
（强制开不受组内已有人开着的限制，全关仍然全关）；未进组的开关按原
规则求值，不参与组规则。

放量分桶：sha256("{flag_name}:{identity}") % 100，同一身份对同一开关
永远落在同一侧，与进程、机器、重启无关。

整包（bundle）：调用方只带身份，一次拿走所有开关的开/关结果与整包版本。
版本是对「会影响此身份求值的全部输入」的确定性摘要：配置不变时同一身份
反复来拿，结果与版本都不变；管理端改动任何会影响此身份的一层后，版本必变。
带 version 参数来问可校验手中的包是否过期。已发出的整包落库，管理端每次
变更后重算各整包版本，版本变了的身份记入 bundle_invalidations，
管理端可看到「谁改了什么、让哪些人的整包失效了」。
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
-- 已发出的整包：身份 -> 最近一次整包的版本与结果
CREATE TABLE IF NOT EXISTS bundles (
    identity   TEXT PRIMARY KEY,
    version    TEXT NOT NULL,
    results    TEXT NOT NULL,
    updated_at REAL NOT NULL
);
-- 整包失效记录：哪次变更（谁、改了什么）让哪个身份的整包过期了
CREATE TABLE IF NOT EXISTS bundle_invalidations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,
    change      TEXT NOT NULL,
    identity    TEXT NOT NULL,
    old_version TEXT NOT NULL,
    new_version TEXT NOT NULL,
    created_at  REAL NOT NULL
);
"""

LAYERS = ("kill_switch", "override", "group", "rollout", "default")


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
    conn.commit()
    conn.close()


def audit(actor, flag_name, layer, action, detail=""):
    get_db().execute(
        "INSERT INTO audit_log (actor, flag_name, layer, action, detail, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (actor, flag_name, layer, action, detail, time.time()),
    )


def flag_to_dict(row):
    return {
        "name": row["name"],
        "description": row["description"],
        "default_enabled": bool(row["default_enabled"]),
        "rollout_percent": row["rollout_percent"],
        "kill_switch": bool(row["kill_switch"]),
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


def evaluate(db, flag, identity):
    """按固定优先级求值，返回 (enabled, reason)。"""
    if flag["kill_switch"]:
        return False, "kill_switch"

    override = db.execute(
        "SELECT enabled FROM overrides WHERE flag_id=? AND identity=?",
        (flag["id"], identity),
    ).fetchone()
    if override is not None:
        return bool(override["enabled"]), "override"

    # 放量比例 > 0 时这一层直接定论：命中开、未命中关，不再落到默认值
    if flag["rollout_percent"] > 0:
        natural = bucket_of(flag["name"], identity) < flag["rollout_percent"]
        reason = "rollout"
    else:
        natural = bool(flag["default_enabled"])
        reason = "default"

    group_id = group_of(db, flag["id"])
    if group_id is None or not natural:
        return natural, reason

    # 互斥组裁决：仅当自然结果为开才参与。组内同一身份最多一个开，
    # 先到先得；落定写库后，身份不变，开着的那个不换。
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
    return winner["flag_id"] == flag["id"], "group"


# ---------------------------------------------------------------- bundle

def bundle_version(db, identity):
    """整包版本：对「会影响此身份求值结果的全部输入」做确定性摘要。

    覆盖：所有开关的求值相关字段、此身份的单人强制、互斥组成员关系、
    此身份在组内的落定记录。任一变化都会改变版本；与求值无关的字段
    （如描述、时间戳）不影响版本；只与别人相关的改动（如给他人的
    单人强制）也不影响此身份的版本。
    """
    flags = db.execute(
        "SELECT name, default_enabled, rollout_percent, kill_switch"
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
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def compute_bundle(db, identity):
    """求出此身份所有开关的结果与整包版本。先求值（可能写入组落定记录），
    再取版本，保证版本覆盖了本次求值产生的落定记录，之后重拿版本不变。"""
    flags = db.execute("SELECT * FROM flags ORDER BY name").fetchall()
    results = {}
    for flag in flags:
        enabled, reason = evaluate(db, flag, identity)
        results[flag["name"]] = {"enabled": enabled, "reason": reason}
    return results, bundle_version(db, identity)


def record_invalidations(db, actor_name, change):
    """配置变更后调用：重算每个已发整包的版本，版本变了的身份记一条
    失效记录（谁改的、哪次变更、让谁的整包从哪个版本变成哪个版本）。"""
    rows = db.execute("SELECT identity, version FROM bundles").fetchall()
    if not rows:
        return
    now = time.time()
    for r in rows:
        current = bundle_version(db, r["identity"])
        if current != r["version"]:
            db.execute(
                "INSERT INTO bundle_invalidations"
                " (actor, change, identity, old_version, new_version, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (actor_name, change, r["identity"], r["version"], current, now),
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
    """调用方接口：只带身份，得到 开/关。

    identity 走查询参数（调用方需做 URL 编码），允许包含斜杠、空格、
    引号等任意字符；不做 strip，首尾空格也是身份的一部分。
    """
    identity = request.args.get("identity")
    if not identity:
        return jsonify({"error": "identity is required"}), 400
    db = get_db()
    flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    enabled, reason = evaluate(db, flag, identity)
    return jsonify({
        "flag": name,
        "identity": identity,
        "enabled": enabled,
        "reason": reason,
    })


@app.get("/api/bundle")
def bundle():
    """调用方整包接口：只带身份，一次拿走所有开关的开/关与整包版本。

    配置不变时，同一身份多次来拿，每个开关的结果与版本都不变。
    带上 version 参数可校验手中的包是否仍然有效：valid=false 即已过期，
    响应里同时附带按当前规则重算的新版本与新结果。
    """
    identity = request.args.get("identity")
    if not identity:
        return jsonify({"error": "identity is required"}), 400
    db = get_db()
    results, version = compute_bundle(db, identity)
    db.execute(
        "INSERT INTO bundles (identity, version, results, updated_at) VALUES (?,?,?,?)"
        " ON CONFLICT(identity) DO UPDATE SET version=excluded.version,"
        " results=excluded.results, updated_at=excluded.updated_at",
        (identity, version, json.dumps(results, ensure_ascii=False), time.time()),
    )
    db.commit()
    body = {"identity": identity, "version": version, "flags": results}
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
    result = []
    for row in rows:
        item = flag_to_dict(row)
        item["override_count"] = db.execute(
            "SELECT COUNT(*) c FROM overrides WHERE flag_id=?", (row["id"],)
        ).fetchone()["c"]
        item["group"] = groups.get(row["id"])
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
    now = time.time()
    default_enabled = 1 if body.get("default_enabled") else 0
    db.execute(
        "INSERT INTO flags (name, description, default_enabled, created_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        (name, body.get("description", ""), default_enabled, now, now),
    )
    audit(actor(), name, "default", "create_flag",
          f"default_enabled={bool(default_enabled)}")
    db.commit()
    record_invalidations(db, actor(), f"create_flag {name}")
    return jsonify({"ok": True}), 201


@app.patch("/api/flags/<name>")
@require_admin
def update_flag(name):
    db = get_db()
    flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    body = request.get_json(force=True)
    changes = []

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
            return jsonify({"error": "rollout_percent must be an int in [0,100]"}), 400
        if new != flag["rollout_percent"]:
            db.execute("UPDATE flags SET rollout_percent=?, updated_at=? WHERE id=?",
                       (new, time.time(), flag["id"]))
            changes.append(("rollout", "set_rollout",
                            f"rollout_percent: {flag['rollout_percent']} -> {new}"))
    if "description" in body:
        db.execute("UPDATE flags SET description=?, updated_at=? WHERE id=?",
                   (body["description"], time.time(), flag["id"]))

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
    cur = db.execute("DELETE FROM flags WHERE name=?", (name,))
    if cur.rowcount == 0:
        return jsonify({"error": "flag not found"}), 404
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
    """管理端：已发出的整包清单，stale=true 表示配置已变、此人还没来拿新包。"""
    db = get_db()
    rows = db.execute(
        "SELECT identity, version, updated_at FROM bundles"
        " ORDER BY updated_at DESC LIMIT 500"
    ).fetchall()
    return jsonify([
        {"identity": r["identity"], "version": r["version"],
         "stale": bundle_version(db, r["identity"]) != r["version"],
         "updated_at": r["updated_at"]}
        for r in rows
    ])


@app.get("/api/bundles/invalidations")
@require_admin
def list_bundle_invalidations():
    """管理端：整包失效记录——谁改了什么、让哪些人的整包过期了。"""
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = get_db().execute(
        "SELECT actor, change, identity, old_version, new_version, created_at"
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
