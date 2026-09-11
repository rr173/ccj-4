"""跨环境推送（POST /api/push）的端到端测试。

覆盖：
- 少写了（source/target/flags 缺一、名单为空/非法、源目标同环境）-> 400 说清楚
- 写了个没有的环境 -> 404 说清楚是哪一边
- 点名的开关在源里没有 -> 404，一个都不推
- 推成后：目标里这些开关按源此刻的规则算（含新建），没点名的还是目标自己的
- 源这边一份规则都没被这次改掉（配置 / 审计 / 历史 / 整包版本逐项对比）
- 依赖按名字落到目标；被依赖开关在目标里不存在 -> 400 一个都不推
- 这么推会让目标里的开关互相绕着依赖 -> 400，一个开关都不改，目标保持推之前的规则
- 目标已发整包在推送后换新版本；目标本地的单人强制等状态不被推送改动
"""
import os
import tempfile
import time

os.environ["FLAG_DB"] = tempfile.mktemp(suffix=".db")
os.environ["ADMIN_TOKEN"] = "test-token"

import app as flag_app  # noqa: E402

flag_app.app.config["TESTING"] = True
c = flag_app.app.test_client()
H = {"X-Admin-Token": "test-token", "X-Actor": "alice"}

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}  {extra}")


def j(r):
    return r.get_json()


def create_env(name):
    r = c.post("/api/environments", headers=H, json={"name": name})
    assert r.status_code in (201, 409), j(r)


def get(path, env):
    return c.get(path, headers={**H, "X-Environment": env})


def post(path, env, body):
    return c.post(path, headers={**H, "X-Environment": env}, json=body)


def patch(path, env, body):
    return c.patch(path, headers={**H, "X-Environment": env}, json=body)


def push(body, headers=None):
    return c.post("/api/push", headers=headers or H, json=body)


def flags_by_name(env):
    return {f["name"]: f for f in j(get("/api/flags", env))}


# 求值相关字段（推送要原样带过去的整套规则 + 描述）
RULE_FIELDS = ("description", "default_enabled", "rollout_percent",
               "rollout_condition", "rollout_rules", "kill_switch",
               "targeting", "config", "depends_on")


def rules_of(env, name):
    f = flags_by_name(env)[name]
    return {k: f[k] for k in RULE_FIELDS}


create_env("staging")
create_env("prod")

print("== 1. 少写了 / 写岔了 -> 400 说清楚，推不成 ==")
r = push({"target": "prod", "flags": ["a"]})
check("少写 source 400", r.status_code == 400 and "source is required" in j(r)["error"], j(r))
r = push({"source": "staging", "flags": ["a"]})
check("少写 target 400", r.status_code == 400 and "target is required" in j(r)["error"], j(r))
r = push({"source": "staging", "target": "prod"})
check("少写 flags 400", r.status_code == 400 and "flags is required" in j(r)["error"], j(r))
r = push({"source": "staging", "target": "prod", "flags": []})
check("flags 空名单 400", r.status_code == 400 and "non-empty" in j(r)["error"], j(r))
r = push({"source": "staging", "target": "prod", "flags": "a"})
check("flags 不是名单 400", r.status_code == 400, j(r))
r = push({"source": "staging", "target": "prod", "flags": ["a", 1]})
check("flags 里有非字符串 400", r.status_code == 400, j(r))
r = push({"source": "staging", "target": "prod", "flags": [""]})
check("flags 里有空名 400", r.status_code == 400, j(r))
r = push({"source": "staging", "target": "staging", "flags": ["a"]})
check("源目标同环境 400", r.status_code == 400 and "different" in j(r)["error"], j(r))
r = push({"source": "../bad", "target": "prod", "flags": ["a"]})
check("环境名非法 400", r.status_code == 400, j(r))
r = push({"source": "staging", "from": "prod", "target": "prod", "flags": ["a"]})
check("source/from 不一致 400", r.status_code == 400 and "must match" in j(r)["error"], j(r))
r = c.post("/api/push", headers={"X-Admin-Token": "wrong"},
           json={"source": "staging", "target": "prod", "flags": ["a"]})
check("没有管理令牌 401", r.status_code == 401, j(r))
r = c.post("/api/push", headers=H, data="not json",
           content_type="application/json")
check("body 不是 JSON 对象 400", r.status_code == 400, r.status_code)

print("== 2. 写了个没有的环境 -> 404 说清楚 ==")
r = push({"source": "nope", "target": "prod", "flags": ["a"]})
check("源环境没有 404", r.status_code == 404
      and "source environment not found: nope" in j(r)["error"], j(r))
r = push({"source": "staging", "target": "nope", "flags": ["a"]})
check("目标环境没有 404", r.status_code == 404
      and "target environment not found: nope" in j(r)["error"], j(r))

print("== 3. 点名的开关在源里没有 -> 404，一个都不推 ==")
r = post("/api/flags", "staging",
         {"name": "alpha", "default_enabled": True,
          "description": "alpha from source",
          "targeting": {"plan": "pro"}, "config": {"theme": "dark"}})
check("源建 alpha", r.status_code == 201, j(r))
r = push({"source": "staging", "target": "prod", "flags": ["alpha", "ghost"]})
check("源里没有的开关 404", r.status_code == 404
      and "ghost" in j(r)["error"] and "staging" in j(r)["error"], j(r))
check("一个都不推（alpha 也没进目标）", "alpha" not in flags_by_name("prod"))

print("== 4. 推成后：点名的按源此刻的规则算，没点名的还是目标自己的 ==")
# 源上再补几个开关：依赖、条件放量、放量规矩、全关
post("/api/flags", "staging", {"name": "beta", "default_enabled": True})
patch("/api/flags/beta", "staging", {"depends_on": "alpha"})
post("/api/flags", "staging",
     {"name": "gamma", "rollout_condition": {"plan": "pro"},
      "rollout_rules": [{"condition": {"plan": "ent"}, "percent": 100}]})
patch("/api/flags/gamma", "staging", {"rollout_percent": 50})
post("/api/flags", "staging", {"name": "delta"})
patch("/api/flags/delta", "staging", {"kill_switch": True})

# 目标自己的预置：同名不同规则的 alpha、没点名的 local、以及对 alpha 的单人强制
post("/api/flags", "prod", {"name": "alpha", "default_enabled": False,
                            "description": "target 自己的"})
post("/api/flags", "prod", {"name": "local", "default_enabled": False,
                            "description": "target only"})
r = c.put("/api/flags/alpha/overrides", headers={**H, "X-Environment": "prod"},
          json={"identity": "u9", "enabled": False})
check("目标预置单人强制", r.status_code == 200, j(r))

# 推送前的快照：源的配置 / 审计 / 历史 / 整包版本；目标 local 的规则；u2 的目标整包
src_flags_before = j(get("/api/flags", "staging"))
src_audit_before = j(get("/api/audit?limit=500", "staging"))
t0 = time.time()
src_hist_before = j(get(f"/api/history?identity=u1&at={t0}", "staging"))
src_bundle_before = j(get("/api/bundle?identity=u1", "staging"))
local_rules_before = rules_of("prod", "local")
tgt_bundle_before = j(get("/api/bundle?identity=u2", "prod"))

r = push({"source": "staging", "target": "prod",
          "flags": ["alpha", "beta", "gamma", "delta"]})
check("推送成功", r.status_code == 200, j(r))
check("响应点名推了哪些", j(r)["pushed"] == ["alpha", "beta", "delta", "gamma"], j(r))
check("响应分出新建与更新", j(r)["created"] == ["beta", "delta", "gamma"]
      and j(r)["updated"] == ["alpha"], j(r))

for name in ("alpha", "beta", "gamma", "delta"):
    check(f"目标 {name} 按源此刻的规则算",
          rules_of("prod", name) == rules_of("staging", name),
          (rules_of("prod", name), rules_of("staging", name)))
check("没点名的还是目标自己的", rules_of("prod", "local") == local_rules_before,
      (rules_of("prod", "local"), local_rules_before))

# 同一身份同一身属性，目标与源逐个结果一致（分桶只认开关名与身份，跨环境相同）
for name in ("alpha", "beta", "gamma", "delta"):
    for qs in ("identity=u1", "identity=u2&attrs=%7B%22plan%22%3A%22pro%22%7D",
               "identity=u3&attrs=%7B%22plan%22%3A%22ent%22%7D"):
        s = j(get(f"/api/flags/{name}/check?{qs}", "staging"))
        t = j(get(f"/api/flags/{name}/check?{qs}", "prod"))
        check(f"{name}?{qs} 目标与源结果一致",
              (s["enabled"], s["reason"], s.get("config"))
              == (t["enabled"], t["reason"], t.get("config")), (s, t))

# 目标本地的单人强制是目标自己的状态：推送不抄也不清，仍然生效
r = j(get("/api/flags/alpha/check?identity=u9", "prod"))
check("目标本地单人强制不被推送改动", r["enabled"] is False and r["reason"] == "override", r)

print("== 5. 源这边一份都没被这次改掉 ==")
check("源开关配置不变", j(get("/api/flags", "staging")) == src_flags_before)
check("源审计没有多一条", j(get("/api/audit?limit=500", "staging")) == src_audit_before)
check("源历史重放不变",
      j(get(f"/api/history?identity=u1&at={t0}", "staging")) == src_hist_before)
r = j(get(f"/api/bundle?identity=u1&version={src_bundle_before['version']}", "staging"))
check("源整包版本仍有效", r["valid"] is True and r["version"] == src_bundle_before["version"], r)
src_inv = j(get("/api/bundles/invalidations?limit=500", "staging"))
check("源没有失效记录", src_inv == [], src_inv)

print("== 6. 目标已发整包换新版本，失效记录可查 ==")
r = j(get(f"/api/bundle?identity=u2&version={tgt_bundle_before['version']}", "prod"))
check("目标旧包过期", r["valid"] is False and r["version"] != tgt_bundle_before["version"], r)
check("新包按源规则算", r["flags"]["alpha"]["enabled"] is True
      and r["flags"]["beta"]["enabled"] is True
      and r["flags"]["delta"]["reason"] == "kill_switch", r["flags"])
check("没点名的在新包里还是目标自己的", r["flags"]["local"]["enabled"] is False, r["flags"])
inv = j(get("/api/bundles/invalidations?limit=500", "prod"))
check("失效记录写明是这次推送", any("push staging -> prod" in x["change"]
                                    and x["identity"] == "u2" for x in inv), inv)
audit = j(get("/api/audit?limit=500", "prod"))
check("目标审计记下这次推送", any(x["action"] == "push_flags" for x in audit), audit)

print("== 7. 依赖按名字落到目标，按目标的开关求值 ==")
r = j(get("/api/flags/beta/check?identity=u1", "prod"))
check("依赖链在目标生效（alpha 开 -> beta 开）", r["enabled"] is True, r)
r = patch("/api/flags/alpha", "prod", {"kill_switch": True})
check("目标里全关 alpha", r.status_code == 200, j(r))
r = j(get("/api/flags/beta/check?identity=u1", "prod"))
check("目标的 alpha 关了 -> 目标的 beta 依赖关", r["enabled"] is False
      and r["reason"] == "depends_on", r)
r = j(get("/api/flags/alpha/check?identity=u1", "staging"))
check("源 alpha 不受目标全关影响", r["enabled"] is True, r)
patch("/api/flags/alpha", "prod", {"kill_switch": False})

print("== 8. 被依赖开关在目标里不存在 -> 400，一个都不推 ==")
post("/api/flags", "staging", {"name": "base", "default_enabled": True})
post("/api/flags", "staging", {"name": "lonely", "default_enabled": True})
patch("/api/flags/lonely", "staging", {"depends_on": "base"})
r = push({"source": "staging", "target": "prod", "flags": ["lonely"]})
check("依赖目标缺失 400", r.status_code == 400 and "base" in j(r)["error"]
      and "prod" in j(r)["error"], j(r))
check("一个都不推（lonely 没进目标）", "lonely" not in flags_by_name("prod"))
r = push({"source": "staging", "target": "prod", "flags": ["lonely", "base"]})
check("一起推就成", r.status_code == 200, j(r))
check("依赖按名字落到目标", rules_of("prod", "lonely")["depends_on"] == "base",
      rules_of("prod", "lonely"))

print("== 9. 会让目标互相绕着依赖 -> 400，一个开关都不改 ==")
# 源：cycA -> cycB；目标：cycB -> cycA（各自环境内都合法）。只推 cycA 就会在目标成环。
post("/api/flags", "staging", {"name": "cycB"})
post("/api/flags", "staging", {"name": "cycA"})
patch("/api/flags/cycA", "staging", {"depends_on": "cycB"})
post("/api/flags", "prod", {"name": "cycA", "default_enabled": True})
post("/api/flags", "prod", {"name": "cycB", "default_enabled": True})
patch("/api/flags/cycB", "prod", {"depends_on": "cycA"})
# 间接成环：源 chainX -> chainY；目标 chainY -> chainZ -> chainX
post("/api/flags", "staging", {"name": "chainY"})
post("/api/flags", "staging", {"name": "chainX"})
patch("/api/flags/chainX", "staging", {"depends_on": "chainY"})
post("/api/flags", "prod", {"name": "chainX"})
post("/api/flags", "prod", {"name": "chainY"})
post("/api/flags", "prod", {"name": "chainZ"})
patch("/api/flags/chainY", "prod", {"depends_on": "chainZ"})
patch("/api/flags/chainZ", "prod", {"depends_on": "chainX"})

tgt_before = flags_by_name("prod")
r = push({"source": "staging", "target": "prod", "flags": ["cycA"]})
check("直接成环 400", r.status_code == 400 and "circular" in j(r)["error"], j(r))
r = push({"source": "staging", "target": "prod",
          "flags": ["chainX", "alpha"]})  # 同批还有个没问题的 alpha
check("间接成环也 400", r.status_code == 400 and "circular" in j(r)["error"], j(r))
after = flags_by_name("prod")
check("一个开关都没改（含同批没问题的 alpha）",
      {n: {k: after[n][k] for k in RULE_FIELDS} for n in after}
      == {n: {k: tgt_before[n][k] for k in RULE_FIELDS} for n in tgt_before})
check("目标依赖边也没动", after["cycB"]["depends_on"] == "cycA"
      and after["chainZ"]["depends_on"] == "chainX"
      and after["chainX"]["depends_on"] == "", after)
audit = j(get("/api/audit?limit=500", "prod"))
check("目标审计记下推不成的原因", any(x["action"] == "push_rejected"
                                      and "circular" in x["detail"] for x in audit), audit)
src_now = j(get("/api/flags", "staging"))
r = push({"source": "staging", "target": "prod", "flags": ["cycA"]})
check("成环被拒时源也不变", r.status_code == 400
      and j(get("/api/flags", "staging")) == src_now)

print("== 10. from/to 别名 ==")
r = push({"from": "staging", "to": "prod", "flags": ["gamma"]})
check("from/to 也能推", r.status_code == 200 and j(r)["pushed"] == ["gamma"], j(r))

print(f"\n{'=' * 40}\nPASS {passed}  FAIL {failed}")
if failed:
    raise SystemExit(1)
