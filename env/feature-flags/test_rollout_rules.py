"""有序放量规矩（rollout_rules）的端到端测试。

覆盖需求（口语版）：
1. 管理端能给一个开关定好几条放量规矩，一条一条往下写：每条是一串要对上的
   条件和一个比例。来问时从上往下对，对上哪条就按那条的比例开一部分、剩下
   的关；一条都对不上（含没带属性）还按这个开关原来的默认走。
2. 同一个人、同一身属性，多次来问不能变。
3. 全关、冻住、依赖、单人强制还是压过它（属性打开条件、互斥组的老规矩也
   不变）。
4. 改了某条条件或比例（含增删、调序、清空），整包换新版本，拿着改前那包
   来问说过期。
5. 定了规矩时单条比例/放量条件不参与求值，清空后老路恢复；没定规矩时老路
   一字不差。定时生效、发布稿、预演、历史重放都能带上规矩。
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
ENT = {"plan": "ent"}

# ---------------------------------------------------------------- 准备开关
c.post("/api/flags", headers=H, json={"name": "rr-feat"})                       # 默认关
RULES = [
    {"condition": {"plan": "pro"}, "percent": 50},
    {"condition": {"plan": "free"}, "percent": 100},
    {"condition": {"level": 3}, "percent": 0},
]
patch("rr-feat", {"rollout_rules": RULES})
IN = uid_with_bucket("rr-feat", lambda b: b < 50)      # 分桶命中（比例内）
OUT = uid_with_bucket("rr-feat", lambda b: b >= 50)    # 分桶未命中

print("== 1. 从上往下对：对上哪条按哪条；都对不上还按原来的默认走 ==")
check("管理端列表回显放量规矩（顺序保持）", get_flag("rr-feat")["rollout_rules"] == RULES,
      get_flag("rr-feat")["rollout_rules"])
r = check_flag("rr-feat", IN, PRO)
check("对上第一条且分桶命中 -> 开，reason=rollout",
      r["enabled"] is True and r["reason"] == "rollout", r)
r = check_flag("rr-feat", OUT, PRO)
check("对上第一条但分桶未命中 -> 关，reason=rollout（这一层定论，不落默认）",
      r["enabled"] is False and r["reason"] == "rollout", r)
r = check_flag("rr-feat", OUT, FREE)
check("对上第二条（100%）-> 开，reason=rollout",
      r["enabled"] is True and r["reason"] == "rollout", r)
r = check_flag("rr-feat", IN, {"level": 3})
check("对上第三条（0%）-> 关，reason=rollout（0% 也是定论，不落默认）",
      r["enabled"] is False and r["reason"] == "rollout", r)
r = check_flag("rr-feat", IN, ENT)
check("一条都对不上 -> 不看任何比例，按原来的默认（默认关），reason=default",
      r["enabled"] is False and r["reason"] == "default", r)
r = check_flag("rr-feat", IN)
check("没带属性也算对不上 -> 默认关", r["enabled"] is False
      and r["reason"] == "default" and "attrs" not in r, r)
# 默认开的开关：对上未命中 -> 关；对不上 -> 按原来的默认开
c.post("/api/flags", headers=H, json={"name": "rr-defon", "default_enabled": True})
patch("rr-defon", {"rollout_rules": [{"condition": PRO, "percent": 50}]})
OUT_ON = uid_with_bucket("rr-defon", lambda b: b >= 50)
r = check_flag("rr-defon", OUT_ON, PRO)
check("默认开的开关：对上未命中 -> 关（rollout 定论）",
      r["enabled"] is False and r["reason"] == "rollout", r)
r = check_flag("rr-defon", OUT_ON, FREE)
check("默认开的开关：对不上 -> 按原来的默认开，reason=default",
      r["enabled"] is True and r["reason"] == "default", r)
r = check_flag("rr-defon", OUT_ON)
check("默认开的开关：没带属性 -> 默认开", r["enabled"] is True
      and r["reason"] == "default", r)
# 分布：对上第一条的 120 个人里两边都有，且开的就是分桶 < 50 的那批
sample = [f"dist{i}" for i in range(120)]
got = {u: check_flag("rr-feat", u, PRO) for u in sample}
on_uids = {u for u, r in got.items() if r["enabled"]}
expect_on = {u for u in sample if flag_app.bucket_of("rr-feat", u) < 50}
check("对上的人里按比例开一部分、剩下的关（两边都有）",
      0 < len(on_uids) < len(sample), len(on_uids))
check("开的集合与确定性分桶完全一致", on_uids == expect_on)
check("对上的人结果都由放量层定论", all(r["reason"] == "rollout"
                                        for r in got.values()))

print("== 1b. 两条都对上：按最上面那条；调序后按新的第一条 ==")
c.post("/api/flags", headers=H, json={"name": "rr-order"})
patch("rr-order", {"rollout_rules": [
    {"condition": {"plan": "pro"}, "percent": 50},
    {"condition": {"plan": "pro", "level": 3}, "percent": 100},
]})
INO = uid_with_bucket("rr-order", lambda b: b < 50)
OUTO = uid_with_bucket("rr-order", lambda b: b >= 50)
BOTH = {"plan": "pro", "level": 3}
r = check_flag("rr-order", OUTO, BOTH)
check("两条都对上：按第一条 50%，分桶未命中 -> 关（不看后面的 100%）",
      r["enabled"] is False and r["reason"] == "rollout", r)
r = check_flag("rr-order", INO, BOTH)
check("两条都对上：按第一条 50%，分桶命中 -> 开",
      r["enabled"] is True and r["reason"] == "rollout", r)
patch("rr-order", {"rollout_rules": [
    {"condition": {"plan": "pro", "level": 3}, "percent": 100},
    {"condition": {"plan": "pro"}, "percent": 50},
]})
r = check_flag("rr-order", OUTO, BOTH)
check("调序后 100% 那条在前 -> 同一个人同一身属性变开",
      r["enabled"] is True and r["reason"] == "rollout", r)
r = check_flag("rr-order", OUTO, PRO)
check("调序后只对上第二条（50%）-> 仍按第二条分桶（未命中关）",
      r["enabled"] is False and r["reason"] == "rollout", r)

print("== 2. 同一个人、同一身属性，多次来问不能变 ==")
first = None
for i in range(5):
    # 故意打乱 key 顺序与空白
    raw = '{"plan":"pro"}' if i % 2 else '{ "plan" : "pro" }'
    r = c.get(f"/api/flags/rr-feat/check?identity={IN}"
              f"&attrs={urllib.parse.quote(raw)}").get_json()
    cur = (r["enabled"], r["reason"])
    if first is None:
        first = cur
    check(f"第{i+1}次来问结果相同", cur == first, r)
b = bundle(IN, PRO)
one = check_flag("rr-feat", IN, PRO)
check("单查与整包同一身属性结果一致",
      one["enabled"] == b["flags"]["rr-feat"]["enabled"]
      and one["reason"] == b["flags"]["rr-feat"]["reason"])
b2 = bundle(IN, PRO)
check("同人同属性再拿整包：版本与结果都不变",
      b2["version"] == b["version"] and b2["flags"] == b["flags"])
# 多键 AND + 列表 OR 的条件
c.post("/api/flags", headers=H, json={"name": "rr-multi"})
patch("rr-multi", {"rollout_rules": [
    {"condition": {"plan": ["pro", "ent"], "level": 3}, "percent": 100}]})
r = check_flag("rr-multi", "m1", {"plan": "ent", "level": 3})
check("多键 AND + 列表 OR：全对上才走这条", r["enabled"] is True
      and r["reason"] == "rollout", r)
r = check_flag("rr-multi", "m1", {"plan": "ent", "level": 4})
check("只对上一个键 -> 没对上，走默认", r["enabled"] is False
      and r["reason"] == "default", r)

print("== 3. 全关、冻住、依赖、单人强制还是压过它 ==")
# 全关
c.post("/api/flags", headers=H, json={"name": "rr-kill"})
patch("rr-kill", {"rollout_rules": [{"condition": PRO, "percent": 100}]})
check("前提：对上 100% -> 开", check_flag("rr-kill", "k1", PRO)["enabled"] is True)
patch("rr-kill", {"kill_switch": True})
r = check_flag("rr-kill", "k1", PRO)
check("全关压过放量规矩", r["enabled"] is False and r["reason"] == "kill_switch", r)
patch("rr-kill", {"kill_switch": False})
check("全关解除恢复", check_flag("rr-kill", "k1", PRO)["enabled"] is True)
# 冻住（冻住那一刻按不带属性的口径求值：都对不上走默认，所以用默认开的开关
# 才能冻在开上）
c.post("/api/flags", headers=H, json={"name": "rr-frz", "default_enabled": True})
patch("rr-frz", {"rollout_rules": [{"condition": PRO, "percent": 100}]})
r = c.put("/api/flags/rr-frz/freezes", headers=H, json={"identity": "fz1"})
check("冻住（不带属性口径：对不上走默认开）", r.get_json()["enabled"] is True,
      r.get_json())
patch("rr-frz", {"rollout_rules": [{"condition": {"plan": "nobody"}, "percent": 100}],
                 "default_enabled": False})
r = check_flag("rr-frz", "fz1", PRO)
check("冻住后改规矩、改默认也不动他（仍开，reason=freeze）",
      r["enabled"] is True and r["reason"] == "freeze", r)
vfz = bundle("fz1", PRO)["version"]
patch("rr-frz", {"rollout_rules": [{"condition": PRO, "percent": 100}]})
check("冻住期间改规矩：此人整包版本一字不变", bundle("fz1", PRO)["version"] == vfz)
c.delete("/api/flags/rr-frz/freezes", headers=H, json={"identity": "fz1"})
r = check_flag("rr-frz", "fz1", FREE)
check("解冻后按当时规则（free 对不上 -> 默认关）",
      r["enabled"] is False and r["reason"] == "default", r)
# 依赖
c.post("/api/flags", headers=H, json={"name": "rr-parent"})   # 默认关
c.post("/api/flags", headers=H, json={"name": "rr-child"})
patch("rr-child", {"rollout_rules": [{"condition": PRO, "percent": 100}],
                   "depends_on": "rr-parent"})
r = check_flag("rr-child", "d1", PRO)
check("被依赖开关关着：对上 100% 也开不了（reason=depends_on）",
      r["enabled"] is False and r["reason"] == "depends_on", r)
patch("rr-parent", {"default_enabled": True})
r = check_flag("rr-child", "d1", PRO)
check("依赖开了：放量规矩正常生效", r["enabled"] is True
      and r["reason"] == "rollout", r)
# 单人强制
c.post("/api/flags", headers=H, json={"name": "rr-ovr"})
patch("rr-ovr", {"rollout_rules": [{"condition": PRO, "percent": 100}]})
c.put("/api/flags/rr-ovr/overrides", headers=H,
      json={"identity": "ov1", "enabled": False})
r = check_flag("rr-ovr", "ov1", PRO)
check("单人强制关压过规矩命中", r["enabled"] is False
      and r["reason"] == "override", r)
c.put("/api/flags/rr-ovr/overrides", headers=H,
      json={"identity": "ov2", "enabled": True})
r = check_flag("rr-ovr", "ov2", ENT)
check("单人强制开压过「对不上走默认」", r["enabled"] is True
      and r["reason"] == "override", r)

print("== 4. 规矩命中仍过互斥组；与属性打开条件共存 ==")
c.post("/api/groups", headers=H, json={"name": "rrg"})
c.post("/api/flags", headers=H, json={"name": "rr-g1"})
c.post("/api/flags", headers=H, json={"name": "rr-g2"})
c.put("/api/groups/rrg/flags", headers=H, json={"flag": "rr-g1"})
c.put("/api/groups/rrg/flags", headers=H, json={"flag": "rr-g2"})
patch("rr-g1", {"rollout_rules": [{"condition": PRO, "percent": 100}]})
patch("rr-g2", {"rollout_rules": [{"condition": PRO, "percent": 100}]})
r1 = check_flag("rr-g1", "g-u1", PRO)
check("组内：规矩命中且自然开 -> 落定（reason=group）",
      r1["enabled"] is True and r1["reason"] == "group", r1)
r2 = check_flag("rr-g2", "g-u1", PRO)
check("组内已有人落定 -> 关（reason=group）",
      r2["enabled"] is False and r2["reason"] == "group", r2)
r3 = check_flag("rr-g1", "g-u2", ENT)
check("对不上 -> 默认关，不在组内占位",
      r3["enabled"] is False and r3["reason"] == "default", r3)
# 属性打开条件（targeting）仍排在放量规矩之前
c.post("/api/flags", headers=H, json={"name": "rr-tgt"})
patch("rr-tgt", {"targeting": {"vip": True},
                 "rollout_rules": [{"condition": PRO, "percent": 100}]})
r = check_flag("rr-tgt", "t1", {"plan": "free", "vip": True})
check("属性打开条件命中 -> 开（reason=targeting），不看规矩",
      r["enabled"] is True and r["reason"] == "targeting", r)
r = check_flag("rr-tgt", "t1", PRO)
check("属性打开条件没中、规矩对上 -> 走放量层",
      r["enabled"] is True and r["reason"] == "rollout", r)

print("== 5. 改了某条条件或比例：整包换新版本，拿着改前那包说过期 ==")
v_pro = bundle("v1", PRO)["version"]
v_none = bundle("v1")["version"]
v_ent = bundle("v1", ENT)["version"]
bundle("v2", PRO)  # 另一个人也拿过命中属性的包
t_mark = time.time()
patch("rr-feat", {"rollout_rules": [
    {"condition": {"plan": "pro"}, "percent": 60},   # 改第一条的比例
    {"condition": {"plan": "free"}, "percent": 100},
    {"condition": {"level": 3}, "percent": 0},
]}, headers=H_BOB)
b_new = bundle("v1", PRO)
check("改某条比例：命中属性的包换新版本", b_new["version"] != v_pro)
check("拿着改前那包来问 -> valid=false（过期）",
      bundle("v1", PRO, version=v_pro)["valid"] is False)
check("拿着新包来问 -> valid=true",
      bundle("v1", PRO, version=b_new["version"])["valid"] is True)
check("改某条比例：不带属性的包也换新版本（走哪层由规矩决定）",
      bundle("v1")["version"] != v_none)
check("不带属性的旧包同样过期", bundle("v1", version=v_none)["valid"] is False)
check("一条都对不上的那身属性（ent）的包也换了版本",
      bundle("v1", ENT)["version"] != v_ent)
inv = c.get("/api/bundles/invalidations?limit=300", headers=H).get_json()
mine = [x for x in inv if x["created_at"] >= t_mark]
check("改规矩产生了失效记录", len(mine) > 0)
check("失效记录的操作人是 bob", all(x["actor"] == "bob" for x in mine))
hashes = {x["attrs_hash"] for x in mine}
check("失效记录同时波及带属性的包与不带属性的包",
      "" in hashes and len(hashes) > 1, hashes)
rec = next(x for x in mine if x["identity"] == "v2")
check("记下的新版本与本人再来拿一致",
      rec["new_version"] == bundle("v2", PRO)["version"])
# 改某条条件一样换版本
v1 = bundle("v1", PRO)["version"]
patch("rr-feat", {"rollout_rules": [
    {"condition": {"plan": "pro"}, "percent": 60},
    {"condition": {"plan": "free", "level": 9}, "percent": 100},  # 改第二条的条件
    {"condition": {"level": 3}, "percent": 0},
]})
check("改某条条件：整包也换新版本", bundle("v1", PRO)["version"] != v1)
check("改条件前的包过期", bundle("v1", PRO, version=v1)["valid"] is False)
r = check_flag("rr-feat", IN, FREE)
check("改完条件：free 不再对上任何条 -> 默认关",
      r["enabled"] is False and r["reason"] == "default", r)
# 增一条、删一条、调序、清空都换版本
v1 = bundle("v1", PRO)["version"]
patch("rr-feat", {"rollout_rules": RULES + [
    {"condition": {"plan": "ent"}, "percent": 20}]})              # 增一条
check("增一条：换新版本", bundle("v1", PRO)["version"] != v1)
v1 = bundle("v1", PRO)["version"]
patch("rr-feat", {"rollout_rules": RULES[:2]})                    # 删一条
check("删一条：换新版本", bundle("v1", PRO)["version"] != v1)
v1 = bundle("v1", PRO)["version"]
patch("rr-feat", {"rollout_rules": [RULES[1], RULES[0]]})         # 调序
check("调序：换新版本", bundle("v1", PRO)["version"] != v1)
v1 = bundle("v1", PRO)["version"]
patch("rr-feat", {"rollout_rules": []})                           # 清空
check("空列表清空规矩：换新版本", bundle("v1", PRO)["version"] != v1)
check("清空后列表回显空规矩", get_flag("rr-feat")["rollout_rules"] == [])
r = check_flag("rr-feat", IN, PRO)
check("清空后没定规矩：比例 0 未启用放量 -> 默认关",
      r["enabled"] is False and r["reason"] == "default", r)
patch("rr-feat", {"rollout_rules": RULES})                        # 恢复

print("== 6. 规矩形式与校验 ==")
cur = get_flag("rr-feat")["rollout_rules"]
bad_rules = [
    "notalist", 42, {"condition": PRO, "percent": 50},            # 不是列表
    ["x"],                                                        # 元素不是对象
    [{"condition": PRO}],                                         # 缺 percent
    [{"percent": 50}],                                            # 缺 condition
    [{"condition": PRO, "percent": 50, "x": 1}],                  # 多出来的键
    [{"condition": {}, "percent": 50}],                           # 空条件
    [{"condition": None, "percent": 50}],                         # 条件为 null
    [{"condition": {"k": []}, "percent": 50}],                    # 条件值空列表
    [{"condition": {"k": {"x": 1}}, "percent": 50}],              # 条件值非标量
    [{"condition": PRO, "percent": -1}],                          # 比例越界
    [{"condition": PRO, "percent": 101}],
    [{"condition": PRO, "percent": "50"}],                        # 比例不是整数
    [{"condition": PRO, "percent": 50.5}],
    [{"condition": PRO, "percent": True}],                        # 布尔不是整数
    [{"condition": PRO, "percent": 50}] * 101,                    # 超过 100 条
]
for bad in bad_rules:
    r = patch("rr-feat", {"rollout_rules": bad})
    check(f"非法放量规矩 400：{json.dumps(bad, ensure_ascii=False)[:60]}",
          r.status_code == 400, r.status_code)
check("校验失败后原规矩还在", get_flag("rr-feat")["rollout_rules"] == cur)
r = c.post("/api/flags", headers=H,
           json={"name": "rr-bad", "rollout_rules": [{"condition": {}}]})
check("建开关带非法规矩 400", r.status_code == 400)
r = patch("rr-feat", {"rollout_rules": [{"condition": {}, "percent": 50}],
                      "effective_at": time.time() + 60})
check("定时非法规矩 400", r.status_code == 400)
r = patch("rr-feat", {"rollout_rules": RULES}, headers={"X-Actor": "x"})
check("定放量规矩接口要鉴权", r.status_code == 401)
r = c.put("/api/drafts", headers=H)  # 占位防误用（无意义请求不校验，跳过）
# null 也清除规矩
patch("rr-feat", {"rollout_rules": None})
check("null 清除规矩", get_flag("rr-feat")["rollout_rules"] == [])
patch("rr-feat", {"rollout_rules": RULES})                        # 恢复

print("== 7. 定了规矩时单条比例/放量条件不参与；清空后老路恢复 ==")
c.post("/api/flags", headers=H, json={"name": "rr-mix"})
patch("rr-mix", {"rollout_percent": 100, "rollout_condition": PRO})
MIN = uid_with_bucket("rr-mix", lambda b: True)
check("老路前提：pro 对上放量条件 100% -> 开",
      check_flag("rr-mix", MIN, PRO)["enabled"] is True)
patch("rr-mix", {"rollout_rules": [{"condition": FREE, "percent": 0}]})
r = check_flag("rr-mix", MIN, PRO)
check("定了规矩：pro 对不上规矩 -> 默认关（老的 100% 条件放量不参与）",
      r["enabled"] is False and r["reason"] == "default", r)
r = check_flag("rr-mix", MIN, FREE)
check("定了规矩：free 对上 0% -> 关（rollout 定论）",
      r["enabled"] is False and r["reason"] == "rollout", r)
patch("rr-mix", {"rollout_rules": []})
r = check_flag("rr-mix", MIN, PRO)
check("清空规矩：老路恢复（pro 对上放量条件 100% -> 开）",
      r["enabled"] is True and r["reason"] == "rollout", r)

print("== 8. 定时生效：到点前照旧，到点按新规矩 ==")
c.post("/api/flags", headers=H, json={"name": "rr-sched"})
patch("rr-sched", {"rollout_rules": [{"condition": PRO, "percent": 100}]})
b0 = bundle("s1", {"plan": "beta"})
check("到点前 beta 属性对不上 -> 默认关",
      b0["flags"]["rr-sched"]["enabled"] is False)
r = patch("rr-sched", {"rollout_rules": [{"condition": {"plan": "beta"},
                                         "percent": 100}],
                       "effective_at": time.time() + 0.4})
check("约放量规矩返回 202", r.status_code == 202
      and r.get_json().get("scheduled") is True, r.status_code)
check("到点前仍按旧规矩", check_flag("rr-sched", "s9", {"plan": "beta"})
      ["enabled"] is False)
check("到点前整包版本不换", bundle("s1", {"plan": "beta"})["version"] == b0["version"])
time.sleep(0.6)
r = check_flag("rr-sched", "s9", {"plan": "beta"})
check("到点后按新规矩命中（100% 开）", r["enabled"] is True
      and r["reason"] == "rollout", r)
b1 = bundle("s1", {"plan": "beta"})
check("到点后整包换新版本", b1["version"] != b0["version"])
check("到点前那包过期",
      bundle("s1", {"plan": "beta"}, version=b0["version"])["valid"] is False)

print("== 9. 发布稿：收进稿不生效，发布一起生效 ==")
c.post("/api/flags", headers=H, json={"name": "rr-draft"})
r = c.post("/api/drafts", headers=H, json={"note": "放量规矩一揽子"})
did = r.get_json()["draft_id"]
DRULES = [{"condition": PRO, "percent": 100}]
r = c.put(f"/api/drafts/{did}/flags/rr-draft", headers=H,
          json={"rollout_rules": DRULES})
check("放量规矩能收进稿", r.status_code == 200
      and r.get_json()["changes"]["rollout_rules"] == DRULES, r.get_json())
r = c.put(f"/api/drafts/{did}/flags/rr-draft", headers=H,
          json={"rollout_rules": [{"condition": {}}]})
check("非法放量规矩收不进稿", r.status_code == 400)
check("发布前：对上的人还是默认关（稿不生效）",
      check_flag("rr-draft", "dr1", PRO)["enabled"] is False)
r = c.post(f"/api/drafts/{did}/publish", headers=H)
check("发布成功", r.status_code == 200 and r.get_json()["published"] is True,
      r.get_json())
r = check_flag("rr-draft", "dr1", PRO)
check("发布后：对上的人走放量层（100% 开）",
      r["enabled"] is True and r["reason"] == "rollout", r)
r = check_flag("rr-draft", "dr1", ENT)
check("发布后：对不上的人走默认关",
      r["enabled"] is False and r["reason"] == "default", r)
d = c.get(f"/api/drafts/{did}", headers=H).get_json()
check("稿详情回显放量规矩",
      d["flags"]["rr-draft"]["rollout_rules"] == DRULES, d["flags"]["rr-draft"])

print("== 10. 预演：先看一眼，真的什么都不动 ==")
c.post("/api/flags", headers=H, json={"name": "rr-prev"})
before = check_flag("rr-prev", "p1", PRO)
check("前提：没定规矩 -> 默认关", before["enabled"] is False
      and before["reason"] == "default", before)
r = c.post("/api/preview", headers=H, json={
    "identities": ["p1"], "flag": "rr-prev",
    "changes": {"rollout_rules": [{"condition": PRO, "percent": 100}]},
    "attrs": PRO})
j = r.get_json()
check("预演返回 200", r.status_code == 200, r.status_code)
p = j["identities"]["p1"]
check("预演：定了规矩后 pro 对上 -> 会变成开",
      p["preview"]["rr-prev"]["enabled"] is True
      and p["preview"]["rr-prev"]["reason"] == "rollout", p["preview"])
check("预演：current 还是现在的关", p["current"]["rr-prev"]["enabled"] is False)
check("changed 里列出本开关", "rr-prev" in p["changed"], p["changed"])
check("预演后真开关没动（仍没有规矩）", get_flag("rr-prev")["rollout_rules"] == [])
check("预演后来问结果不变", check_flag("rr-prev", "p1", PRO)["enabled"] is False)
r = c.post("/api/preview", headers=H, json={
    "identities": ["p1"], "flag": "rr-prev",
    "changes": {"rollout_rules": [{"condition": {}}]}})
check("预演非法放量规矩 400", r.status_code == 400)

print("== 11. 指定过去时刻问：当时还没定规矩按当时算 ==")
c.post("/api/flags", headers=H, json={"name": "rr-hist"})
t0 = time.time()
time.sleep(0.05)
check("现在：没定规矩，pro 来问默认关",
      check_flag("rr-hist", "h1", PRO)["enabled"] is False)
patch("rr-hist", {"rollout_rules": [{"condition": PRO, "percent": 100}]})
time.sleep(0.05)
t1 = time.time()
r = c.get(f"/api/history?identity=h1&at={t0}"
          f"&attrs={urllib.parse.quote(json.dumps(PRO))}").get_json()
check("问定规矩之前：pro 当时默认关",
      r["flags"]["rr-hist"]["enabled"] is False
      and r["flags"]["rr-hist"]["reason"] == "default", r["flags"]["rr-hist"])
r = c.get(f"/api/history?identity=h1&at={t1}"
          f"&attrs={urllib.parse.quote(json.dumps(PRO))}").get_json()
check("问定规矩之后：pro 对上 -> 放量层开",
      r["flags"]["rr-hist"]["enabled"] is True
      and r["flags"]["rr-hist"]["reason"] == "rollout", r["flags"]["rr-hist"])
r = c.get(f"/api/history?identity=h1&at={t1}"
          f"&attrs={urllib.parse.quote(json.dumps(ENT))}").get_json()
check("问定规矩之后：ent 对不上 -> 默认关",
      r["flags"]["rr-hist"]["enabled"] is False
      and r["flags"]["rr-hist"]["reason"] == "default", r["flags"]["rr-hist"])

print("== 12. 建开关时直接带规矩；老路不坏 ==")
c.post("/api/flags", headers=H,
       json={"name": "rr-born", "rollout_rules": [{"condition": PRO, "percent": 100}]})
check("建开关时直接带规矩：列表回显",
      get_flag("rr-born")["rollout_rules"] == [{"condition": PRO, "percent": 100}])
r = check_flag("rr-born", "nb1", PRO)
check("建开关时直接带规矩：对上即开", r["enabled"] is True
      and r["reason"] == "rollout", r)
r = check_flag("rr-born", "nb1", ENT)
check("建开关时直接带规矩：对不上走默认",
      r["enabled"] is False and r["reason"] == "default", r)
# 没定规矩的开关：单条比例 + 放量条件的老路一字不差
c.post("/api/flags", headers=H, json={"name": "rr-plain"})
patch("rr-plain", {"rollout_percent": 50, "rollout_condition": PRO})
PIN = uid_with_bucket("rr-plain", lambda b: b < 50)
r = check_flag("rr-plain", PIN, PRO)
check("没定规矩：对上放量条件走老路放量层", r["enabled"] is True
      and r["reason"] == "rollout", r)
r = check_flag("rr-plain", PIN, FREE)
check("没定规矩：对不上放量条件走默认", r["enabled"] is False
      and r["reason"] == "default", r)
r = c.get("/api/flags/rr-plain/check")
check("check 缺 identity 仍 400", r.status_code == 400)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
