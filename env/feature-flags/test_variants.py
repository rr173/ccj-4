"""定档（variants）的端到端测试。

覆盖需求（口语版）：
1. 管理端能给一个开关定几档，每档有一个名字和一个比例，几档加起来必须
   刚好满（100）。来问这个人落到哪一档，就带回这一档的名字。
2. 同一人、同一身属性，落到哪一档不能换（分桶只认开关与身份，与带不带
   属性无关）。
3. 没定档的开关还是只回答开或关（响应里没有 variant 这个键）。
4. 定档以后改了某一档的名字或比例，再来问要按新的（分桶不变、区间重划、
   改名只换带回去的名字）；任何一档改动都让整包换新版本，拿着改前那包
   来问说过期。
5. 全关、冻住、依赖关、单人强制关压过定档（没有档名）；强制开 / 属性
   命中 / 互斥组争胜这些「开」的路径也带档名；被别的开关依赖时只看开/关。
6. 冻住的人拿冻住那一刻的档名，改档不动他。
7. 定时生效、发布稿、预演、历史重放、跨环境推送都能带档。
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
flag_app.init_environment_db("other", "test")


class _EnvClient(flag_app.FlaskClient):
    def open(self, *args, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault("X-Environment", "test")
        kwargs["headers"] = headers
        return super().open(*args, **kwargs)

    def request(self, method, url, *args, **kwargs):
        # 兼容旧 Werkzeug：client.request("DELETE", url, ...) -> open(url, method=...)
        kwargs["method"] = method
        return self.open(url, *args, **kwargs)


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


def uid_with_bucket(flag, pred, prefix="uv"):
    """找一个对 flag 分桶满足 pred 的测试身份（确定性，可复算）。"""
    i = 0
    while True:
        uid = f"{prefix}{i}"
        if pred(flag_app.bucket_of(flag, uid)):
            return uid
        i += 1


# ---------------------------------------------------------------- 准备
c.post("/api/flags", headers=H, json={"name": "exp"})          # 定档开关
c.post("/api/flags", headers=H, json={"name": "plain",
                                      "default_enabled": True})  # 不定档
c.post("/api/flags", headers=H, json={"name": "dep"})          # 被依赖开关

VARIANTS = [{"name": "control", "percent": 50},
            {"name": "treat-a", "percent": 30},
            {"name": "treat-b", "percent": 20}]
r = patch("exp", {"variants": VARIANTS})
check("定档 50/30/20 立即生效", r.status_code == 200, r.get_json())
check("管理端列表回显各档（名字、比例、顺序）",
      get_flag("exp")["variants"] == VARIANTS, get_flag("exp")["variants"])

print("== 1. 写入校验：名字非空不重复、比例 0-100、加起来恰好 100 ==")
r = patch("exp", {"variants": [{"name": "a", "percent": 60},
                               {"name": "b", "percent": 30}]})
check("加起来 90 不收（400），且不改现网",
      r.status_code == 400 and get_flag("exp")["variants"] == VARIANTS,
      (r.status_code, r.get_json()))
r = patch("exp", {"variants": [{"name": "a", "percent": 101}]})
check("单档比例 101 不收", r.status_code == 400, r.get_json())
r = patch("exp", {"variants": [{"name": "a", "percent": 100},
                               {"name": "a", "percent": 0}]})
check("档名重复不收", r.status_code == 400, r.get_json())
r = patch("exp", {"variants": [{"name": "", "percent": 100}]})
check("空名字不收", r.status_code == 400, r.get_json())
r = patch("exp", {"variants": [{"name": "zero", "percent": 0},
                               {"name": "full", "percent": 100}]})
check("0% 的档合法（先占位，谁也落不进去）",
      r.status_code == 200 and get_flag("exp")["variants"][0]["percent"] == 0,
      r.get_json())
patch("exp", {"variants": VARIANTS})  # 还原
r = patch("exp", {"variants": []})
check("空数组语法本身合法（下面再定回来）", r.status_code == 200, r.get_json())
patch("exp", {"variants": VARIANTS})

print("== 2. 落档：分桶划区间，带回档名；同一人恒定、与属性无关 ==")
u_ctrl = uid_with_bucket("exp", lambda b: b < 50)
u_a = uid_with_bucket("exp", lambda b: 50 <= b < 80)
u_b = uid_with_bucket("exp", lambda b: b >= 80)
r = check_flag("exp", u_ctrl)
check("桶值 <50 -> control，开，reason=variants，带 variant",
      r["enabled"] is True and r["reason"] == "variants"
      and r.get("variant") == "control", r)
r = check_flag("exp", u_a)
check("50<=桶值<80 -> treat-a",
      r["enabled"] is True and r.get("variant") == "treat-a", r)
r = check_flag("exp", u_b)
check("桶值>=80 -> treat-b",
      r["enabled"] is True and r.get("variant") == "treat-b", r)
r1, r2 = check_flag("exp", u_a, {"plan": "pro"}), check_flag("exp", u_a, {"plan": "free"})
r3 = check_flag("exp", u_a)
check("同一人带不同属性 / 不带属性，档不换",
      r1["variant"] == r2["variant"] == r3["variant"] == "treat-a", (r1, r2, r3))
many = [check_flag("exp", f"stable{i}")["variant"] for i in range(50)]
again = [check_flag("exp", f"stable{i}")["variant"] for i in range(50)]
check("问多少次结果都一样（纯函数）", many == again)
b = bundle(u_ctrl)
check("整包里定档开关也带 variant",
      b["flags"]["exp"].get("variant") == "control"
      and b["flags"]["exp"]["reason"] == "variants", b["flags"]["exp"])

print("== 3. 没定档的开关只回答开/关，没有 variant 键 ==")
r = check_flag("plain", "u1")
check("不定档 + 默认开 -> enabled=true，无 variant 键",
      r["enabled"] is True and "variant" not in r, r)
r = bundle("u1")
check("整包里不定档的开关也没有 variant 键",
      "variant" not in r["flags"]["plain"], r["flags"]["plain"])

print("== 4. 改名 / 改比例立即按新的算；整包换新版本 ==")
b_before = bundle(u_a)
patch("exp", {"variants": [{"name": "control", "percent": 50},
                           {"name": "treat-AX", "percent": 30},
                           {"name": "treat-b", "percent": 20}]})
r = check_flag("exp", u_a)
check("只改名字：桶不换，带回去的名字换成新的",
      r["variant"] == "treat-AX", r)
b_after = bundle(u_a, version=b_before["version"])
check("改名字后旧整包过期，新包带新档名",
      b_after["valid"] is False
      and b_after["version"] != b_before["version"]
      and b_after["flags"]["exp"]["variant"] == "treat-AX",
      (b_before["version"], b_after))
# 改比例：50/30/20 -> 20/30/50，u_a 的桶 50-79 落进新的第二档（20-49 是
# control，50-79 仍是 treat-AX），u_ctrl 中桶值落在 20-49 的人会移到 treat-AX
patch("exp", {"variants": [{"name": "control", "percent": 20},
                           {"name": "treat-AX", "percent": 30},
                           {"name": "treat-b", "percent": 50}]})
moved = uid_with_bucket("exp", lambda b: 20 <= b < 50)
# 旧档（50/30/20）第二档区间 [50,80)，新档（20/30/50）第二档区间 [20,50)：
# 要验证「没移走的人」需找两边都在第二档的桶，但两个区间不相交，所以改成
# 验证区间语义本身：桶 [50,80) 的人在新档落进 treat-b（新第三档 [50,100)）
old_a_now_b = uid_with_bucket("exp", lambda b: 50 <= b < 80)
check("改比例：桶 20-49 的人移到 treat-AX；桶 50-79 的人移到 treat-b（区间重划）",
      check_flag("exp", moved)["variant"] == "treat-AX"
      and check_flag("exp", old_a_now_b)["variant"] == "treat-b",
      (check_flag("exp", moved), check_flag("exp", old_a_now_b)))
check("改比例也让旧包过期",
      bundle(u_ctrl, version=b_before["version"])["valid"] is False)
# 清空档：开关恢复只回答开/关
patch("exp", {"variants": []})
r = check_flag("exp", u_ctrl)
check("清空档后没有 variant 键，落回默认（默认关 -> 关）",
      "variant" not in r and r["enabled"] is False and r["reason"] == "default", r)
patch("exp", {"variants": VARIANTS})

print("== 5. 定档与更高优先级层 ==")
# 全关
patch("exp", {"kill_switch": True})
r = check_flag("exp", u_ctrl)
check("全关压过定档：关、reason=kill_switch、无档名",
      not r["enabled"] and r["reason"] == "kill_switch" and "variant" not in r, r)
patch("exp", {"kill_switch": False})
# 依赖关：exp 依赖 dep；dep 默认关 -> exp 一律关
patch("exp", {"depends_on": "dep"})
r = check_flag("exp", u_ctrl)
check("被依赖开关关着：关、reason=depends_on、无档名",
      not r["enabled"] and r["reason"] == "depends_on" and "variant" not in r, r)
patch("dep", {"rollout_percent": 100})
r = check_flag("exp", u_ctrl)
check("被依赖开关开着：照常定档，带 control",
      r["enabled"] and r.get("variant") == "control", r)
patch("exp", {"depends_on": ""})
# 单人强制关 / 强制开
c.put("/api/flags/exp/overrides", headers=H,
      json={"identity": u_ctrl, "enabled": False})
r = check_flag("exp", u_ctrl)
check("单人强制关：关、reason=override、无档名",
      not r["enabled"] and r["reason"] == "override" and "variant" not in r, r)
c.put("/api/flags/exp/overrides", headers=H,
      json={"identity": u_b, "enabled": True})
r = check_flag("exp", u_b)
check("单人强制开：开、reason=override，但档名仍按同一分桶补算（treat-b）",
      r["enabled"] and r["reason"] == "override"
      and r.get("variant") == "treat-b", r)
c.request("DELETE", f"/api/flags/exp/overrides", headers=H,
          json={"identity": u_ctrl})
c.request("DELETE", f"/api/flags/exp/overrides", headers=H,
          json={"identity": u_b})
# 属性打开条件排在定档层之前：命中属性的人由属性层定论为开，但档名仍按
# 同一分桶补上；对不上属性的人照常落定档层
patch("exp", {"targeting": {"plan": "pro"}})
r = check_flag("exp", u_ctrl, {"plan": "pro"})
check("属性命中：reason=targeting、开，档名仍按同一分桶带上",
      r["reason"] == "targeting" and r.get("variant") == "control", r)
r = check_flag("exp", u_ctrl, {"plan": "free"})
check("属性没对上：走定档层（variants），不落默认",
      r["reason"] == "variants" and r.get("variant") == "control", r)
patch("exp", {"targeting": {}})
# 互斥组：没争到组的判关、无档名；争到的带档名
c.post("/api/groups", headers=H, json={"name": "g1"})
c.put("/api/groups/g1/flags", headers=H, json={"flag": "exp"})
c.put("/api/groups/g1/flags", headers=H, json={"flag": "dep"})
# dep 对所有人 100% 开；整包按名字序求值，dep 先落定，exp 争不到
r = bundle("grp-user")
check("互斥组：exp 没争到 -> 关、reason=group、无档名；dep 争到 -> 开",
      r["flags"]["exp"]["enabled"] is False
      and r["flags"]["exp"]["reason"] == "group"
      and "variant" not in r["flags"]["exp"]
      and r["flags"]["dep"]["enabled"] is True, r["flags"])
c.request("DELETE", "/api/groups/g1/flags", headers=H, json={"flag": "dep"})
c.request("DELETE", "/api/groups/g1/flags", headers=H, json={"flag": "exp"})
c.delete("/api/groups/g1", headers=H)
# 被别的开关依赖时，依赖者只看开/关，不看档名
c.post("/api/flags", headers=H, json={"name": "leaf"})
patch("leaf", {"depends_on": "exp", "default_enabled": True})
r = check_flag("leaf", u_ctrl)
check("依赖者叶子开时没有档名（档只属于 exp 自己）",
      r["enabled"] is True and "variant" not in r, r)
patch("leaf", {"depends_on": ""})

print("== 6. 冻住拿冻住那一刻的档名快照，改档不动他 ==")
fz = c.put("/api/flags/exp/freezes", headers=H,
           json={"identity": u_a})
check("冻住返回当时档名 treat-a",
      fz.get_json().get("variant") == "treat-a", fz.get_json())
patch("exp", {"variants": [{"name": "control", "percent": 50},
                           {"name": "renamed", "percent": 30},
                           {"name": "treat-b", "percent": 20}]})
r = check_flag("exp", u_a)
check("冻住期间改名：此人仍拿冻住的 treat-a，reason=freeze",
      r["reason"] == "freeze" and r.get("variant") == "treat-a", r)
unfrozen = check_flag("exp", u_b)
check("没冻的人立即按新名（treat-b 名字没动，仍 treat-b）",
      unfrozen.get("variant") == "treat-b", unfrozen)
fl = c.get("/api/flags/exp/freezes", headers=H).get_json()
check("冻结列表带档名快照",
      next(z for z in fl if z["identity"] == u_a)["variant"] == "treat-a", fl)
c.request("DELETE", "/api/flags/exp/freezes", headers=H,
          json={"identity": u_a})
r = check_flag("exp", u_a)
check("解冻后立即按新档名（renamed）",
      r.get("variant") == "renamed", r)
# 冻在开但之后清空档：没定档的开关只回答开/关，冻住的档名也不再带回
# （快照仍在库里，历史重放还能还原）
c.put("/api/flags/exp/freezes", headers=H, json={"identity": u_b})
at_frozen = time.time()
time.sleep(0.02)
patch("exp", {"variants": []})
r = check_flag("exp", u_b)
check("清空档后，冻在开的人也只给开/关（reason=freeze、无 variant 键）",
      r["enabled"] is True and r["reason"] == "freeze" and "variant" not in r, r)
check("整包同样不带 variant",
      "variant" not in bundle(u_b)["flags"]["exp"])
hr = c.get(f"/api/history?{q(u_b)}&at={at_frozen}")
check("但历史重放冻结那一刻仍还原档名（treat-b）",
      hr.get_json()["flags"]["exp"]["variant"] == "treat-b", hr.get_json())
patch("exp", {"variants": VARIANTS})
c.request("DELETE", "/api/flags/exp/freezes", headers=H,
          json={"identity": u_b})
# 冻在关（单人强制关时冻）：没有档名
c.put("/api/flags/exp/overrides", headers=H,
      json={"identity": "offu", "enabled": False})
fz = c.put("/api/flags/exp/freezes", headers=H, json={"identity": "offu"})
check("冻在关：冻结响应无 variant", fz.get_json().get("variant") is None,
      fz.get_json())
c.request("DELETE", "/api/flags/exp/freezes", headers=H,
          json={"identity": "offu"})
c.request("DELETE", "/api/flags/exp/overrides", headers=H,
          json={"identity": "offu"})

print("== 7. 定时生效、发布稿、预演、历史重放、跨环境推送 ==")
# 定时生效：约一个「改成 100% 单档 full」的未来时刻
eff = time.time() + 3600
r = patch("exp", {"variants": [{"name": "full", "percent": 100}],
                  "effective_at": eff})
check("定档可预约（202），到点前一切照旧",
      r.status_code == 202 and check_flag("exp", u_a)["variant"] == "treat-a",
      (r.status_code, r.get_json()))
cid = r.get_json()["change_id"]
c.delete(f"/api/scheduled-changes/{cid}", headers=H)
check("取消预约后仍按现在的档", check_flag("exp", u_a)["variant"] == "treat-a")
# 发布稿：收一个改名改动，发布前不生效，发布后整包换版本
b0 = bundle(u_b)
did = c.post("/api/drafts", headers=H, json={"note": "rename"}).get_json()["draft_id"]
sr = c.put(f"/api/drafts/{did}/flags/exp", headers=H,
           json={"variants": [{"name": "control", "percent": 50},
                              {"name": "treat-a", "percent": 30},
                              {"name": "treat-BX", "percent": 20}]})
check("定档改动可收进稿（回显还原为列表）",
      sr.status_code == 200
      and sr.get_json()["changes"]["variants"][2]["name"] == "treat-BX",
      (sr.status_code, sr.get_json()))
check("发布前来问还是老名 treat-b",
      check_flag("exp", u_b)["variant"] == "treat-b")
pub = c.post(f"/api/drafts/{did}/publish", headers=H)
check("发布成功", pub.status_code == 200, pub.get_json())
check("发布后按新名 treat-BX",
      check_flag("exp", u_b)["variant"] == "treat-BX")
check("发布让旧整包过期", bundle(u_b, version=b0["version"])["valid"] is False)
patch("exp", {"variants": VARIANTS})
# 预演：假设现在就把档改了，current/preview 各给各的档名
pr = c.post("/api/preview", headers=H, json={
    "identities": [u_b], "flag": "exp",
    "changes": {"variants": [{"name": "control", "percent": 50},
                             {"name": "treat-a", "percent": 30},
                             {"name": "preview-b", "percent": 20}]}})
pj = pr.get_json()
person = pj["identities"][u_b]
check("预演 current=老名、preview=新名，且真库不动",
      pr.status_code == 200
      and person["current"]["exp"]["variant"] == "treat-b"
      and person["preview"]["exp"]["variant"] == "preview-b"
      and "exp" in person["changed"]
      and check_flag("exp", u_b)["variant"] == "treat-b", pj)
# 预演校验同一口径：加起来不满 100 直接 400
pr_bad = c.post("/api/preview", headers=H, json={
    "identities": [u_b], "flag": "exp",
    "changes": {"variants": [{"name": "control", "percent": 90}]}})
check("预演里档位和不是 100 也拒绝（400）", pr_bad.status_code == 400, pr_bad.get_json())
# 历史重放：改成新名后，问「改名之前的时刻」仍拿老名
at_old = time.time()
time.sleep(0.02)
patch("exp", {"variants": [{"name": "control", "percent": 50},
                           {"name": "treat-a", "percent": 30},
                           {"name": "old-b", "percent": 20}]})
hr = c.get(f"/api/history?{q(u_b)}&at={at_old}")
check("历史重放：过去时刻按当时的档名（treat-b）",
      hr.status_code == 200
      and hr.get_json()["flags"]["exp"]["variant"] == "treat-b", hr.get_json())
hr2 = c.get(f"/api/history?{q(u_b)}&at={time.time()}")
check("历史重放：现在按新名 old-b",
      hr2.get_json()["flags"]["exp"]["variant"] == "old-b", hr2.get_json())
patch("exp", {"variants": VARIANTS})
# 跨环境推送：把 exp 的档推到 other，other 按源此刻的规则算
push = c.post("/api/push", headers=H,
              json={"source": "test", "target": "other", "flags": ["exp"]})
check("跨环境推送成功", push.status_code == 200, push.get_json())
ro = c.get(f"/api/flags/exp/check?{q(u_a)}",
           headers={"X-Environment": "other"})
check("目标环境按推过去的档落同一档（同一分桶 -> treat-a）",
      ro.status_code == 200 and ro.get_json().get("variant") == "treat-a",
      (ro.status_code, ro.get_json()))

print("== 8. 定档与旧放量配置互不相干地切换 ==")
patch("exp", {"rollout_percent": 30, "rollout_condition": {"plan": "pro"}})
patch("exp", {"rollout_rules": [{"condition": {"plan": "free"}, "percent": 100}]})
check("定了档时比例/条件/规矩都回显保留但不参与求值（仍 reason=variants）",
      check_flag("exp", u_ctrl, {"plan": "free"})["reason"] == "variants",
      check_flag("exp", u_ctrl, {"plan": "free"}))
patch("exp", {"variants": []})
# 清空档时规矩还在（规矩先于单条比例）：对上 free -> rollout
r = check_flag("exp", "zzz", {"plan": "free"})
check("清空档后老规矩恢复：对上 free 规矩 -> rollout",
      r["reason"] == "rollout", r)
# 连规矩一起清掉 -> 单条比例 + 放量条件恢复
patch("exp", {"rollout_rules": []})
pro_in = uid_with_bucket("exp", lambda b: b < 30, prefix="pro-in")
check("再清空规矩后单条比例+条件恢复（pro 走比例分桶）",
      check_flag("exp", pro_in, {"plan": "pro"})["reason"] == "rollout",
      check_flag("exp", pro_in, {"plan": "pro"}))
r = check_flag("exp", "zzz", {"plan": "none"})
check("都对不上落默认值", r["reason"] == "default", r)
# 还原现场
patch("exp", {"rollout_percent": 0, "rollout_condition": {}})
patch("exp", {"variants": VARIANTS})

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
