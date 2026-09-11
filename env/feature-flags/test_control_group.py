"""对照组（control group）的端到端测试。

覆盖需求（口语版）：
1. 管理端能点名一拨人进对照：PUT /api/control-group {"identities":[…]}，至少
   一个；少写了 / 不是字符串列表 / 空串 / 重复，这次点不成（400）并说清楚，
   一个人都不写库。
2. 进了的人来问时不再按人分开算：冻结、单人强制、属性条件、放量、定档、
   互斥组、依赖一概不看，每个开关只走它自己的默认开或关（reason=control）；
   没有档名（variant）。全关仍然压过对照（reason=kill_switch）。
3. 没进对照组的人还按各自开关原来的算法算（放量 / 强制 / 冻结 / 属性都在）。
4. 同一人进没进，多次来问（check / 整包 / 带不带属性）结果恒定。
5. 改了谁在对照里（加入 / 移出 / 换名单 / 清空）再来问按新的算；移出后原来
   的冻结 / 强制立刻恢复作用；相关已发整包换新版本，旧版本过期；无关的人
   版本一字不变。
6. 同一份名单再点一次是幂等 no-op（changed=false，不换版本）。
7. 历史重放：进对照前按原算法，对照期只走默认值，移出后又按原算法。
8. 身份合并：给别名点名 / 拨内有人在对照里，合并后拨内任一身份来问一致。
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
    return r.status_code, (r.get_json() if r.status_code == 200 else r.get_json())


def bundle(identity, attrs=None, version=None):
    r = c.get(f"/api/bundle?{q(identity, attrs, version)}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def create(name, default=False, **kw):
    body = {"name": name, "default_enabled": default}
    body.update(kw)
    r = c.post("/api/flags", headers=H, json=body)
    assert r.status_code in (200, 201), r.get_json()


def patch(name, **kw):
    r = c.patch(f"/api/flags/{name}", headers=H, json=kw)
    assert r.status_code == 200, r.get_json()


def override(flag, identity, enabled):
    r = c.put(f"/api/flags/{flag}/overrides", headers=H,
              json={"identity": identity, "enabled": enabled})
    assert r.status_code == 200, r.get_json()


def freeze(flag, identity):
    r = c.put(f"/api/flags/{flag}/freezes", headers=H, json={"identity": identity})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def unfreeze(flag, identity):
    r = c.delete(f"/api/flags/{flag}/freezes", headers=H,
                 json={"identity": identity})
    assert r.status_code == 200, r.get_json()


def control_put(idents, headers=H):
    return c.put("/api/control-group", headers=headers, json={"identities": idents})


def control_get():
    r = c.get("/api/control-group", headers=H)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def control_delete(idents=None):
    return c.delete("/api/control-group", headers=H,
                    json={"identities": idents} if idents is not None else None)


def hist(identity, at, flag="f-default-on", attrs=None):
    r = c.get(f"/api/history?{q(identity, attrs)}&at={at}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["flags"][flag]


# ---------------------------------------------------------------- 造一批开关

# 默认开 / 默认关各一个
create("f-default-on", default=True)
create("f-default-off", default=False)
# 放量 50%：有人命中开、有人不命中关（结果因人而异）
create("f-rollout", default=False)
patch("f-rollout", rollout_percent=50)
# 属性条件：plan=pro 才开
create("f-target", default=False, targeting={"plan": "pro"})
# 定档：所有人开、带档名
create("f-variants", default=False,
       variants=[{"name": "control", "percent": 50},
                 {"name": "treat", "percent": 50}])
# 挂了配置的默认开开关：判开时带 config
create("f-config", default=True, config={"color": "red"})
# 依赖 f-default-on：被依赖者关 -> 本开关关
create("f-dep", default=True)
patch("f-dep", depends_on="f-default-on")

# 找一个对 f-rollout 命中开、一个不命中关的普通人
rollout_hit = next(i for i in range(500)
                   if flag_app.bucket_of("f-rollout", f"hit-{i}") < 50)
rollout_miss = next(i for i in range(500)
                    if flag_app.bucket_of("f-rollout", f"miss-{i}") >= 50)
HIT, MISS = f"hit-{rollout_hit}", f"miss-{rollout_miss}"
st, j = check_flag("f-rollout", HIT)
check("基线：放量命中者开", j["enabled"] is True and j["reason"] == "rollout", j)
st, j = check_flag("f-rollout", MISS)
check("基线：放量未命中者关", j["enabled"] is False and j["reason"] == "rollout", j)
st, j = check_flag("f-variants", HIT)
check("基线：定档开关开且带档名", j["enabled"] is True and j.get("variant") in
      ("control", "treat"), j)

# 给 HIT 下单人强制关、冻住在关——之后用来验证对照层压过它们
override("f-default-on", HIT, False)
st, j = check_flag("f-default-on", HIT)
check("基线：HIT 的单人强制关生效", j["enabled"] is False and j["reason"] == "override", j)
fr = freeze("f-default-off", HIT)
check("基线：HIT 冻住 f-default-off（默认关，冻在关）", fr["enabled"] is False, fr)
# 再给 f-default-on 建个互斥组，验证对照组不参与组规则
r = c.post("/api/groups", headers=H, json={"name": "g1"})
assert r.status_code in (200, 201), r.get_json()
r = c.put("/api/groups/g1/flags", headers=H, json={"flag": "f-default-on"})
assert r.status_code == 200, r.get_json()

# 进对照前最后拿一次 HIT 的包（拿着它验证进对照后过期）
hit_pre_version = bundle(HIT)["version"]

# ---------------------------------------------------------------- 校验：少写了点不成

st, j = control_put([]).status_code, control_put([]).get_json()
check("空名单 400", st == 400, (st, j))
check("空名单说清楚", isinstance(j.get("error"), str) and "at least 1" in j["error"], j)
st2, j2 = control_put(None).status_code, control_put(None).get_json()
check("identities=null 400", st2 == 400, (st2, j2))
for bad, label in ((["u1", 3], "混了非字符串"),
                   (["", "u2"], "空字符串"),
                   (["u1", "u1"], "重复")):
    r = control_put(bad)
    check(f"{label} 400 并说清楚", r.status_code == 400 and "error" in r.get_json(),
          (r.status_code, r.get_json()))
r = control_put("not-a-list")
check("identities 不是列表 400", r.status_code == 400, r.get_json())
check("点不成时一个人都没写库", control_get()["identities"] == [], control_get())

# 鉴权
r = c.put("/api/control-group", json={"identities": ["u1"]})
check("无 Token 401", r.status_code == 401, r.status_code)

# ---------------------------------------------------------------- 点名进对照

r = control_put([HIT, MISS])
check("点名成功 200", r.status_code == 200, r.get_json())
body = r.get_json()
check("返回新名单与增删", body["changed"] is True and body["identities"] ==
      sorted([HIT, MISS]) and body["added"] == sorted([HIT, MISS]) and
      body["removed"] == [], body)
check("GET 能看到名单", control_get()["identities"] == sorted([HIT, MISS]))

# 进了的人：每个开关只走默认值
st, j = check_flag("f-default-on", HIT)
check("对照：默认开的开关就是开（强制关被压过）",
      j["enabled"] is True and j["reason"] == "control", j)
st, j = check_flag("f-default-off", HIT)
check("对照：默认关的开关就是关（冻在关也不再以 freeze 给）",
      j["enabled"] is False and j["reason"] == "control", j)
st, j = check_flag("f-rollout", HIT)
check("对照：放量命中者不看比例，走默认关",
      j["enabled"] is False and j["reason"] == "control", j)
st, j = check_flag("f-rollout", MISS)
check("对照：放量未命中者也走默认关，与 HIT 不再按人分开",
      j["enabled"] is False and j["reason"] == "control", j)
st, j = check_flag("f-target", HIT, {"plan": "pro"})
check("对照：属性对上也不看属性条件，走默认关",
      j["enabled"] is False and j["reason"] == "control", j)
st, j = check_flag("f-variants", HIT)
check("对照：定档开关不落档（无 variant，走默认关）",
      j["enabled"] is False and j["reason"] == "control" and
      "variant" not in j, j)
st, j = check_flag("f-config", HIT)
check("对照：默认开带配置（与默认值层同口径，判开带 config）",
      j["enabled"] is True and j["reason"] == "control" and
      j.get("config") == {"color": "red"}, j)
st, j = check_flag("f-dep", HIT)
check("对照：不看依赖（被依赖者对 HIT 此刻是强制关），本开关走自己的默认开",
      j["enabled"] is True and j["reason"] == "control", j)

# 全关仍压过对照
patch("f-default-on", kill_switch=True)
st, j = check_flag("f-default-on", HIT)
check("全关压过对照：reason=kill_switch",
      j["enabled"] is False and j["reason"] == "kill_switch", j)
st, j = check_flag("f-default-on", "plain-bob")
check("全关对所有人关", j["enabled"] is False and j["reason"] == "kill_switch", j)
patch("f-default-on", kill_switch=False)

# 多次来问恒定（check 与整包、带不带属性）
b1 = bundle(HIT)
b2 = bundle(HIT)
check("同一人多次拿整包版本一致", b1["version"] == b2["version"])
b_attrs = bundle(HIT, {"plan": "pro"})
check("对照结果与属性无关：带属性 reason 仍是 control",
      all(v["reason"] != "targeting" for v in b_attrs["flags"].values()) and
      b_attrs["flags"]["f-default-on"]["reason"] == "control",
      b_attrs["flags"]["f-default-on"])
for _ in range(3):
    st, j = check_flag("f-rollout", HIT)
    check_again = (j["enabled"] is False and j["reason"] == "control")
    assert check_again
check("同一人多次 check 恒定", True)
st1, j1 = check_flag("f-default-on", HIT)
st2, j2 = check_flag("f-default-on", HIT)
check("check 两次一字不差", j1 == j2, (j1, j2))

# 整包口径
check("整包：对照成员 f-default-on 为 control 开",
      b1["flags"]["f-default-on"]["enabled"] is True and
      b1["flags"]["f-default-on"]["reason"] == "control", b1["flags"]["f-default-on"])
check("整包：对照成员 f-variants 无 variant",
      "variant" not in b1["flags"]["f-variants"], b1["flags"]["f-variants"])

# ---------------------------------------------------------------- 没进的人照旧

st, j = check_flag("f-rollout", "plain-carol")
# carol 落哪侧都行，但 reason 必须是 rollout（原算法）
check("没进的人：放量层照常（rollout）", j["reason"] == "rollout", j)
st, j = check_flag("f-target", "plain-carol", {"plan": "pro"})
check("没进的人：属性条件照常开", j["enabled"] is True and j["reason"] == "targeting", j)
st, j = check_flag("f-variants", "plain-carol")
check("没进的人：定档照常开并带档名",
      j["enabled"] is True and j.get("variant") in ("control", "treat"), j)
st, j = check_flag("f-dep", "plain-carol")
# f-default-on 对 carol 默认开 -> 继续算 -> f-dep 默认开
check("没进的人：依赖照常（被依赖开，本开关按自己规则）",
      j["enabled"] is True and j["reason"] in ("default", "group"), j)

# ---------------------------------------------------------------- 改名单：按新的算 + 版本

# 别人的旧包
bob_before = bundle("plain-carol")
# 把 MISS 移出，HIT 留着，再加入 plain-dave
r = control_put([HIT, "plain-dave"])
check("换名单 200，增删正确", r.status_code == 200 and
      r.get_json()["added"] == ["plain-dave"] and
      r.get_json()["removed"] == [MISS], r.get_json())

st, j = check_flag("f-rollout", MISS)
check("移出后 MISS 恢复原算法（rollout）", j["reason"] == "rollout", j)
st, j = check_flag("f-default-off", MISS)
check("移出后 MISS 的 f-default-off 回到默认关 default",
      j["enabled"] is False and j["reason"] == "default", j)
# HIT 还在对照里：默认开（强制关仍被压）
st, j = check_flag("f-default-on", HIT)
check("还在对照里的 HIT 仍走默认开 control",
      j["enabled"] is True and j["reason"] == "control", j)
# dave 新进对照
st, j = check_flag("f-variants", "plain-dave")
check("新进的 dave 走默认值（定档被跳过）",
      j["enabled"] is False and j["reason"] == "control" and "variant" not in j, j)

# 移出后原来的按人状态立刻恢复作用：给 MISS 下强制开
override("f-default-off", MISS, True)
st, j = check_flag("f-default-off", MISS)
check("移出后单人强制对 MISS 生效",
      j["enabled"] is True and j["reason"] == "override", j)
# 但 HIT 在对照里，给他改强制不影响对照结果
override("f-default-off", HIT, True)
st, j = check_flag("f-default-off", HIT)
check("对照成员的强制不参与求值（仍 control 关）",
      j["enabled"] is False and j["reason"] == "control", j)

# 版本：拿着 HIT 进对照前那包来问 -> valid=false
b_hit_now = bundle(HIT)
check("进对照后 HIT 的包版本变了",
      b_hit_now["version"] != hit_pre_version,
      (hit_pre_version, b_hit_now["version"]))
check("拿旧版本来问 valid=false",
      bundle(HIT, version=hit_pre_version)["valid"] is False)
# 别人（plain-carol）的包版本一字不变
bob_after = bundle("plain-carol")
check("无关的人版本不变",
      bob_before["version"] == bob_after["version"],
      (bob_before["version"], bob_after["version"]))
check("无关的人拿旧版本仍 valid=true",
      bundle("plain-carol", version=bob_before["version"])["valid"] is True)

# 失效记录里能看到这次名单改动波及了谁
r = c.get("/api/bundles/invalidations?limit=20", headers=H)
assert r.status_code == 200, r.get_json()
changes = {inv["change"] for inv in r.get_json()["invalidations"]} \
    if isinstance(r.get_json(), dict) and "invalidations" in r.get_json() \
    else {inv.get("change", "") for inv in r.get_json()}
check("失效记录里记的是对照组名单改动",
      any("control_group" in ch for ch in changes), sorted(changes)[:5])

# ---------------------------------------------------------------- 幂等

v_dave = bundle("plain-dave")["version"]
r = control_put([HIT, "plain-dave"])
check("同一名单再点 changed=false", r.status_code == 200 and
      r.get_json()["changed"] is False, r.get_json())
check("幂等 no-op 不换 dave 的版本", bundle("plain-dave")["version"] == v_dave)

# 名单换个书写顺序仍是同一名单：幂等
v_hit_2 = bundle(HIT)["version"]
r = control_put(["plain-dave", HIT])
check("同一名单换书写顺序 changed=false", r.status_code == 200 and
      r.get_json()["changed"] is False, r.get_json())
check("换序幂等不换版本", bundle(HIT)["version"] == v_hit_2)

# ---------------------------------------------------------------- DELETE

r = control_delete(["plain-dave"])
check("DELETE 移出 dave", r.status_code == 200 and
      r.get_json()["removed"] == ["plain-dave"], r.get_json())
st, j = check_flag("f-variants", "plain-dave")
check("DELETE 后 dave 恢复定档", j["reason"] == "variants" and j["enabled"] is True, j)
r = control_delete(["plain-dave"])
check("再删一次 no-op changed=false", r.status_code == 200 and
      r.get_json()["changed"] is False, r.get_json())
r = control_delete(["nope", HIT])
check("混着删：只删在名单里的",
      r.status_code == 200 and r.get_json()["removed"] == [HIT], r.get_json())
st, j = check_flag("f-default-on", HIT)
check("HIT 移出后强制关恢复（override）",
      j["enabled"] is False and j["reason"] == "override", j)
r = control_put(["temp-1", "temp-2"])
assert r.status_code == 200
r = control_delete()
check("清空整拨", r.status_code == 200 and
      set(r.get_json()["removed"]) == {"temp-1", "temp-2"}, r.get_json())
check("清空后名单为空", control_get()["identities"] == [])
r = control_delete()
check("空名单再清空 changed=false", r.status_code == 200 and
      r.get_json()["changed"] is False, r.get_json())
r = control_delete([""])
check("DELETE 里有空串 400", r.status_code == 400 and "error" in r.get_json(), r.get_json())

# ---------------------------------------------------------------- 历史重放

# 在干净名单下重放一段：进对照前（强制关生效）→ 对照期（只走默认值）→ 移出
control_delete()
override("f-default-on", "hist-u", False)
time.sleep(0.02)
t0 = time.time()
time.sleep(0.02)
t_add = time.time()
time.sleep(0.01)
control_put(["hist-u"])
time.sleep(0.02)
t_during = time.time()
time.sleep(0.01)
control_delete(["hist-u"])
time.sleep(0.02)
t_end = time.time()

before = hist("hist-u", (t0 + t_add) / 2)
check("历史：进对照前按强制算（关/override）",
      before["enabled"] is False and before["reason"] == "override", before)
during = hist("hist-u", (t_add + t_during) / 2)
check("历史：对照期只走默认开（control）",
      during["enabled"] is True and during["reason"] == "control", during)
after = hist("hist-u", (t_during + t_end) / 2)
check("历史：移出后强制恢复（关/override）",
      after["enabled"] is False and after["reason"] == "override", after)

# 当前时刻重放与线上一致
now_j = hist("hist-u", time.time() - 0.001)
check("历史：现在移出后与线上一致（override）",
      now_j["reason"] == "override", now_j)

# ---------------------------------------------------------------- 身份合并

# 给别名点名：merge-a 与 merge-b 收成同一个人，点 merge-b 进对照
r = c.post("/api/identities/merges", headers=H,
           json={"identities": ["merge-a", "merge-b"]})
assert r.status_code in (200, 201), r.get_json()
r = control_put(["merge-b"])
check("给合并拨的别名点名成功（归主身份 merge-a）", r.status_code == 200, r.get_json())
check("名单里存的是主身份", control_get()["identities"] == ["merge-a"],
      control_get())
st_ja = check_flag("f-default-on", "merge-a")
st_jb = check_flag("f-default-on", "merge-b")
check("合并拨内任一身份来问都是 control（除 identity 回显外一字不差）",
      st_ja[1]["reason"] == "control" and st_jb[1]["reason"] == "control" and
      {k: v for k, v in st_ja[1].items() if k != "identity"} ==
      {k: v for k, v in st_jb[1].items() if k != "identity"}, (st_ja, st_jb))
# 主身份口径重复校验
r = control_put(["merge-a", "merge-b"])
check("同拨两个别名一起点=重复（400），说清楚",
      r.status_code == 400 and "error" in r.get_json(), r.get_json())

# 拆开：留在主身份 merge-a 名下，merge-b 恢复各算各的
mgroups = c.get("/api/identities/merges", headers=H).get_json()
gid = next(g["id"] for g in mgroups if g["primary"] == "merge-a")
r = c.delete(f"/api/identities/merges/{gid}", headers=H)
assert r.status_code == 200, r.get_json()
st, ja = check_flag("f-default-on", "merge-a")
st, jb = check_flag("f-default-on", "merge-b")
check("拆开后主身份仍在对照（control），别名恢复原算法",
      ja["reason"] == "control" and jb["reason"] != "control", (ja, jb))

# 合并时把「已在对照里的人」带进拨：ctrl-x 在对照里，ctrl-y 不在，收成一拨
control_put(["ctrl-x"])
r = c.post("/api/identities/merges", headers=H,
           json={"identities": ["ctrl-x", "ctrl-y"]})
assert r.status_code in (200, 201), r.get_json()
st, jx = check_flag("f-default-on", "ctrl-x")
st, jy = check_flag("f-default-on", "ctrl-y")
check("合并带对照归属：拨内任一身份都是 control",
      jx["reason"] == "control" and jy["reason"] == "control", (jx, jy))

print()
print(f"{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
