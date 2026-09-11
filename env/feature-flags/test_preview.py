"""预演（POST /api/preview：改前先看一眼，真的开关和稿都不动）的端到端测试。

覆盖需求（口语版）：
1. 管理端要改某个开关、或手头有一稿还没发，能先点几个人看：这些人现在每个
   开关开没开；要是现在就改完、或现在就把这稿发出去，会变成什么样。开着的
   要带上那份配置。
2. 这只是先看一眼：真的开关、稿、整包版本、组落定、审计、失效记录一概不动。
3. 预演里全关、冻结、依赖还按现在的规矩算；约了时间还没到点的改动、别的
   没发布的稿，都不能算进去。
4. 稿现在发不出去（成环 / 目标没了）时照实说 publishable=false，预演=现在。
5. 原来问现在开没开、一次拿走所有开关、带上属性来问、指定过去时刻来问，
   都不能坏。
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

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}  {extra}")


def preview(body, headers=H):
    return c.post("/api/preview", headers=headers, json=body)


def patch(flag, body):
    r = c.patch(f"/api/flags/{flag}", headers=H, json=body)
    assert r.status_code == 200, r.get_json()
    return r


def check_flag(flag, identity, attrs=None):
    q = f"identity={urllib.parse.quote(identity)}"
    if attrs is not None:
        q += "&attrs=" + urllib.parse.quote(json.dumps(attrs, ensure_ascii=False))
    r = c.get(f"/api/flags/{flag}/check?{q}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def bundle(identity, attrs=None, version=None):
    q = f"identity={urllib.parse.quote(identity)}"
    if attrs is not None:
        q += "&attrs=" + urllib.parse.quote(json.dumps(attrs, ensure_ascii=False))
    if version is not None:
        q += f"&version={version}"
    r = c.get(f"/api/bundle?{q}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def new_draft(note=""):
    r = c.post("/api/drafts", headers=H, json={"note": note})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["draft_id"]


def stage(did, flag, body):
    r = c.put(f"/api/drafts/{did}/flags/{flag}", headers=H, json=body)
    assert r.status_code == 200, r.get_json()
    return r


def diff_maps(a, b):
    """两个字典逐键对比，返回不同的键及两边的值（断言失败时定位用）。"""
    return {k: (a.get(k), b.get(k)) for k in set(a) | set(b)
            if a.get(k) != b.get(k)}


# ---------------------------------------------------------------- 准备开关与状态
c.post("/api/flags", headers=H, json={"name": "pay", "default_enabled": True,
                                      "config": {"color": "blue"}})
c.post("/api/flags", headers=H, json={"name": "promo", "default_enabled": False})
c.post("/api/flags", headers=H, json={"name": "beta", "default_enabled": False})
c.post("/api/flags", headers=H, json={"name": "tgt", "default_enabled": False,
                                      "targeting": {"plan": "pro"}})
c.post("/api/flags", headers=H, json={"name": "grp-a", "default_enabled": False})
c.post("/api/flags", headers=H, json={"name": "grp-b", "default_enabled": False})
c.post("/api/flags", headers=H, json={"name": "dep-base", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "dep-child", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "fz", "default_enabled": False})
c.post("/api/flags", headers=H, json={"name": "sched", "default_enabled": False})
c.post("/api/flags", headers=H, json={"name": "other-draft-flag",
                                      "default_enabled": False})
c.post("/api/flags", headers=H, json={"name": "cyc-1", "default_enabled": False})
c.post("/api/flags", headers=H, json={"name": "cyc-2", "default_enabled": False})
c.post("/api/flags", headers=H, json={"name": "promo2", "default_enabled": False})
c.post("/api/groups", headers=H, json={"name": "exp"})
c.put("/api/groups/exp/flags", headers=H, json={"flag": "grp-a"})
c.put("/api/groups/exp/flags", headers=H, json={"flag": "grp-b"})
patch("dep-child", {"depends_on": "dep-base"})
# u7 的 pay 单人强制关；u3 的 fz 冻在关；u5 的 pay 冻在开（带 blue 配置快照）
c.put("/api/flags/pay/overrides", headers=H, json={"identity": "u7", "enabled": False})
c.put("/api/flags/fz/freezes", headers=H, json={"identity": "u3"})
c.put("/api/flags/pay/freezes", headers=H, json={"identity": "u5"})
# 约了时间还没到点的改动：sched 一小时后默认开（预演不能算进去）
r = c.patch("/api/flags/sched", headers=H,
            json={"default_enabled": True, "effective_at": time.time() + 3600})
assert r.status_code == 202, r.get_json()
# 别的没发布的稿：other-draft-flag 默认开（预演不能算进去）
draft_other = new_draft("别人的稿")
stage(draft_other, "other-draft-flag", {"default_enabled": True})

# 基线：预演跑完后这些必须一字不变
flags_before = c.get("/api/flags", headers=H).get_json()
draft_other_before = c.get(f"/api/drafts/{draft_other}", headers=H).get_json()
audit_before = len(c.get("/api/audit?limit=500", headers=H).get_json())
inv_before = len(c.get("/api/bundles/invalidations?limit=500", headers=H).get_json())
v_u1 = bundle("u1")["version"]
v_u1p = bundle("u1", {"plan": "pro"})["version"]

print("== 1. 改开关预演：现在什么样、改完什么样，开着的带配置 ==")
r = preview({"identities": ["u1", "u2"], "flag": "promo",
             "changes": {"default_enabled": True}})
check("预演 200", r.status_code == 200, r.get_json())
body = r.get_json()
check("回显 mode/flag/changes",
      body["mode"] == "changes" and body["flag"] == "promo"
      and body["changes"] == {"default_enabled": True}, body)
p1 = body["identities"]["u1"]
check("current：promo 现在关", p1["current"]["promo"]["enabled"] is False)
check("preview：改完 promo 开（默认值层）",
      p1["preview"]["promo"]["enabled"] is True
      and p1["preview"]["promo"]["reason"] == "default")
check("changed 只列 promo", p1["changed"] == ["promo"], p1["changed"])
check("current 里 pay 开着、带现在那份配置",
      p1["current"]["pay"]["enabled"] is True
      and p1["current"]["pay"]["config"] == {"color": "blue"})
check("preview 里 pay 配置不受影响",
      p1["preview"]["pay"]["config"] == {"color": "blue"})
check("没挂配置的开关不带 config 键",
      "config" not in p1["preview"]["promo"])
check("u1 的 current 与真实整包一字不差",
      p1["current"] == bundle("u1")["flags"],
      diff_maps(p1["current"], bundle("u1")["flags"]))
check("u2 的 current 与真实整包一字不差",
      body["identities"]["u2"]["current"] == bundle("u2")["flags"])

print("== 2. 预演全关：连冻住的人也压过，且不带配置 ==")
r = preview({"identities": ["u1", "u5"], "flag": "pay",
             "changes": {"kill_switch": True}})
b = r.get_json()
for uid in ("u1", "u5"):
    pv = b["identities"][uid]["preview"]["pay"]
    check(f"{uid} 预演全关：pay 关、reason=kill_switch",
          pv["enabled"] is False and pv["reason"] == "kill_switch")
    check(f"{uid} 预演全关不带配置", "config" not in pv)
check("冻住的 u5 在全关预演里也变关", "pay" in b["identities"]["u5"]["changed"])
check("真开关没被动：pay 仍开", check_flag("pay", "u1")["enabled"] is True)
check("u5 真来问仍拿冻住的开", check_flag("pay", "u5")["enabled"] is True)

print("== 3. 预演里冻结按现在的规矩算 ==")
r = preview({"identities": ["u3", "u4"], "flag": "fz",
             "changes": {"default_enabled": True}})
b = r.get_json()
check("冻住的 u3：预演改默认开也动不了他",
      b["identities"]["u3"]["preview"]["fz"]["enabled"] is False
      and b["identities"]["u3"]["preview"]["fz"]["reason"] == "freeze")
check("u3 changed 为空", b["identities"]["u3"]["changed"] == [])
check("没冻的 u4：预演按新默认开",
      b["identities"]["u4"]["preview"]["fz"]["enabled"] is True)
r = preview({"identities": ["u5", "u6"], "flag": "pay",
             "changes": {"config": {"color": "red"}}})
b = r.get_json()
check("u5 冻在开：预演改配置后仍拿冻住那一刻的 blue",
      b["identities"]["u5"]["preview"]["pay"]["config"] == {"color": "blue"})
check("u5 changed 不含 pay", "pay" not in b["identities"]["u5"]["changed"])
check("u6 预演拿到新配置 red",
      b["identities"]["u6"]["preview"]["pay"]["config"] == {"color": "red"})
check("u6 current 还是 blue",
      b["identities"]["u6"]["current"]["pay"]["config"] == {"color": "blue"})
check("u6 changed 含 pay（配置变了）", "pay" in b["identities"]["u6"]["changed"])

print("== 4. 预演里依赖按现在的规矩算 ==")
r = preview({"identities": ["u1"], "flag": "dep-base",
             "changes": {"kill_switch": True}})
b = r.get_json()["identities"]["u1"]
check("全关 dep-base 的预演：dep-child 跟着 depends_on 关",
      b["preview"]["dep-child"]["enabled"] is False
      and b["preview"]["dep-child"]["reason"] == "depends_on")
check("changed 含 dep-base 与 dep-child",
      set(b["changed"]) == {"dep-base", "dep-child"}, b["changed"])
check("真来问 dep-child 仍开", check_flag("dep-child", "u1")["enabled"] is True)
r = preview({"identities": ["u1", "u7"], "flag": "promo",
             "changes": {"depends_on": "pay", "default_enabled": True}})
b = r.get_json()
check("u1 的 pay 开：promo 预演开",
      b["identities"]["u1"]["preview"]["promo"]["enabled"] is True)
check("u7 的 pay 被强制关：promo 预演 depends_on 关",
      b["identities"]["u7"]["preview"]["promo"]["enabled"] is False
      and b["identities"]["u7"]["preview"]["promo"]["reason"] == "depends_on")

print("== 5. 没到点的定时变更、别的没发布的稿，都不算进预演 ==")
r = preview({"identities": ["u1"], "flag": "promo",
             "changes": {"default_enabled": True}})
b = r.get_json()["identities"]["u1"]
check("sched 约了 1 小时后默认开：现在关", b["current"]["sched"]["enabled"] is False)
check("预演里也不算：sched 仍关", b["preview"]["sched"]["enabled"] is False)
check("别人的稿没发布：other-draft-flag 现在关",
      b["current"]["other-draft-flag"]["enabled"] is False)
check("预演里也不算：other-draft-flag 仍关",
      b["preview"]["other-draft-flag"]["enabled"] is False)

print("== 6. 带属性来预演 ==")
r = preview({"identities": ["u1"], "attrs": {"plan": "pro"},
             "flag": "promo", "changes": {"default_enabled": True}})
b = r.get_json()
check("响应回显 attrs", b.get("attrs") == {"plan": "pro"})
check("带 pro 属性：tgt 命中条件开",
      b["identities"]["u1"]["current"]["tgt"]["enabled"] is True
      and b["identities"]["u1"]["current"]["tgt"]["reason"] == "targeting")
r = preview({"identities": ["u1"], "flag": "promo",
             "changes": {"default_enabled": True}})
check("不带属性：tgt 关",
      r.get_json()["identities"]["u1"]["current"]["tgt"]["enabled"] is False)
r = preview({"identities": ["u1"], "attrs": {"plan": "pro"},
             "flag": "tgt", "changes": {"targeting": {"plan": "vip"}}})
b = r.get_json()["identities"]["u1"]
check("改条件预演：现在 pro 命中开", b["current"]["tgt"]["enabled"] is True)
check("改成只认 vip 后 pro 对不上：预演关", b["preview"]["tgt"]["enabled"] is False)
check("changed 含 tgt", "tgt" in b["changed"])
r = preview({"identities": ["u1"], "attrs": {"plan": "vip"},
             "flag": "tgt", "changes": {"targeting": {"plan": "vip"}}})
check("换 vip 属性来问：预演开",
      r.get_json()["identities"]["u1"]["preview"]["tgt"]["enabled"] is True)
check("真条件没被动：tgt 仍认 pro",
      check_flag("tgt", "u1", {"plan": "pro"})["enabled"] is True)

print("== 7. 预演全程只读：开关、稿、版本、审计、失效记录一概不动 ==")
check("开关列表一字不变", c.get("/api/flags", headers=H).get_json() == flags_before)
check("别人的稿一字未动",
      c.get(f"/api/drafts/{draft_other}", headers=H).get_json() == draft_other_before)
check("审计没多一条",
      len(c.get("/api/audit?limit=500", headers=H).get_json()) == audit_before)
check("失效记录没多一条",
      len(c.get("/api/bundles/invalidations?limit=500", headers=H).get_json())
      == inv_before)
check("u1 整包版本没变", bundle("u1")["version"] == v_u1)
check("u1 属性包版本没变", bundle("u1", {"plan": "pro"})["version"] == v_u1p)

print("== 8. 互斥组预演：能看出谁落定，且不留下任何落定记录 ==")
r = preview({"identities": ["u9"], "flag": "grp-b",
             "changes": {"default_enabled": True}})
b = r.get_json()["identities"]["u9"]
check("预演 grp-b 默认开：grp-b 开（组层）",
      b["preview"]["grp-b"]["enabled"] is True
      and b["preview"]["grp-b"]["reason"] == "group")
check("grp-a 仍关", b["preview"]["grp-a"]["enabled"] is False)
draft_grp = new_draft("组内两个都开")
stage(draft_grp, "grp-a", {"default_enabled": True})
stage(draft_grp, "grp-b", {"default_enabled": True})
r = preview({"identities": ["u9b"], "draft_id": draft_grp})
b = r.get_json()["identities"]["u9b"]
check("组内两个都开：名字序在前的 grp-a 落定",
      b["preview"]["grp-a"]["enabled"] is True)
check("grp-b 被组挡下（reason=group）",
      b["preview"]["grp-b"]["enabled"] is False
      and b["preview"]["grp-b"]["reason"] == "group")
c.delete(f"/api/drafts/{draft_grp}", headers=H)
# 预演若把模拟落定写进库，下面两个真查询就会看到错的赢家
patch("grp-b", {"default_enabled": True})
r = bundle("u9b")
check("u9b 真来拿：grp-b 开（预演没把 grp-a 落定给他）",
      r["flags"]["grp-b"]["enabled"] is True, r["flags"])
patch("grp-a", {"default_enabled": True})
r = bundle("u9")
check("u9 真来拿：grp-a 开（预演没把 grp-b 落定给他）",
      r["flags"]["grp-a"]["enabled"] is True, r["flags"])
check("u9 的 grp-b 被组挡下", r["flags"]["grp-b"]["enabled"] is False)
patch("grp-a", {"default_enabled": False})
patch("grp-b", {"default_enabled": False})

print("== 9. 稿预演：先看到的与真发布后的一字不差 ==")
draft_d = new_draft("要发布的一稿")
stage(draft_d, "promo", {"default_enabled": True})
stage(draft_d, "beta", {"kill_switch": True})
stage(draft_d, "pay", {"config": {"color": "green"}})
draft_snapshot = c.get(f"/api/drafts/{draft_d}", headers=H).get_json()
r = preview({"identities": ["u1", "u5"], "draft_id": draft_d})
check("稿预演 200", r.status_code == 200, r.get_json())
b = r.get_json()
check("mode=draft、可发布", b["mode"] == "draft" and b["draft_id"] == draft_d
      and b["publishable"] is True, b)
prev_u1 = b["identities"]["u1"]["preview"]
prev_u5 = b["identities"]["u5"]["preview"]
check("u1 预演：promo 开", prev_u1["promo"]["enabled"] is True)
check("u1 预演：beta 全关", prev_u1["beta"]["reason"] == "kill_switch")
check("u1 预演：pay 换 green 配置", prev_u1["pay"]["config"] == {"color": "green"})
check("u5 冻住：预演仍是冻住的 blue", prev_u5["pay"]["config"] == {"color": "blue"})
check("预演后稿一字未动",
      c.get(f"/api/drafts/{draft_d}", headers=H).get_json() == draft_snapshot)
check("预演后 promo 真还是关", check_flag("promo", "u1")["enabled"] is False)
r = c.post(f"/api/drafts/{draft_d}/publish", headers=H)
check("真发布成功", r.status_code == 200, r.get_json())
real_u1 = bundle("u1")["flags"]
real_u5 = bundle("u5")["flags"]
check("发布后真实结果与预演一字不差（u1）", real_u1 == prev_u1,
      diff_maps(real_u1, prev_u1))
check("发布后真实结果与预演一字不差（u5）", real_u5 == prev_u5,
      diff_maps(real_u5, prev_u5))

print("== 10. 发不出去的稿：照实说，预演=现在 ==")
draft_e = new_draft("成环稿")
stage(draft_e, "cyc-1", {"depends_on": "cyc-2"})
stage(draft_e, "cyc-2", {"depends_on": "cyc-1"})
r = preview({"identities": ["u1"], "draft_id": draft_e})
b = r.get_json()
check("成环稿预演 200 但 publishable=false",
      r.status_code == 200 and b["publishable"] is False, b)
check("带成环原因", "circular" in b.get("error", ""), b)
p = b["identities"]["u1"]
check("发不出去=什么都不会变：preview==current", p["preview"] == p["current"])
check("changed 为空", p["changed"] == [])
c.post("/api/flags", headers=H, json={"name": "temp-del", "default_enabled": False})
draft_f = new_draft("目标会被删")
stage(draft_f, "temp-del", {"default_enabled": True})
c.delete("/api/flags/temp-del", headers=H)
r = preview({"identities": ["u1"], "draft_id": draft_f})
b = r.get_json()
check("目标开关没了：publishable=false",
      b["publishable"] is False and "no longer exist" in b.get("error", ""), b)
c.post("/api/flags", headers=H, json={"name": "ghost-tgt", "default_enabled": True})
draft_g = new_draft("依赖目标会被删")
stage(draft_g, "beta", {"depends_on": "ghost-tgt"})
c.delete("/api/flags/ghost-tgt", headers=H)
r = preview({"identities": ["u1"], "draft_id": draft_g})
b = r.get_json()
check("依赖目标没了：publishable=false",
      b["publishable"] is False and "not found" in b.get("error", ""), b)
check("beta 真没依赖",
      next(f for f in c.get("/api/flags", headers=H).get_json()
           if f["name"] == "beta")["depends_on"] == "")
for d in (draft_e, draft_f, draft_g):
    c.delete(f"/api/drafts/{d}", headers=H)

print("== 11. 参数校验 ==")
check("预演要鉴权", preview({"identities": ["u1"], "flag": "pay",
                             "changes": {"kill_switch": True}},
                            headers={}).status_code == 401)
check("缺 identities 400",
      preview({"flag": "pay", "changes": {"kill_switch": True}}).status_code == 400)
check("空 identities 400",
      preview({"identities": [], "flag": "pay",
               "changes": {"kill_switch": True}}).status_code == 400)
check("身份不是字符串 400",
      preview({"identities": [1], "flag": "pay",
               "changes": {"kill_switch": True}}).status_code == 400)
check("超过 100 人 400",
      preview({"identities": [f"u{i}" for i in range(101)], "flag": "pay",
               "changes": {"kill_switch": True}}).status_code == 400)
check("100 人没问题",
      preview({"identities": [f"u{i}" for i in range(100)], "flag": "pay",
               "changes": {"kill_switch": True}}).status_code == 200)
check("两种情形都给 400",
      preview({"identities": ["u1"], "flag": "pay",
               "changes": {"kill_switch": True},
               "draft_id": draft_other}).status_code == 400)
check("都不给 400", preview({"identities": ["u1"]}).status_code == 400)
check("只给 flag 400", preview({"identities": ["u1"], "flag": "pay"}).status_code == 400)
check("只给 changes 400",
      preview({"identities": ["u1"],
               "changes": {"kill_switch": True}}).status_code == 400)
check("空 changes 400",
      preview({"identities": ["u1"], "flag": "pay", "changes": {}}).status_code == 400)
check("changes 没有可应用字段 400",
      preview({"identities": ["u1"], "flag": "pay",
               "changes": {"note": "x"}}).status_code == 400)
check("开关不存在 404",
      preview({"identities": ["u1"], "flag": "nope",
               "changes": {"kill_switch": True}}).status_code == 404)
check("changes 不支持 effective_at 400",
      preview({"identities": ["u1"], "flag": "pay",
               "changes": {"kill_switch": True,
                           "effective_at": time.time() + 60}}).status_code == 400)
check("放量越界 400",
      preview({"identities": ["u1"], "flag": "pay",
               "changes": {"rollout_percent": 120}}).status_code == 400)
check("targeting 非法 400",
      preview({"identities": ["u1"], "flag": "pay",
               "changes": {"targeting": [1]}}).status_code == 400)
check("depends_on 指向不存在的开关 400",
      preview({"identities": ["u1"], "flag": "pay",
               "changes": {"depends_on": "nope"}}).status_code == 400)
check("自依赖 400",
      preview({"identities": ["u1"], "flag": "pay",
               "changes": {"depends_on": "pay"}}).status_code == 400)
patch("cyc-2", {"depends_on": "cyc-1"})
r = preview({"identities": ["u1"], "flag": "cyc-1",
             "changes": {"depends_on": "cyc-2"}})
check("成环 400", r.status_code == 400 and "circular" in r.get_json()["error"],
      r.get_json())
patch("cyc-2", {"depends_on": ""})
check("draft_id 不是整数 400",
      preview({"identities": ["u1"], "draft_id": "x"}).status_code == 400)
check("稿不存在 404", preview({"identities": ["u1"], "draft_id": 99999}).status_code == 404)
empty_draft = new_draft("空稿")
check("空稿 400", preview({"identities": ["u1"], "draft_id": empty_draft}).status_code == 400)
c.delete(f"/api/drafts/{empty_draft}", headers=H)
check("已发布的稿 409",
      preview({"identities": ["u1"], "draft_id": draft_d}).status_code == 409)
check("attrs 不是对象 400",
      preview({"identities": ["u1"], "attrs": [1], "flag": "pay",
               "changes": {"kill_switch": True}}).status_code == 400)
check("attrs 值不是标量 400",
      preview({"identities": ["u1"], "attrs": {"k": {"x": 1}}, "flag": "pay",
               "changes": {"kill_switch": True}}).status_code == 400)
r = preview({"identities": ["u1"], "attrs": {}, "flag": "pay",
             "changes": {"kill_switch": True}})
check("空 attrs 等价不带（200 且不回显）",
      r.status_code == 200 and "attrs" not in r.get_json())

print("== 12. changed 的口径：理由变也算变，只改描述不算 ==")
r = preview({"identities": ["u1"], "flag": "pay",
             "changes": {"rollout_percent": 100}})
b = r.get_json()["identities"]["u1"]
check("pay 仍开但理由 default→rollout：算变",
      b["current"]["pay"]["reason"] == "default"
      and b["preview"]["pay"]["reason"] == "rollout"
      and "pay" in b["changed"], b["changed"])
r = preview({"identities": ["u1"], "flag": "pay",
             "changes": {"description": "改个描述"}})
b = r.get_json()
check("只改描述：结果全不变", b["identities"]["u1"]["changed"] == [])
check("changes 回显描述", b["changes"] == {"description": "改个描述"})
r = preview({"identities": ["u1"], "flag": "pay",
             "changes": {"config": {"x": 1}, "depends_on": "dep-base",
                         "targeting": {"plan": "vip"}}})
b = r.get_json()
check("changes 回显规范化后的值",
      b["changes"]["config"] == {"x": 1}
      and b["changes"]["depends_on"] == "dep-base"
      and b["changes"]["targeting"] == {"plan": "vip"}, b["changes"])
check("预演后 pay 真没依赖", check_flag("pay", "u1")["reason"] == "default")

print("== 13. 特殊字符身份、预演后真改与预演一致 ==")
r = preview({"identities": ["u/1 x", " 空格 "], "flag": "promo2",
             "changes": {"default_enabled": True}})
check("特殊字符身份能预演",
      r.status_code == 200 and "u/1 x" in r.get_json()["identities"]
      and " 空格 " in r.get_json()["identities"], r.get_json())
r = preview({"identities": ["u11"], "flag": "promo2",
             "changes": {"default_enabled": True, "rollout_percent": 100}})
prev = r.get_json()["identities"]["u11"]["preview"]
patch("promo2", {"default_enabled": True, "rollout_percent": 100})
check("真改完后与预演一字不差", bundle("u11")["flags"] == prev,
      diff_maps(bundle("u11")["flags"], prev))

print("== 14. 原来的问法都不能坏 ==")
r = c.get("/api/flags/pay/check?identity=u1")
check("单查没坏", r.status_code == 200
      and {"flag", "identity", "enabled", "reason"} <= set(r.get_json()))
r = c.get("/api/flags/pay/check?identity=u1&attrs=%7B%22plan%22%3A%22pro%22%7D")
check("带属性单查没坏",
      r.status_code == 200 and r.get_json()["attrs"] == {"plan": "pro"})
r = c.get("/api/bundle?identity=u1")
check("整包没坏", r.status_code == 200
      and "version" in r.get_json() and "flags" in r.get_json())
r = c.get(f"/api/history?identity=u1&at={time.time()}")
check("问过去时刻没坏", r.status_code == 200
      and r.get_json()["flags"]["pay"]["enabled"] is True, r.get_json())
check("历史里 pay 带发布后那份配置",
      r.get_json()["flags"]["pay"]["config"] == {"color": "green"})

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
