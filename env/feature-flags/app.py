"""特性开关服务：管理端 + 调用方查询接口。

求值优先级（固定，不可配置）：
    1. 全关（kill switch）        -> 一律关
    2. 单人强制（override）        -> 强制开 / 强制关
    3. 比例放量（rollout_percent） -> 命中放量区间则开
    4. 默认值（default_enabled）   -> 以上都未决定时兜底

放量分桶：sha256("{flag_name}:{identity}") % 100，同一身份对同一开关
永远落在同一侧，与进程、机器、重启无关。
"""

import hashlib
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
"""

LAYERS = ("kill_switch", "override", "rollout", "default")


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

    if flag["rollout_percent"] > 0:
        if bucket_of(flag["name"], identity) < flag["rollout_percent"]:
            return True, "rollout"

    return bool(flag["default_enabled"]), "default"


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
    """调用方接口：只带身份，得到 开/关。"""
    identity = request.args.get("identity", "").strip()
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


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


# ---------------------------------------------------------------- admin API

@app.get("/api/flags")
@require_admin
def list_flags():
    db = get_db()
    rows = db.execute("SELECT * FROM flags ORDER BY name").fetchall()
    result = []
    for row in rows:
        item = flag_to_dict(row)
        item["override_count"] = db.execute(
            "SELECT COUNT(*) c FROM overrides WHERE flag_id=?", (row["id"],)
        ).fetchone()["c"]
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


@app.put("/api/flags/<name>/overrides/<identity>")
@require_admin
def put_override(name, identity):
    body = request.get_json(force=True)
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
    return jsonify({"ok": True})


@app.delete("/api/flags/<name>/overrides/<identity>")
@require_admin
def delete_override(name, identity):
    db = get_db()
    flag = db.execute("SELECT * FROM flags WHERE name=?", (name,)).fetchone()
    if flag is None:
        return jsonify({"error": "flag not found"}), 404
    db.execute("DELETE FROM overrides WHERE flag_id=? AND identity=?",
               (flag["id"], identity))
    audit(actor(), name, "override", "remove_override", f"identity={identity}")
    db.commit()
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


# ---------------------------------------------------------------- admin page

@app.get("/")
def admin_page():
    return render_template("index.html")


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
