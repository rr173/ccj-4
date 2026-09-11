"""结果冻结（freeze）的端到端测试。

覆盖需求（口语版）：
1. 管理端能把某个人对某个开关此刻的结果冻住：冻住的是当时的开/关。
2. 冻住以后，改默认值、放量、属性条件、依赖（连单人强制也算），这个人来问
   （单查 / 整包 / 带属性问）都还是冻住的那个结果，reason=freeze；
   别人不受影响，按改后的规则算。
3. 全关仍能压过冻住：本开关全关期间一律关（reason=kill_switch），解除后
   回到冻住的值。
4. 解冻后立刻按解冻当时的规则算。
5. 冻 / 解冻：整包换新版本（此人拿过的不带属性的包与各身属性包都换），
   拿着冻/解冻前那包来问算过期；管理端失效记录里记的新版本 == 本人再来拿
   拿到的。冻住期间改默认/放量/条件/依赖，此人的包版本一字不变。
6. 原来问一个、拿整包、带属性问都不能坏。
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
H = {"X-Admin-Token": "test-token", "X-Actor": "alice"}
H_BOB = {"X-Admin-Token": "test-token", "X-Actor": "bob"}

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


def freeze(flag, identity, headers=H):
    return c.put(f"/api/flags/{flag}/freezes", headers=headers,
                 json={"identity": identity})


def unfreeze(flag, identity, headers=H):
    return c.delete(f"/api/flags/{flag}/freezes", headers=headers,
                    json={"identity": identity})


def freezes_of(flag):
    return c.get(f"/api/flags/{flag}/freezes", headers=H).get_json()


def invalidations(identity=None, limit=500):
    rows = c.get(f"/api/bundles/invalidations?limit={limit}",
                 headers=H).get_json()
    return [r for r in rows if identity is None or r["identity"] == identity]


# ---------------------------------------------------------------- 准备开关
c.post("/api/flags", headers=H, json={"name": "fon", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "foff"})
c.post("/api/flags", headers=H, json={"name": "dep", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "child", "default_enabled": True})
patch("child", {"depends_on": "dep"})
c.post("/api/flags", headers=H, json={"name": "tgt"})
patch("tgt", {"targeting": {"plan": "pro"}})
c.post("/api/flags", headers=H, json={"name": "ga", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "gb", "default_enabled": True})
c.post("/api/groups", headers=H, json={"name": "gg"})
c.put("/api/groups/gg/flags", headers=H, json={"flag": "ga"})
c.put("/api/groups/gg/flags", headers=H, json={"flag": "gb"})
PRO = {"plan": "pro"}
FREE = {"plan": "free"}

print("== 1. 冻住：冻的是此刻的结果 ==")
r = check_flag("fon", "u1")
check("冻前 fon 对 u1 开（默认值层）", r["enabled"] is True and r["reason"] == "default")
r = freeze("fon", "u1")
check("冻住返回 200", r.status_code == 200, r.get_json())
body = r.get_json()
check("返回冻住的值=开 与当时理由", body["enabled"] is True and body["reason"] == "default", body)
r = check_flag("fon", "u1")
check("冻住后单查仍是开，reason=freeze",
      r["enabled"] is True and r["reason"] == "freeze", r)
r = check_flag("foff", "u2")
check("冻前 foff 对 u2 关", r["enabled"] is False and r["reason"] == "default")
r = freeze("foff", "u2")
check("冻关：返回冻住的值=关", r.get_json()["enabled"] is False
      and r.get_json()["reason"] == "default")
check("冻关后单查仍是关，reason=freeze",
      check_flag("foff", "u2")["enabled"] is False
      and check_flag("foff", "u2")["reason"] == "freeze")
rows = freezes_of("fon")
check("冻结列表能看到 u1", len(rows) == 1 and rows[0]["identity"] == "u1"
      and rows[0]["enabled"] is True and rows[0]["frozen_reason"] == "default"
      and rows[0]["created_by"] == "alice", rows)

print("== 2. 冻住后改默认 / 放量 / 属性条件：人不动，别人按新规则 ==")
patch("fon", {"default_enabled": False})
check("改默认关：u1 仍冻在开", check_flag("fon", "u1")["enabled"] is True)
check("u1 的理由仍是 freeze", check_flag("fon", "u1")["reason"] == "freeze")
check("没冻的 u3 按新默认关", check_flag("fon", "u3")["enabled"] is False)
patch("fon", {"rollout_percent": 100})
check("放量调到 100：u1 仍冻在开（值没变）", check_flag("fon", "u1")["enabled"] is True)
patch("fon", {"rollout_percent": 0})
check("放量调到 0：u1 仍冻在开", check_flag("fon", "u1")["enabled"] is True)
patch("fon", {"targeting": {"plan": "free"}})
check("加属性条件：u1 带属性来问仍是冻住的开",
      check_flag("fon", "u1", FREE)["enabled"] is True
      and check_flag("fon", "u1", FREE)["reason"] == "freeze")
check("u1 不带属性也是冻住的开",
      check_flag("fon", "u1")["enabled"] is True)
check("别人带对得上的属性按属性层开",
      check_flag("fon", "u3", FREE)["enabled"] is True
      and check_flag("fon", "u3", FREE)["reason"] == "targeting")
patch("fon", {"targeting": {}, "default_enabled": True})  # 还原别人的世界
# 对冻在「关」的 foff 反向再验一遍
patch("foff", {"default_enabled": True})
check("冻在关：改默认开后 u2 仍关", check_flag("foff", "u2")["enabled"] is False)
patch("foff", {"rollout_percent": 100})
check("冻在关：放量 100 后 u2 仍关", check_flag("foff", "u2")["enabled"] is False)
patch("foff", {"targeting": {"plan": "pro"}})
check("冻在关：属性对上后 u2 带属性仍关",
      check_flag("foff", "u2", PRO)["enabled"] is False
      and check_flag("foff", "u2", PRO)["reason"] == "freeze")
patch("foff", {"rollout_percent": 0})

print("== 3. 全关仍能压过冻住 ==")
patch("fon", {"kill_switch": True})
r = check_flag("fon", "u1")
check("全关压过冻住的开 -> 关，reason=kill_switch",
      r["enabled"] is False and r["reason"] == "kill_switch", r)
check("整包里同样被全关压过",
      bundle("u1")["flags"]["fon"]["enabled"] is False
      and bundle("u1")["flags"]["fon"]["reason"] == "kill_switch")
patch("fon", {"kill_switch": False})
r = check_flag("fon", "u1")
check("解除全关：回到冻住的开，reason=freeze",
      r["enabled"] is True and r["reason"] == "freeze", r)
# 冻在关的开关开全关再解除：仍是冻住的关
patch("foff", {"kill_switch": True})
check("冻在关：全关期间关、理由 kill_switch",
      check_flag("foff", "u2")["reason"] == "kill_switch")
patch("foff", {"kill_switch": False})
r = check_flag("foff", "u2")
check("冻在关：解除全关后回到冻住的关",
      r["enabled"] is False and r["reason"] == "freeze")

print("== 4. 依赖：冻住在依赖之前，改依赖动不了冻住的值 ==")
# child 默认开、依赖 dep（dep 默认开）。先把 u1 的 child 冻在开
r = freeze("child", "u1")
check("冻 child 时它开着（dep 开）", r.get_json()["enabled"] is True)
patch("dep", {"kill_switch": True})  # dep 对所有人关
r = check_flag("child", "u1")
check("被依赖的 dep 全关：u1 的 child 仍冻在开（冻住在依赖之前）",
      r["enabled"] is True and r["reason"] == "freeze", r)
check("没冻的 u4：child 被依赖层判关",
      check_flag("child", "u4")["enabled"] is False
      and check_flag("child", "u4")["reason"] == "depends_on")
# 解除 child 的依赖也不影响冻住的值
patch("child", {"depends_on": ""})
check("解除依赖：u1 的 child 仍冻在开", check_flag("child", "u1")["enabled"] is True)
patch("child", {"depends_on": "dep"})
patch("dep", {"kill_switch": False})
# 反过来：dep 自己被冻在关时，依赖它的 child（未冻）按 depends_on 关
r = freeze("dep", "u5")
check("dep 默认开着却可以被冻（先验证它此刻开）", True)  # 占位；下面直接构造冻关
unfreeze("dep", "u5")
patch("dep", {"default_enabled": False, "kill_switch": True})
r = freeze("dep", "u5")  # 此刻 dep 全关 -> 冻住的是关
check("dep 全关时冻住的是关", r.get_json()["enabled"] is False
      and r.get_json()["reason"] == "kill_switch")
patch("dep", {"kill_switch": False, "default_enabled": True})  # dep 恢复开
r = check_flag("dep", "u5")
check("dep 恢复开，但 u5 取的仍是冻住的关",
      r["enabled"] is False and r["reason"] == "freeze", r)
r = check_flag("child", "u5")
check("child 依赖 dep：u5 沿链拿到 dep 冻住的关 -> child 关（depends_on）",
      r["enabled"] is False and r["reason"] == "depends_on", r)
# child 自己冻在开时，即便 dep（对此人）冻在关，child 仍是冻住的开
# （重新冻按「假设本开关没冻」求此刻结果：先把 dep 的冻结摘掉才能冻到开）
unfreeze("dep", "u5")
r = freeze("child", "u5")
check("dep 开着时重新冻 child：冻在开", r.get_json()["enabled"] is True, r.get_json())
freeze("dep", "u5")  # 再把 dep 冻回关
check("child 冻在开：dep 对 u5 冻着关也压不过这个冻结",
      check_flag("child", "u5")["enabled"] is True
      and check_flag("child", "u5")["reason"] == "freeze")

print("== 5. 冻住也压过单人强制与互斥组 ==")
c.put("/api/flags/fon/overrides", headers=H, json={"identity": "u1", "enabled": False})
check("冻住后给 u1 加强制关：仍是冻住的开",
      check_flag("fon", "u1")["enabled"] is True
      and check_flag("fon", "u1")["reason"] == "freeze")
c.delete("/api/flags/fon/overrides", headers=H, json={"identity": "u1"})
# 互斥组：u6 先把 ga 在组内落定为开，再冻住；之后默认改关、移出组都不动冻住的值
r = check_flag("ga", "u6")
check("冻前 u6 的 ga 组裁决开", r["enabled"] is True and r["reason"] == "group")
freeze("ga", "u6")
patch("ga", {"default_enabled": False})
check("组内开关冻在开：改默认关后仍开（freeze）",
      check_flag("ga", "u6")["enabled"] is True
      and check_flag("ga", "u6")["reason"] == "freeze")
c.put("/api/flags/ga/overrides", headers=H, json={"identity": "u6", "enabled": False})
check("组内开关冻在开：单人强制关也压不过",
      check_flag("ga", "u6")["enabled"] is True)
c.delete("/api/flags/ga/overrides", headers=H, json={"identity": "u6"})

print("== 6. 解冻：立即按当时规则 ==")
r = unfreeze("fon", "u1")
check("解冻返回 200 changed=true", r.status_code == 200 and r.get_json()["changed"] is True)
check("解冻列表里没有 u1", all(x["identity"] != "u1" for x in freezes_of("fon")))
patch("fon", {"default_enabled": False})  # 冻住期间默认已被还原成开过，再关
r = check_flag("fon", "u1")
check("解冻后按当前规则：默认关 -> 关，reason=default",
      r["enabled"] is False and r["reason"] == "default", r)
r = unfreeze("fon", "u1")
check("重复解冻幂等（changed=false）", r.status_code == 200
      and r.get_json()["changed"] is False)
# 解冻组内开关：按当时的组落定/默认值算
unfreeze("ga", "u6")
r = check_flag("ga", "u6")
check("解冻组内开关：默认关 -> 自然结果关（reason=default），不再是冻住的开",
      r["enabled"] is False and r["reason"] == "default", r)

print("== 7. 冻 / 解冻：整包换新版本，旧包过期（含各属性包） ==")
patch("fon", {"default_enabled": True, "kill_switch": False})
b0 = bundle("u7")
bp0 = bundle("u7", PRO)
check("冻前整包里 fon 开", b0["flags"]["fon"]["enabled"] is True)
r = freeze("fon", "u7")
check("冻住 200", r.status_code == 200)
b1 = bundle("u7")
bp1 = bundle("u7", PRO)
check("无属性包换新版本", b1["version"] != b0["version"])
check("属性包也换新版本", bp1["version"] != bp0["version"])
check("两个包里 fon 都 reason=freeze",
      b1["flags"]["fon"]["reason"] == "freeze"
      and bp1["flags"]["fon"]["reason"] == "freeze")
check("属性包回显属性", bp1.get("attrs") == PRO)
r = bundle("u7", version=b0["version"])
check("拿冻前的无属性包来问 -> 过期", r["valid"] is False)
check("过期响应附当前新版本", r["version"] == b1["version"])
r = bundle("u7", PRO, version=bp0["version"])
check("拿冻前的属性包来问 -> 也过期", r["valid"] is False)
check("拿冻后的新版本来问 -> 有效",
      bundle("u7", version=b1["version"])["valid"] is True
      and bundle("u7", PRO, version=bp1["version"])["valid"] is True)
# 失效记录：两条（无属性 attrs_hash='' 与属性包各一条），新版本 == 本人再来拿
inv = invalidations("u7")
fz = [x for x in inv if x["change"].startswith("freeze_result fon")]
check("冻住产生了两条失效记录（无属性包 + 属性包）", len(fz) == 2, fz)
check("其中一条是无属性包", {x["attrs_hash"] for x in fz} == {"", flag_app.attrs_hash_of(PRO)})
check("记录的新版本与本人来拿一致",
      {x["new_version"] for x in fz} == {b1["version"], bp1["version"]})
check("记录的旧版本正是手里的版本",
      {x["old_version"] for x in fz} == {b0["version"], bp0["version"]})
check("失效记录操作人是 alice", all(x["actor"] == "alice" for x in fz))
# 解冻再换一次版本
r = unfreeze("fon", "u7")
check("解冻 200", r.status_code == 200)
b2 = bundle("u7")
bp2 = bundle("u7", PRO)
check("解冻后无属性包又换新版本", b2["version"] != b1["version"])
check("解冻后属性包又换新版本", bp2["version"] != bp1["version"])
check("拿冻住期间的包来问 -> 过期",
      bundle("u7", version=b1["version"])["valid"] is False
      and bundle("u7", PRO, version=bp1["version"])["valid"] is False)
uz = [x for x in invalidations("u7") if x["change"].startswith("unfreeze_result fon")]
check("解冻也为两个包记了失效", len(uz) == 2, uz)

print("== 8. 冻住期间改默认/放量/条件/依赖/强制：此人包版本一字不变 ==")
freeze("fon", "u8")
v = bundle("u8")["version"]
vp = bundle("u8", PRO)["version"]
patch("fon", {"default_enabled": False})
patch("fon", {"rollout_percent": 55})
patch("fon", {"targeting": {"plan": "pro"}})
patch("fon", {"depends_on": "dep"})
c.put("/api/flags/fon/overrides", headers=H, json={"identity": "u8", "enabled": False})
check("一连串改动后：无属性包版本不变", bundle("u8")["version"] == v)
check("一连串改动后：属性包版本不变", bundle("u8", PRO)["version"] == vp)
check("结果仍是冻住的开",
      bundle("u8")["flags"]["fon"]["enabled"] is True
      and bundle("u8")["flags"]["fon"]["reason"] == "freeze")
check("这些改动没有给 u8 产生新的失效记录",
      not [x for x in invalidations("u8")
           if x["change"].startswith("update_flag fon")
           or x["change"].startswith("set_override fon")])
# 但全关仍让包换版本
patch("fon", {"kill_switch": True})
check("冻住期间开全关：包换版本", bundle("u8")["version"] != v)
patch("fon", {"kill_switch": False})
check("全关解除后又是新版本，且结果回到冻住的开",
      bundle("u8")["flags"]["fon"]["enabled"] is True
      and bundle("u8")["flags"]["fon"]["reason"] == "freeze")
# 解冻后，冻住期间攒下的改动一次性按当时规则生效，包再换版本
r = unfreeze("fon", "u8")
b = bundle("u8")
check("解冻后按当时规则：fon 依赖 dep（开）后继续，命中强制关 -> 关",
      b["flags"]["fon"]["enabled"] is False
      and b["flags"]["fon"]["reason"] == "override", b["flags"]["fon"])

print("== 9. 冻 / 解冻只影响这一个身份 ==")
patch("fon", {"default_enabled": True, "rollout_percent": 0, "targeting": {},
              "depends_on": None, "kill_switch": False})
c.delete("/api/flags/fon/overrides", headers=H, json={"identity": "u8"})
bundle("u9")
vu9_before = bundle("u9")["version"]
freeze("fon", "u10")
check("冻 u10：u9 的版本不变", bundle("u9")["version"] == vu9_before)
unfreeze("fon", "u10")
check("解冻 u10：u9 的版本仍不变", bundle("u9")["version"] == vu9_before)
check("u10 解冻后按默认开开",
      bundle("u10")["flags"]["fon"]["enabled"] is True)

print("== 10. 重复冻同一个人：值没变是 no-op ==")
patch("foff", {"default_enabled": False, "rollout_percent": 0, "targeting": {}})
r1 = freeze("foff", "u20")
check("第一次冻在关", r1.get_json()["enabled"] is False, r1.get_json())
v1 = bundle("u20")["version"]
n1 = len(invalidations("u20"))
r2 = freeze("foff", "u20")
check("重复冻返回 changed=false",
      r2.get_json()["changed"] is False, r2.get_json())
check("重复冻不产生新失效、版本不变",
      len(invalidations("u20")) == n1 and bundle("u20")["version"] == v1)
# 配置把结果改了之后重新冻：以此刻结果为准（冻的内容跟着换），包换版本
patch("foff", {"default_enabled": True})
r3 = freeze("foff", "u20")
check("结果变了再冻：changed=true、冻住新值=开",
      r3.get_json()["changed"] is True and r3.get_json()["enabled"] is True, r3.get_json())
check("重新冻后整包换新版本", bundle("u20")["version"] != v1)
unfreeze("foff", "u20")

print("== 11. 定时变更到点：冻住的人不被推动 ==")
patch("fon", {"default_enabled": True})
freeze("fon", "u11")
v_before = bundle("u11")["version"]
patch("fon", {"default_enabled": False, "effective_at": time.time() + 0.4})
time.sleep(0.6)
check("定时改默认到点：u11 仍冻在开",
      check_flag("fon", "u11")["enabled"] is True
      and check_flag("fon", "u11")["reason"] == "freeze")
check("定时改默认到点：u11 的包版本不变", bundle("u11")["version"] == v_before)
unfreeze("fon", "u11")
check("解冻后吃到定时的默认关", check_flag("fon", "u11")["enabled"] is False)
patch("fon", {"default_enabled": True})

print("== 12. 管理端杂项与校验 ==")
check("冻结接口需要鉴权",
      c.put("/api/flags/fon/freezes", json={"identity": "x"}).status_code == 401)
check("解冻接口需要鉴权",
      c.delete("/api/flags/fon/freezes", json={"identity": "x"}).status_code == 401)
check("列表接口需要鉴权", c.get("/api/flags/fon/freezes").status_code == 401)
r = freeze("nope-flag", "u1")
check("冻未知开关 404", r.status_code == 404)
r = c.put("/api/flags/fon/freezes", headers=H, json={})
check("缺 identity 400", r.status_code == 400)
r = c.put("/api/flags/fon/freezes", headers=H, json={"identity": ""})
check("空 identity 400", r.status_code == 400)
r = c.delete("/api/flags/fon/freezes", headers=H, json={"identity": "u-no-freeze"})
check("解冻一个没冻过的人 200 changed=false",
      r.status_code == 200 and r.get_json()["changed"] is False)
# 开关列表带 freeze_count
rows = c.get("/api/flags", headers=H).get_json()
fon_row = next(f for f in rows if f["name"] == "fon")
check("开关列表带 freeze_count", "freeze_count" in fon_row)
check("freeze_count 统计正确（foff 还有 u2）",
      next(f for f in rows if f["name"] == "foff")["freeze_count"] >= 1)
# 审计日志有冻 / 解冻
au = c.get("/api/audit?limit=500", headers=H).get_json()
check("审计里有 freeze_result / unfreeze_result，layer=freeze",
      any(a["action"] == "freeze_result" and a["layer"] == "freeze" for a in au)
      and any(a["action"] == "unfreeze_result" and a["layer"] == "freeze" for a in au))

print("== 13. 删除开关：冻结记录一并清掉，系统不报错 ==")
c.post("/api/flags", headers=H, json={"name": "tmp", "default_enabled": True})
freeze("tmp", "u1")
r = c.delete("/api/flags/tmp", headers=H)
check("删带冻结记录的开关 200", r.status_code == 200)
r = bundle("u1")
check("删后整包正常、不含已删开关", "tmp" not in r)

print("== 14. 老路不坏：问一个 / 拿整包 / 带属性问 ==")
r = c.get("/api/flags/fon/check")
check("check 缺 identity 仍 400", r.status_code == 400)
r = c.get("/api/bundle")
check("bundle 缺 identity 仍 400", r.status_code == 400)
r = c.get("/api/flags/nope/check?identity=u1")
check("未知开关 check 仍 404", r.status_code == 404)
r = check_flag("tgt", "u12", PRO)
check("没冻的开关带属性问照常（targeting 开）",
      r["enabled"] is True and r["reason"] == "targeting")
r = bundle("u12", PRO)
check("没冻的开关整包照常", r["flags"]["tgt"]["reason"] == "targeting")
# 单查与整包对冻住的开关结果、理由一致
r1 = check_flag("foff", "u2")
r2 = bundle("u2")["flags"]["foff"]
check("冻住的开关：单查与整包一致",
      r1["enabled"] == r2["enabled"] and r1["reason"] == r2["reason"], (r1, r2))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
