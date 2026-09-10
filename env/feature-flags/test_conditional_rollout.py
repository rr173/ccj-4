"""条件按比例放量（rollout_condition）的端到端测试。

覆盖需求（口语版）：
1. 管理端能说：这个开关只对对上条件的人按比例放量——对上的人里按比例开
   一部分、剩下的关；没对上的（含没带属性来问的）还按这个开关原来的默认走。
2. 同一个人、同一身属性，多次来问结果不变（key 顺序也无关）。
3. 全关、冻住、依赖、单人强制还是压过它。
4. 改了比例或条件，整包换新版本，拿着改前那包来问说过期。
5. 原来问现在开没开、一次拿走所有开关、带上属性问、指定过去时刻问、预演，
   都不能坏（定时生效与发布稿也一样能带这个条件）。
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


def get_flag(flag):
    return next(f for f in c.get("/api/flags", headers=H).get_json()
                if f["name"] == flag)


def uid_with_bucket(flag, pred, prefix="ub"):
    """找一个对 flag 分桶满足 pred 的测试身份（确定性，可复算）。"""
    i = 0
    while True:
        uid = f"{prefix}{i}"
        if pred(flag_app.bucket_of(flag, uid)):
            return uid
        i += 1


PRO = {"plan": "pro"}
FREE = {"plan": "free"}

# ---------------------------------------------------------------- 准备开关
c.post("/api/flags", headers=H, json={"name": "cr-feat"})                       # 默认关
c.post("/api/flags", headers=H, json={"name": "cr-defon", "default_enabled": True})
patch("cr-feat", {"rollout_percent": 50, "rollout_condition": PRO})
patch("cr-defon", {"rollout_percent": 50, "rollout_condition": PRO})
IN = uid_with_bucket("cr-feat", lambda b: b < 50)      # 分桶命中（比例内）
OUT = uid_with_bucket("cr-feat", lambda b: b >= 50)    # 分桶未命中
# cr-defon 与 cr-feat 名字不同，分桶不同，各自找
IN_ON = uid_with_bucket("cr-defon", lambda b: b < 50)
OUT_ON = uid_with_bucket("cr-defon", lambda b: b >= 50)

print("== 1. 只对对上条件的人按比例放量；没对上的按原来的默认走 ==")
check("管理端列表回显放量条件", get_flag("cr-feat")["rollout_condition"] == PRO,
      get_flag("cr-feat"))
r = check_flag("cr-feat", IN, PRO)
check("对上且分桶命中 -> 开，reason=rollout",
      r["enabled"] is True and r["reason"] == "rollout", r)
r = check_flag("cr-feat", OUT, PRO)
check("对上但分桶未命中 -> 关，reason=rollout（这一层定论，不落默认）",
      r["enabled"] is False and r["reason"] == "rollout", r)
r = check_flag("cr-feat", IN, FREE)
check("没对上 -> 不看比例，按原来的默认（默认关），reason=default",
      r["enabled"] is False and r["reason"] == "default", r)
r = check_flag("cr-feat", IN)
check("没带属性也算没对上 -> 默认关", r["enabled"] is False
      and r["reason"] == "default" and "attrs" not in r, r)
r = check_flag("cr-defon", OUT_ON, PRO)
check("默认开的开关：对上未命中 -> 关（rollout 定论）",
      r["enabled"] is False and r["reason"] == "rollout", r)
r = check_flag("cr-defon", OUT_ON, FREE)
check("默认开的开关：没对上 -> 按原来的默认开，reason=default",
      r["enabled"] is True and r["reason"] == "default", r)
r = check_flag("cr-defon", OUT_ON)
check("默认开的开关：没带属性 -> 默认开", r["enabled"] is True
      and r["reason"] == "default", r)
# 分布：对上的 120 个人里两边都有，且开的就是分桶 < 50 的那批
sample = [f"dist{i}" for i in range(120)]
got = {u: check_flag("cr-feat", u, PRO) for u in sample}
on_uids = {u for u, r in got.items() if r["enabled"]}
expect_on = {u for u in sample if flag_app.bucket_of("cr-feat", u) < 50}
check("对上的人里按比例开一部分、剩下的关（两边都有）",
      0 < len(on_uids) < len(sample), len(on_uids))
check("开的集合与确定性分桶完全一致", on_uids == expect_on)
check("对上的人结果都由放量层定论", all(r["reason"] == "rollout"
                                        for r in got.values()))
# 比例为 0 时条件不参与：对上也按默认走
c.post("/api/flags", headers=H, json={"name": "cr-zero"})
patch("cr-zero", {"rollout_condition": PRO})
r = check_flag("cr-zero", IN, PRO)
check("比例为 0：对上也按默认走（条件不启用放量层）",
      r["enabled"] is False and r["reason"] == "default", r)

print("== 2. 同一个人、同一身属性，多次来问不能变 ==")
first = None
for i in range(5):
    # 故意打乱 key 顺序与空白
    raw = '{"plan":"pro"}' if i % 2 else '{ "plan" : "pro" }'
    r = c.get(f"/api/flags/cr-feat/check?identity={IN}"
              f"&attrs={urllib.parse.quote(raw)}").get_json()
    cur = (r["enabled"], r["reason"])
    if first is None:
        first = cur
    check(f"第{i+1}次来问结果相同", cur == first, r)
b = bundle(IN, PRO)
one = check_flag("cr-feat", IN, PRO)
check("单查与整包同一身属性结果一致",
      one["enabled"] == b["flags"]["cr-feat"]["enabled"]
      and one["reason"] == b["flags"]["cr-feat"]["reason"])
check("整包里没对上的开关按默认",
      b["flags"]["cr-zero"]["reason"] == "default", b["flags"]["cr-zero"])
b2 = bundle(IN, PRO)
check("同人同属性再拿整包：版本与结果都不变",
      b2["version"] == b["version"] and b2["flags"] == b["flags"])

print("== 3. 全关、冻住、依赖、单人强制还是压过它 ==")
# 全关
c.post("/api/flags", headers=H, json={"name": "cr-kill"})
patch("cr-kill", {"rollout_percent": 100, "rollout_condition": PRO})
KIN = uid_with_bucket("cr-kill", lambda b: True)
check("前提：100% 且对上 -> 开",
      check_flag("cr-kill", KIN, PRO)["enabled"] is True)
patch("cr-kill", {"kill_switch": True})
r = check_flag("cr-kill", KIN, PRO)
check("全关压过条件放量", r["enabled"] is False and r["reason"] == "kill_switch", r)
patch("cr-kill", {"kill_switch": False})
check("全关解除恢复", check_flag("cr-kill", KIN, PRO)["enabled"] is True)
# 冻住（冻住那一刻按不带属性的口径求值：没对上就按默认，所以用默认开的开关
# 才能冻在开上）
c.post("/api/flags", headers=H, json={"name": "cr-frz", "default_enabled": True})
patch("cr-frz", {"rollout_percent": 100, "rollout_condition": PRO})
r = c.put("/api/flags/cr-frz/freezes", headers=H, json={"identity": "fz1"})
check("冻住对上的人（不带属性口径：没对上走默认开）",
      r.get_json()["enabled"] is True, r.get_json())
patch("cr-frz", {"rollout_condition": {"plan": "nobody"}, "default_enabled": False})
r = check_flag("cr-frz", "fz1", PRO)
check("冻住后改条件、改默认也不动他（仍开，reason=freeze）",
      r["enabled"] is True and r["reason"] == "freeze", r)
c.delete("/api/flags/cr-frz/freezes", headers=H, json={"identity": "fz1"})
r = check_flag("cr-frz", "fz1", PRO)
check("解冻后按当时规则（对不上新条件 -> 默认关）",
      r["enabled"] is False and r["reason"] == "default", r)
# 依赖
c.post("/api/flags", headers=H, json={"name": "cr-parent"})   # 默认关
c.post("/api/flags", headers=H, json={"name": "cr-child"})
patch("cr-child", {"rollout_percent": 100, "rollout_condition": PRO,
                   "depends_on": "cr-parent"})
r = check_flag("cr-child", "d1", PRO)
check("被依赖开关关着：对上 100% 也开不了（reason=depends_on）",
      r["enabled"] is False and r["reason"] == "depends_on", r)
patch("cr-parent", {"default_enabled": True})
r = check_flag("cr-child", "d1", PRO)
check("依赖开了：条件放量正常生效", r["enabled"] is True
      and r["reason"] == "rollout", r)
# 单人强制
c.post("/api/flags", headers=H, json={"name": "cr-ovr"})
patch("cr-ovr", {"rollout_percent": 100, "rollout_condition": PRO})
c.put("/api/flags/cr-ovr/overrides", headers=H,
      json={"identity": "ov1", "enabled": False})
r = check_flag("cr-ovr", "ov1", PRO)
check("单人强制关压过条件命中", r["enabled"] is False
      and r["reason"] == "override", r)
c.put("/api/flags/cr-ovr/overrides", headers=H,
      json={"identity": "ov2", "enabled": True})
r = check_flag("cr-ovr", "ov2", FREE)
check("单人强制开压过「没对上走默认」", r["enabled"] is True
      and r["reason"] == "override", r)

print("== 4. 条件命中仍过互斥组；与属性打开条件共存 ==")
c.post("/api/groups", headers=H, json={"name": "crg"})
c.post("/api/flags", headers=H, json={"name": "cr-g1"})
c.post("/api/flags", headers=H, json={"name": "cr-g2"})
c.put("/api/groups/crg/flags", headers=H, json={"flag": "cr-g1"})
c.put("/api/groups/crg/flags", headers=H, json={"flag": "cr-g2"})
patch("cr-g1", {"rollout_percent": 100, "rollout_condition": PRO})
patch("cr-g2", {"rollout_percent": 100, "rollout_condition": PRO})
r1 = check_flag("cr-g1", "g-u1", PRO)
check("组内：条件命中且自然开 -> 落定（reason=group）",
      r1["enabled"] is True and r1["reason"] == "group", r1)
r2 = check_flag("cr-g2", "g-u1", PRO)
check("组内已有人落定 -> 关（reason=group）",
      r2["enabled"] is False and r2["reason"] == "group", r2)
r3 = check_flag("cr-g1", "g-u2", FREE)
check("没对上 -> 默认关，不在组内占位",
      r3["enabled"] is False and r3["reason"] == "default", r3)
# 属性打开条件（targeting）仍排在条件放量之前
c.post("/api/flags", headers=H, json={"name": "cr-tgt"})
patch("cr-tgt", {"targeting": {"vip": True}, "rollout_percent": 50,
                 "rollout_condition": PRO})
TIN = uid_with_bucket("cr-tgt", lambda b: b < 50)
r = check_flag("cr-tgt", TIN, {"plan": "free", "vip": True})
check("属性打开条件命中 -> 开（reason=targeting），不看放量条件",
      r["enabled"] is True and r["reason"] == "targeting", r)
r = check_flag("cr-tgt", TIN, PRO)
check("属性打开条件没中、放量条件对上 -> 走放量层",
      r["reason"] == "rollout" and r["enabled"] is True, r)

print("== 5. 改了比例或条件：整包换新版本，拿着改前那包说过期 ==")
v_pro = bundle("v1", PRO)["version"]
v_none = bundle("v1")["version"]
v_free = bundle("v1", FREE)["version"]
bundle("v2", PRO)  # 另一个人也拿过命中属性的包
t_mark = time.time()
patch("cr-feat", {"rollout_condition": {"plan": "enterprise"}}, headers=H_BOB)
b_new = bundle("v1", PRO)
check("改条件：pro 属性变成没对上 -> 按默认关",
      b_new["flags"]["cr-feat"]["enabled"] is False
      and b_new["flags"]["cr-feat"]["reason"] == "default")
check("改条件：命中属性的包换新版本", b_new["version"] != v_pro)
check("拿着改前那包来问 -> valid=false（过期）",
      bundle("v1", PRO, version=v_pro)["valid"] is False)
check("拿着新包来问 -> valid=true",
      bundle("v1", PRO, version=b_new["version"])["valid"] is True)
b_none_new = bundle("v1")
check("改条件：不带属性的包也换新版本（没对上的人走哪层由条件决定）",
      b_none_new["version"] != v_none)
check("不带属性的旧包同样过期",
      bundle("v1", version=v_none)["valid"] is False)
check("没对上的那身属性（free）的包也换了版本",
      bundle("v1", FREE)["version"] != v_free)
inv = c.get("/api/bundles/invalidations?limit=300", headers=H).get_json()
mine = [x for x in inv if x["created_at"] >= t_mark]
check("改条件产生了失效记录", len(mine) > 0)
check("失效记录的操作人是 bob", all(x["actor"] == "bob" for x in mine))
hashes = {x["attrs_hash"] for x in mine}
check("失效记录同时波及带属性的包与不带属性的包",
      "" in hashes and len(hashes) > 1, hashes)
rec = next(x for x in mine if x["identity"] == "v2")
check("记下的新版本与本人再来拿一致",
      rec["new_version"] == bundle("v2", PRO)["version"])
# 改比例一样换版本
v1 = bundle("v1", PRO)["version"]
patch("cr-feat", {"rollout_percent": 60})
check("改比例：整包也换新版本", bundle("v1", PRO)["version"] != v1)
check("改比例前的包过期", bundle("v1", PRO, version=v1)["valid"] is False)
# 比例为 0 时改条件不动任何版本（条件不参与求值）
vz = bundle("v1", PRO)["version"]
patch("cr-zero", {"rollout_condition": {"plan": "enterprise"}})
check("比例为 0：改条件不换版本", bundle("v1", PRO)["version"] == vz)
patch("cr-zero", {"rollout_percent": 30})
check("比例从 0 调大（条件开始生效）：换版本",
      bundle("v1", PRO)["version"] != vz)
# 清除条件：回到「比例对所有人分桶」
patch("cr-feat", {"rollout_condition": {}})
check("空对象清除放量条件", get_flag("cr-feat")["rollout_condition"] == {})
r = check_flag("cr-feat", IN, FREE)
check("清除后没定条件：比例对所有人分桶（free 属性也走放量层）",
      r["reason"] == "rollout", r)
patch("cr-feat", {"rollout_condition": PRO})
patch("cr-feat", {"rollout_condition": None})
check("null 也清除放量条件", get_flag("cr-feat")["rollout_condition"] == {})
patch("cr-feat", {"rollout_condition": PRO, "rollout_percent": 50})

print("== 6. 条件形式与校验 ==")
patch("cr-feat", {"rollout_condition": {"plan": ["pro", "ent"], "level": 3}})
r = check_flag("cr-feat", IN, {"plan": "ent", "level": 3})
check("多键 AND + 列表 OR：全对上才走放量层", r["reason"] == "rollout", r)
r = check_flag("cr-feat", IN, {"plan": "ent", "level": 4})
check("只对上一个键 -> 没对上，走默认", r["reason"] == "default", r)
cur = get_flag("cr-feat")["rollout_condition"]
for bad in ([], "pro", 42, {"": "pro"}, {"k": []}, {"k": {"x": 1}}, {"k": [1, []]}):
    r = patch("cr-feat", {"rollout_condition": bad})
    check(f"非法放量条件 400：{json.dumps(bad, ensure_ascii=False)}",
          r.status_code == 400, r.status_code)
check("校验失败后原条件还在", get_flag("cr-feat")["rollout_condition"] == cur)
r = c.post("/api/flags", headers=H,
           json={"name": "cr-bad", "rollout_condition": {"k": []}})
check("建开关带非法放量条件 400", r.status_code == 400)
r = patch("cr-feat", {"rollout_condition": {"k": []},
                      "effective_at": time.time() + 60})
check("定时非法放量条件 400", r.status_code == 400)
r = patch("cr-feat", {"rollout_condition": PRO},
          headers={"X-Actor": "x"})
check("定放量条件接口要鉴权", r.status_code == 401)
patch("cr-feat", {"rollout_condition": PRO})  # 恢复单键条件

print("== 7. 定时生效：到点前照旧，到点按新条件 ==")
c.post("/api/flags", headers=H, json={"name": "cr-sched"})
patch("cr-sched", {"rollout_percent": 100, "rollout_condition": PRO})
SIN = uid_with_bucket("cr-sched", lambda b: True)
b0 = bundle("s1", {"plan": "beta"})
check("到点前 beta 属性没对上 -> 默认关",
      b0["flags"]["cr-sched"]["enabled"] is False)
r = patch("cr-sched", {"rollout_condition": {"plan": "beta"},
                       "effective_at": time.time() + 0.4})
check("约放量条件返回 202", r.status_code == 202
      and r.get_json().get("scheduled") is True, r.status_code)
check("到点前仍按旧条件", check_flag("cr-sched", SIN, {"plan": "beta"})
      ["enabled"] is False)
check("到点前整包版本不换", bundle("s1", {"plan": "beta"})["version"] == b0["version"])
time.sleep(0.6)
r = check_flag("cr-sched", SIN, {"plan": "beta"})
check("到点后按新条件命中（100% 开）", r["enabled"] is True
      and r["reason"] == "rollout", r)
b1 = bundle("s1", {"plan": "beta"})
check("到点后整包换新版本", b1["version"] != b0["version"])
check("到点前那包过期",
      bundle("s1", {"plan": "beta"}, version=b0["version"])["valid"] is False)

print("== 8. 发布稿：收进稿不生效，发布一起生效 ==")
c.post("/api/flags", headers=H, json={"name": "cr-draft"})
patch("cr-draft", {"rollout_percent": 100})
DIN = uid_with_bucket("cr-draft", lambda b: True)
r = c.post("/api/drafts", headers=H, json={"note": "放量条件一揽子"})
did = r.get_json()["draft_id"]
r = c.put(f"/api/drafts/{did}/flags/cr-draft", headers=H,
          json={"rollout_condition": PRO})
check("放量条件能收进稿", r.status_code == 200
      and r.get_json()["changes"]["rollout_condition"] == PRO, r.get_json())
r = c.put(f"/api/drafts/{did}/flags/cr-draft", headers=H,
          json={"rollout_condition": {"k": []}})
check("非法放量条件收不进稿", r.status_code == 400)
check("发布前：对上的人还是走放量（稿不生效）",
      check_flag("cr-draft", DIN, PRO)["reason"] == "rollout")
r = c.post(f"/api/drafts/{did}/publish", headers=H)
check("发布成功", r.status_code == 200 and r.get_json()["published"] is True,
      r.get_json())
r = check_flag("cr-draft", DIN, PRO)
check("发布后：对上的人走放量层（100% 开）",
      r["enabled"] is True and r["reason"] == "rollout", r)
r = check_flag("cr-draft", DIN, FREE)
check("发布后：没对上的人走默认关",
      r["enabled"] is False and r["reason"] == "default", r)
d = c.get(f"/api/drafts/{did}", headers=H).get_json()
check("稿详情回显放量条件",
      d["flags"]["cr-draft"]["rollout_condition"] == PRO, d["flags"]["cr-draft"])

print("== 9. 预演：先看一眼，真的什么都不动 ==")
c.post("/api/flags", headers=H, json={"name": "cr-prev"})
patch("cr-prev", {"rollout_percent": 100})
PIN = uid_with_bucket("cr-prev", lambda b: True)
before = check_flag("cr-prev", PIN, FREE)
check("前提：没定条件时 free 属性也走放量层（100% 开）",
      before["enabled"] is True and before["reason"] == "rollout", before)
r = c.post("/api/preview", headers=H, json={
    "identities": [PIN], "flag": "cr-prev",
    "changes": {"rollout_condition": PRO}, "attrs": FREE})
j = r.get_json()
check("预演返回 200", r.status_code == 200, r.status_code)
p = j["identities"][PIN]
check("预演：加上条件后 free 属性没对上 -> 会变成默认关",
      p["preview"]["cr-prev"]["enabled"] is False
      and p["preview"]["cr-prev"]["reason"] == "default", p["preview"])
check("预演：current 还是现在的开", p["current"]["cr-prev"]["enabled"] is True)
check("changed 里列出本开关", "cr-prev" in p["changed"], p["changed"])
r = c.post("/api/preview", headers=H, json={
    "identities": [PIN], "flag": "cr-prev",
    "changes": {"rollout_condition": PRO}, "attrs": PRO})
check("预演：带对得上的属性则保持开",
      r.get_json()["identities"][PIN]["preview"]["cr-prev"]["enabled"] is True)
check("预演后真开关没动（仍没有放量条件）",
      get_flag("cr-prev")["rollout_condition"] == {})
check("预演后来问结果不变", check_flag("cr-prev", PIN, FREE)["enabled"] is True)
r = c.post("/api/preview", headers=H, json={
    "identities": [PIN], "flag": "cr-prev",
    "changes": {"rollout_condition": {"k": []}}})
check("预演非法放量条件 400", r.status_code == 400)

print("== 10. 指定过去时刻问：当时还没定条件按当时算 ==")
c.post("/api/flags", headers=H, json={"name": "cr-hist"})
patch("cr-hist", {"rollout_percent": 100})
HIN = uid_with_bucket("cr-hist", lambda b: True)
t0 = time.time()
time.sleep(0.05)
check("现在：没定条件，free 属性走放量层开",
      check_flag("cr-hist", HIN, FREE)["enabled"] is True)
patch("cr-hist", {"rollout_condition": PRO})
time.sleep(0.05)
t1 = time.time()
r = c.get(f"/api/history?identity={HIN}&at={t0}"
          f"&attrs={urllib.parse.quote(json.dumps(FREE))}").get_json()
check("问改条件之前：free 属性当时走放量层开",
      r["flags"]["cr-hist"]["enabled"] is True
      and r["flags"]["cr-hist"]["reason"] == "rollout", r["flags"]["cr-hist"])
r = c.get(f"/api/history?identity={HIN}&at={t1}"
          f"&attrs={urllib.parse.quote(json.dumps(FREE))}").get_json()
check("问改条件之后：free 属性没对上 -> 默认关",
      r["flags"]["cr-hist"]["enabled"] is False
      and r["flags"]["cr-hist"]["reason"] == "default", r["flags"]["cr-hist"])
r = c.get(f"/api/history?identity={HIN}&at={t1}"
          f"&attrs={urllib.parse.quote(json.dumps(PRO))}").get_json()
check("问改条件之后：pro 属性对上 -> 放量层开",
      r["flags"]["cr-hist"]["enabled"] is True
      and r["flags"]["cr-hist"]["reason"] == "rollout", r["flags"]["cr-hist"])
r = c.get(f"/api/history?identity={HIN}&at={t1}").get_json()
check("问改条件之后（不带属性）：默认关",
      r["flags"]["cr-hist"]["enabled"] is False
      and r["flags"]["cr-hist"]["reason"] == "default", r["flags"]["cr-hist"])

print("== 11. 老路不坏：没定条件时与只有比例时一字不差 ==")
c.post("/api/flags", headers=H, json={"name": "cr-plain"})
patch("cr-plain", {"rollout_percent": 50})
r = check_flag("cr-plain", "p1", PRO)
check("没定条件：带属性来问也走放量层", r["reason"] == "rollout", r)
r2 = check_flag("cr-plain", "p1")
check("没定条件：不带属性结果相同（分桶只认开关与身份）",
      r2["enabled"] == r["enabled"] and r2["reason"] == "rollout", r2)
r = c.get("/api/flags/cr-plain/check")
check("check 缺 identity 仍 400", r.status_code == 400)
r = c.get("/api/bundle")
check("bundle 缺 identity 仍 400", r.status_code == 400)
# 建开关时直接带放量条件（比例走 PATCH，与现有建开关口径一致）
c.post("/api/flags", headers=H, json={"name": "cr-born", "rollout_condition": PRO})
patch("cr-born", {"rollout_percent": 100})
check("建开关时直接带条件：列表回显", get_flag("cr-born")["rollout_condition"] == PRO)
r = check_flag("cr-born", "nb1", PRO)
check("建开关时直接带条件：对上即开", r["enabled"] is True
      and r["reason"] == "rollout", r)
r = check_flag("cr-born", "nb1", FREE)
check("建开关时直接带条件：没对上走默认",
      r["enabled"] is False and r["reason"] == "default", r)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
