"""开关挂配置（flag config）的端到端测试。

覆盖需求（口语版）：
1. 管理端能给开关挂一份配置：这个人对这个开关是开，单查和整包里都要带上
   这份；是关就不要带（响应里没有 config 这个键）。没挂配置的开关行为与
   以前一字不差。
2. 改这份配置，整包换新版本，拿着改前那包来问要说过期（valid=false）；
   只对此刻判开的人生效，判关的人版本不变。改回不复活。
3. 冻住在开的人还拿冻住那一刻那份：冻住后改配置，他单查/整包/带属性问
   拿到的都是旧那份，且他的包版本不变；全关后不要带；解除全关后快照回来。
   冻在关的人不带配置。重新冻（结果变化）时快照跟着换。
4. 没挂配置的一切按原来的；原来问一个、拿整包、带属性问都不能坏。
另含：挂/清配置的校验、定时生效、发布稿、依赖/组/强制各层下的携带规则。
"""
import json
import os
import tempfile
import time
import urllib.parse

os.environ["FLAG_DB"] = tempfile.mktemp(suffix=".db")
os.environ["ADMIN_TOKEN"] = "test-token"

import app as flag_app  # noqa: E402

flag_app.app.config["TESTING"] = True

flag_app.init_environment_db("test", "test")
class _EnvClient(flag_app.FlaskClient):
    def open(self, *args, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault("X-Environment", "test")
        kwargs["headers"] = headers
        return super().open(*args, **kwargs)
flag_app.app.test_client_class = _EnvClient
c = flag_app.app.test_client()
H = {"X-Actor": "alice"}
H_BOB = {"X-Actor": "bob", "X-Admin-Token": "test-token"}
H["X-Admin-Token"] = "test-token"

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}  {extra}")


def q(identity, attrs=None, version=None):
    s = f"identity={urllib.parse.quote(identity)}"
    if attrs is not None:
        s += "&attrs=" + urllib.parse.quote(json.dumps(attrs, ensure_ascii=False))
    if version is not None:
        s += f"&version={version}"
    return s


def check_flag(flag, identity, attrs=None):
    r = c.get(f"/api/flags/{flag}/check?{q(identity, attrs)}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def bundle(identity, attrs=None, version=None):
    r = c.get(f"/api/bundle?{q(identity, attrs, version)}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def patch(flag, body, headers=H):
    return c.patch(f"/api/flags/{flag}", headers=headers, json=body)


def create(name, **kw):
    return c.post("/api/flags", headers=H, json={"name": name, **kw})


def freeze(flag, identity):
    return c.put(f"/api/flags/{flag}/freezes", headers=H, json={"identity": identity})


def unfreeze(flag, identity):
    return c.delete(f"/api/flags/{flag}/freezes", headers=H,
                    json={"identity": identity})


def invalidations(identity=None, limit=500):
    rows = c.get(f"/api/bundles/invalidations?limit={limit}", headers=H).get_json()
    return [r for r in rows if identity is None or r["identity"] == identity]


CFG1 = {"color": "blue", "n": 3}
CFG2 = {"color": "green", "n": 4}

# ---------------------------------------------------------------- 准备开关
create("fon", default_enabled=True)          # 默认开
create("foff")                               # 默认关
create("plain", default_enabled=True)        # 全程不挂配置的对照开关
c.post("/api/groups", headers=H, json={"name": "g1"})
create("ga", default_enabled=True)
create("gb", default_enabled=True)
c.put("/api/groups/g1/flags", headers=H, json={"flag": "ga"})
c.put("/api/groups/g1/flags", headers=H, json={"flag": "gb"})

print("== 1. 没挂配置时：老路一字不差 ==")
r = check_flag("fon", "u1")
check("没挂配置的开：响应没有 config 键", "config" not in r, r)
check("没挂配置的开：结构不变", set(r) == {"flag", "identity", "enabled", "reason"}, r)
r = check_flag("foff", "u1")
check("没挂配置的关：没有 config 键", "config" not in r)
b = bundle("u1")
check("整包里没挂配置的开关没有 config 键",
      all("config" not in v for v in b["flags"].values()), b["flags"])

print("== 2. 挂上配置：开的人单查 / 整包都带；关的不带 ==")
r = patch("fon", {"config": CFG1})
check("挂配置 200", r.status_code == 200 and r.get_json()["changed"] == 1, r.get_json())
r = check_flag("fon", "u1")
check("开的人单查带上配置", r.get("config") == CFG1 and r["enabled"] is True, r)
b = bundle("u1")
check("开的人整包带上配置", b["flags"]["fon"].get("config") == CFG1, b["flags"]["fon"])
check("整包里其他开关不带 config", "config" not in b["flags"]["plain"]
      and "config" not in b["flags"]["foff"], b["flags"])
r = check_flag("foff", "u1")
check("关的开关挂了配置也不带（单查无 config 键）", "config" not in r, r)
b = bundle("u1")
check("关的开关挂了配置也不带（整包无 config 键）",
      "config" not in b["flags"]["foff"], b["flags"]["foff"])
# 管理端列表能看到这份配置
rows = c.get("/api/flags", headers=H).get_json()
fon_row = next(f for f in rows if f["name"] == "fon")
check("开关列表回显 config", fon_row["config"] == CFG1, fon_row)
check("没挂的开关 config 是 null",
      next(f for f in rows if f["name"] == "plain")["config"] is None)

print("== 3. 配置支持任意 JSON：数组 / 标量 / 嵌套 / null ==")
for val in ([1, 2, 3], "just-a-string", 42, True, {"nested": {"a": [1, True, None]}}):
    patch("fon", {"config": val})
    r = check_flag("fon", "u1")
    check(f"配置 {json.dumps(val, ensure_ascii=False)[:20]} 原样带回",
          r.get("config") == val, r)
patch("fon", {"config": CFG1})  # 还原
check("非法配置（NaN）400", patch("fon", {"config": float("nan")}).status_code == 400)

print("== 4. 改配置：判开的人整包换新版本、旧包过期 ==")
b0 = bundle("u2")
check("改前 u2 的整包带 CFG1", b0["flags"]["fon"].get("config") == CFG1)
patch("fon", {"config": CFG2}, headers=H_BOB)
b1 = bundle("u2")
check("改配置后版本变了", b1["version"] != b0["version"])
check("再来拿带的是新配置", b1["flags"]["fon"].get("config") == CFG2)
r = bundle("u2", version=b0["version"])
check("拿改前那包来问：valid=false", r["valid"] is False)
check("过期响应里是新版本与新配置",
      r["version"] == b1["version"] and r["flags"]["fon"].get("config") == CFG2)
r = bundle("u2", version=b1["version"])
check("拿新包来问：valid=true", r["valid"] is True)
inv = [x for x in invalidations("u2") if x["change"].startswith("update_flag fon")]
check("失效记录记下这次改配置", len(inv) >= 1 and inv[0]["actor"] == "bob", inv[:2])
check("记录的新版本与本人来拿一致", inv[0]["new_version"] == b1["version"])
check("记录的旧版本正是手里那包", inv[0]["old_version"] == b0["version"])

print("== 5. 改配置只波及此刻判开的人；判关的人版本不变 ==")
# u3：fon 被强制关；改 fon 配置不该动他的包
c.put("/api/flags/fon/overrides", headers=H, json={"identity": "u3", "enabled": False})
b_off = bundle("u3")
check("强制关的人拿不到配置", "config" not in b_off["flags"]["fon"])
patch("fon", {"config": {"color": "red"}})
check("强制关的人：改配置版本不变", bundle("u3")["version"] == b_off["version"])
check("强制关的人：依然不带配置",
      "config" not in bundle("u3")["flags"]["fon"])
c.delete("/api/flags/fon/overrides", headers=H, json={"identity": "u3"})
check("解除强制关后立刻带上新配置",
      bundle("u3")["flags"]["fon"].get("config") == {"color": "red"})
patch("fon", {"config": CFG2})
# foff 默认关：给挂配置、改配置都不影响任何已拿包的人
v_foff = bundle("u4")["version"]
patch("foff", {"config": {"x": 1}})
check("给关着的开关挂配置：判关的人版本不变", bundle("u4")["version"] == v_foff)
patch("foff", {"config": {"x": 2}})
check("改关着开关的配置：判关的人版本仍不变", bundle("u4")["version"] == v_foff)
# 但开关一旦对此人开（强制开），配置立刻随结果带上、包换版本
c.put("/api/flags/foff/overrides", headers=H, json={"identity": "u4", "enabled": True})
b = bundle("u4")
check("强制开后带上配置", b["flags"]["foff"].get("config") == {"x": 2})
c.delete("/api/flags/foff/overrides", headers=H, json={"identity": "u4"})

print("== 6. 全关后不要带；解除全关后配置回来 ==")
patch("fon", {"kill_switch": True})
r = check_flag("fon", "u1")
check("全关：单查关且无 config",
      r["enabled"] is False and r["reason"] == "kill_switch" and "config" not in r, r)
b = bundle("u1")
check("全关：整包里 fon 无 config", "config" not in b["flags"]["fon"], b["flags"]["fon"])
patch("fon", {"kill_switch": False})
check("解除全关：配置回来", check_flag("fon", "u1").get("config") == CFG2)

print("== 7. 冻住：开着的人拿冻住那一刻那份 ==")
patch("fon", {"config": CFG1})
r = freeze("fon", "u5")
check("冻住开：返回值带当时配置", r.get_json().get("config") == CFG1, r.get_json())
fz = c.get("/api/flags/fon/freezes", headers=H).get_json()
row = next(x for x in fz if x["identity"] == "u5")
check("冻结列表能看到快照配置", row["config"] == CFG1 and row["enabled"] is True, row)
# 冻住后把配置换成 CFG2
patch("fon", {"config": CFG2})
r = check_flag("fon", "u5")
check("冻住后单查仍给冻住那一刻的旧配置",
      r["enabled"] is True and r["reason"] == "freeze"
      and r.get("config") == CFG1, r)
b = bundle("u5")
check("冻住后整包仍给旧配置", b["flags"]["fon"].get("config") == CFG1
      and b["flags"]["fon"]["reason"] == "freeze", b["flags"]["fon"])
PRO = {"plan": "pro"}
r = check_flag("fon", "u5", PRO)
check("冻住后带属性问也给旧配置（只认人）",
      r["reason"] == "freeze" and r.get("config") == CFG1, r)
v5 = bundle("u5")["version"]
v5p = bundle("u5", PRO)["version"]
patch("fon", {"config": {"color": "red", "n": 9}})
check("冻住期间改配置：无属性包版本不变", bundle("u5")["version"] == v5)
check("冻住期间改配置：属性包版本不变", bundle("u5", PRO)["version"] == v5p)
check("冻住期间改配置：没有给 u5 产生失效记录",
      not [x for x in invalidations("u5")
           if x["change"].startswith("update_flag fon")],
      invalidations("u5"))
# 没冻的人拿新配置
check("没冻的 u6 拿到改后的新配置",
      check_flag("fon", "u6").get("config") == {"color": "red", "n": 9})
# 全关压过：冻住的人也不带
patch("fon", {"kill_switch": True})
check("冻住 + 全关：不带配置",
      "config" not in check_flag("fon", "u5")
      and "config" not in bundle("u5")["flags"]["fon"])
patch("fon", {"kill_switch": False})
check("全关解除：又拿回冻住那一刻的旧配置",
      check_flag("fon", "u5").get("config") == CFG1)
# 解冻：立刻按当时的新配置走
unfreeze("fon", "u5")
r = check_flag("fon", "u5")
check("解冻后拿到当前（改后的）配置",
      r["reason"] != "freeze" and r.get("config") == {"color": "red", "n": 9}, r)

print("== 8. 冻在关：不带配置；重新冻到开时快照补上 ==")
patch("foff", {"config": {"x": 1}})
r = freeze("foff", "u7")
check("冻在关：返回无 config", r.get_json()["enabled"] is False
      and r.get_json().get("config") is None, r.get_json())
check("冻在关：单查 / 整包都不带配置",
      "config" not in check_flag("foff", "u7")
      and "config" not in bundle("u7")["flags"]["foff"])
patch("foff", {"config": {"x": 2}})  # 改配置不动冻在关的人
v7 = bundle("u7")["version"]
check("冻在关改配置：版本不变、仍不带",
      bundle("u7")["version"] == v7
      and "config" not in bundle("u7")["flags"]["foff"])
# 强制开后重新冻：冻住的结果变成开，快照是此刻的配置 {"x": 2}
c.put("/api/flags/foff/overrides", headers=H, json={"identity": "u7", "enabled": True})
r = freeze("foff", "u7")
check("结果变了重新冻：changed=true、冻在开、带此刻配置",
      r.get_json()["changed"] is True and r.get_json()["enabled"] is True
      and r.get_json().get("config") == {"x": 2}, r.get_json())
check("重新冻后整包带新快照", bundle("u7")["flags"]["foff"].get("config") == {"x": 2})
# 冻住后再改配置，仍然只拿快照那份
patch("foff", {"config": {"x": 3}})
check("重新冻住后改配置：仍拿快照 {x:2}",
      check_flag("foff", "u7").get("config") == {"x": 2})
c.delete("/api/flags/foff/overrides", headers=H, json={"identity": "u7"})
unfreeze("foff", "u7")

print("== 9. 重复冻：结果与快照都没变 = no-op，不换版本 ==")
patch("fon", {"config": CFG1, "kill_switch": False})
unfreeze("fon", "u8")  # u8 在第 8 节被冻过（快照是旧的 {"x":2}），先解冻
freeze("fon", "u8")    # 此刻 config=CFG1，重新冻到 CFG1 这份快照
v8 = bundle("u8")["version"]
n8 = len(invalidations("u8"))
r = freeze("fon", "u8")
check("重复冻 changed=false", r.get_json()["changed"] is False, r.get_json())
check("重复冻版本不变、无新失效",
      bundle("u8")["version"] == v8 and len(invalidations("u8")) == n8)
# 冻住期间改配置后再冻（值没变）：仍按 no-op 处理，快照不悄悄更新
patch("fon", {"config": CFG2})
r = freeze("fon", "u8")
check("冻住期间改配置后重复冻：仍 changed=false（要刷新快照先解冻）",
      r.get_json()["changed"] is False, r.get_json())
check("此人仍拿冻住时的旧配置", check_flag("fon", "u8").get("config") == CFG1)
unfreeze("fon", "u8")

print("== 10. 互斥组：组内争到的开关带配置，没争到的不带 ==")
patch("ga", {"config": {"variant": "a"}})
patch("gb", {"config": {"variant": "b"}})
# 组内先到先得：u9 整包求值时按名字序先算 ga，ga 落定
b = bundle("u9")
winner = "ga" if b["flags"]["ga"]["enabled"] else "gb"
loser = "gb" if winner == "ga" else "ga"
expected_cfg = {"variant": winner[1]}  # ga -> "a", gb -> "b"
check(f"组内赢的 {winner} 带配置",
      b["flags"][winner].get("config") == expected_cfg, b["flags"])
check(f"组内没争到的 {loser} 不带配置",
      "config" not in b["flags"][loser], b["flags"][loser])
check("单查与整包一致",
      check_flag(winner, "u9").get("config") == expected_cfg
      and "config" not in check_flag(loser, "u9"))
# 改输家的配置不影响此人（他对输家判关），改赢家的配置让他换版本
v9 = bundle("u9")["version"]
patch("gb", {"config": {"variant": "b2"}})
check("改组内输家的配置：此人版本不变", bundle("u9")["version"] == v9)
patch("ga", {"config": {"variant": "a2"}})
check("改组内赢家的配置：此人换版本且拿新配置",
      bundle("u9")["version"] != v9
      and bundle("u9")["flags"]["ga"].get("config") == {"variant": "a2"})

print("== 11. 依赖：被依赖开关关着时，依赖者不带配置 ==")
create("dep", default_enabled=True)
create("child", default_enabled=True)
patch("child", {"depends_on": "dep", "config": {"child": True}})
check("dep 开：child 开并带配置",
      check_flag("child", "u10").get("config") == {"child": True})
patch("dep", {"kill_switch": True})
r = check_flag("child", "u10")
check("dep 全关：child 判 depends_on 关、不带配置",
      r["enabled"] is False and r["reason"] == "depends_on"
      and "config" not in r, r)
patch("dep", {"kill_switch": False})

print("== 12. 属性包：配置随属性包走，各包独立版本 ==")
create("tgt", default_enabled=False)
patch("tgt", {"targeting": {"plan": "pro"}, "config": {"only": "pro-see-this"}})
r_free = bundle("u11", {"plan": "free"})
r_pro = bundle("u11", PRO)
check("对不上属性条件的包：tgt 关、不带配置",
      "config" not in r_free["flags"]["tgt"], r_free["flags"]["tgt"])
check("对得上属性条件的包：tgt 开、带配置",
      r_pro["flags"]["tgt"].get("config") == {"only": "pro-see-this"},
      r_pro["flags"]["tgt"])
# 改配置：属性包（判开）换版本，对不上的包与无属性包不换
patch("tgt", {"config": {"only": "pro-v2"}})
check("属性包换版本、拿到新配置",
      bundle("u11", PRO)["version"] != r_pro["version"]
      and bundle("u11", PRO)["flags"]["tgt"].get("config") == {"only": "pro-v2"})
check("对不上的属性包版本不变",
      bundle("u11", {"plan": "free"})["version"] == r_free["version"])
check("无属性包版本不变",
      bundle("u11")["version"] == bundle("u11")["version"])

print("== 13. 清配置 ==")
v_before = bundle("u1")["version"]
r = patch("fon", {"config": None})
check("清配置 200", r.status_code == 200 and r.get_json()["changed"] == 1, r.get_json())
check("清掉后开的人也不带 config", "config" not in check_flag("fon", "u1"))
check("清配置让判开的人换版本", bundle("u1")["version"] != v_before)
rows = c.get("/api/flags", headers=H).get_json()
check("列表里 config 回到 null",
      next(f for f in rows if f["name"] == "fon")["config"] is None)
r = patch("fon", {"config": None})
check("再清一次 changed=0", r.get_json()["changed"] == 0)
# 空对象 / 空数组也是合法配置（null 才是清除）
patch("fon", {"config": {}})
check("空对象是合法配置，开的人带 {}", check_flag("fon", "u1").get("config") == {})
patch("fon", {"config": None})

print("== 14. 定时生效：到点前不带、版本不动；到点后带、包过期 ==")
patch("fon", {"default_enabled": True})
v_before = bundle("u12")["version"]
patch("fon", {"config": {"sched": True},
              "effective_at": time.time() + 0.4})
check("到点前：配置不生效", "config" not in check_flag("fon", "u12"))
check("到点前：版本不动", bundle("u12")["version"] == v_before)
time.sleep(0.6)
r = check_flag("fon", "u12")
check("到点后：配置生效", r.get("config") == {"sched": True}, r)
check("到点后：整包换版本", bundle("u12")["version"] != v_before)
check("拿约时间前那包来问：过期",
      bundle("u12", version=v_before)["valid"] is False)
patch("fon", {"config": None})

print("== 15. 发布稿：发布前不带；发布时一起带上、整包换版本 ==")
did = c.post("/api/drafts", headers=H, json={"note": "挂配置一稿"}).get_json()["draft_id"]
v0 = bundle("u13")["version"]
r = c.put(f"/api/drafts/{did}/flags/fon", headers=H,
          json={"config": {"draft": 1}, "default_enabled": True})
check("稿能收 config", r.status_code == 200 and r.get_json()["changes"]["config"] == {"draft": 1})
check("发布前：稿不生效、不带配置", "config" not in check_flag("fon", "u13"))
check("发布前：版本不动", bundle("u13")["version"] == v0)
r = c.post(f"/api/drafts/{did}/publish", headers=H)
check("发布 200", r.status_code == 200, r.get_json())
check("发布后带上稿里的配置", check_flag("fon", "u13").get("config") == {"draft": 1})
check("发布后整包换版本、旧包过期",
      bundle("u13")["version"] != v0
      and bundle("u13", version=v0)["valid"] is False)
# 稿里清配置也行
did2 = c.post("/api/drafts", headers=H, json={"note": "清配置一稿"}).get_json()["draft_id"]
c.put(f"/api/drafts/{did2}/flags/fon", headers=H, json={"config": None})
c.post(f"/api/drafts/{did2}/publish", headers=H)
check("稿里把 config 收成 null：发布后清除",
      "config" not in check_flag("fon", "u13"))

print("== 16. 失效扫描记录的新版本 == 本人来拿 ==")
patch("fon", {"config": {"final": "v1"}})
b0 = bundle("u14")
patch("fon", {"config": {"final": "v2"}})
inv = invalidations("u14")
b1 = bundle("u14")
check("改配置：记录的新版本 == 本人来拿", inv[0]["new_version"] == b1["version"])
check("改配置：记录的旧版本 == 手里那包", inv[0]["old_version"] == b0["version"])

print("== 17. 老路不坏 ==")
r = c.get("/api/flags/fon/check")
check("check 缺 identity 仍 400", r.status_code == 400)
r = c.get("/api/bundle")
check("bundle 缺 identity 仍 400", r.status_code == 400)
r = c.get("/api/flags/nope/check?identity=u1")
check("未知开关 check 仍 404", r.status_code == 404)
r = patch("nope", {"config": {}})
check("改未知开关 404", r.status_code == 404)
check("配置接口需要鉴权",
      c.patch("/api/flags/fon", json={"config": {}}).status_code == 401)
# 审计里有 set_config / clear_config，layer=config
au = c.get("/api/audit?limit=500", headers=H).get_json()
check("审计里有 set_config（layer=config）",
      any(a["action"] == "set_config" and a["layer"] == "config" for a in au))
check("审计里有 clear_config（layer=config）",
      any(a["action"] == "clear_config" and a["layer"] == "config" for a in au))
# 单查与整包对每个开关的 enabled/reason/config 都一致（同口径：都不带属性）
r = bundle("u15")
ok = all(
    check_flag(name, "u15").get("enabled") == v["enabled"]
    and check_flag(name, "u15").get("reason") == v["reason"]
    and check_flag(name, "u15").get("config") == v.get("config")
    for name, v in r["flags"].items())
check("单查与整包：enabled/reason/config 三者一致", ok)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
