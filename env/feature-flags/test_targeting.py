"""属性打开条件（targeting）的端到端测试。

覆盖需求：
1. 来问开关时能带上这个人身上的属性（check / bundle 都能带）
2. 管理端能按这些属性给某个开关定打开条件：对上了才开，对不上按原规则
3. 同一人、同一身属性，多次来问结果不变（确定性，key 顺序无关）
4. 管理端改了打开条件，再来问按新的，整包换新版本；拿着改前那包来问说过期
5. 不带属性来问、一次问一个（check）、拿整包，三条老路都不能坏
6. 优先级：全关 > 单人强制 > 属性条件 > 互斥组 > 放量 > 默认值
7. 属性条件也能预约定时生效
8. 条件增改只让带过属性的整包失效，不带属性的老包版本一字不变
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
    """构造 check/bundle 的查询串（attrs 走 URL 编码 JSON）。"""
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


def set_targeting(flag, rule, headers=H, **extra):
    body = {"targeting": rule}
    body.update(extra)
    return c.patch(f"/api/flags/{flag}", headers=headers, json=body)


# ---------------------------------------------------------------- 准备开关
c.post("/api/flags", headers=H, json={"name": "feat-a"})                       # 默认关
c.post("/api/flags", headers=H, json={"name": "feat-b", "default_enabled": True})
c.patch("/api/flags/feat-b", headers=H, json={"rollout_percent": 50})
c.post("/api/flags", headers=H, json={"name": "feat-c",
        "targeting": {"plan": "pro"}})  # 建开关时直接定条件
c.post("/api/flags", headers=H, json={"name": "grp-x"})
c.post("/api/flags", headers=H, json={"name": "grp-y", "default_enabled": True})
c.post("/api/groups", headers=H, json={"name": "g1"})
c.put("/api/groups/g1/flags", headers=H, json={"flag": "grp-x"})
c.put("/api/groups/g1/flags", headers=H, json={"flag": "grp-y"})

PRO = {"plan": "pro", "level": 3, "vip": True}
FREE = {"plan": "free", "level": 0}

print("== 1. 管理端定条件：对上了才开，对不上按原规则 ==")
r = set_targeting("feat-a", {"plan": "pro"})
check("定条件返回 200", r.status_code == 200, r.status_code)
check("管理端列表回显条件",
      next(f for f in c.get("/api/flags", headers=H).get_json()
           if f["name"] == "feat-a")["targeting"] == {"plan": "pro"})
code, r = check_flag("feat-a", "u1", PRO)
check("对上条件 -> 开，reason=targeting",
      code == 200 and r["enabled"] is True and r["reason"] == "targeting", r)
check("响应回显带来的属性", r.get("attrs") == PRO)
code, r = check_flag("feat-a", "u1", FREE)
check("对不上 -> 按原规则（默认关），reason=default",
      code == 200 and r["enabled"] is False and r["reason"] == "default", r)
code, r = check_flag("feat-a", "u1")
check("没带属性 -> 按原规则，reason=default",
      code == 200 and r["enabled"] is False and r["reason"] == "default", r)
check("不带属性的响应不出现 attrs 字段", "attrs" not in r)

print("== 2. 多键 AND、值列表 OR、精确匹配 ==")
set_targeting("feat-a", {"plan": "pro", "level": 3})
code, r = check_flag("feat-a", "u1", PRO)
check("两个条件都对上 -> 开", r["enabled"] is True, r)
code, r = check_flag("feat-a", "u1", {"plan": "pro", "level": 9})
check("只对上一个 -> 不开（AND）", r["enabled"] is False, r)
set_targeting("feat-a", {"plan": ["pro", "enterprise"]})
code, r = check_flag("feat-a", "u1", {"plan": "enterprise"})
check("列表值命中其一 -> 开（OR）", r["enabled"] is True, r)
code, r = check_flag("feat-a", "u1", {"plan": "team"})
check("列表值都不中 -> 不开", r["enabled"] is False, r)
code, r = check_flag("feat-a", "u1", {"plan": "pro", "extra": 1})
check("属性多带不影响对上", r["enabled"] is True, r)
code, r = check_flag("feat-a", "u1", {"other": "pro"})
check("缺键 -> 不开", r["enabled"] is False, r)
set_targeting("feat-a", {"vip": True})
code, r = check_flag("feat-a", "u1", {"vip": 1})
check("true 与 1 不互等 -> 不开", r["enabled"] is False, r)
code, r = check_flag("feat-a", "u1", {"vip": True})
check("true 与 true -> 开", r["enabled"] is True, r)
set_targeting("feat-a", {"level": 3})
code, r = check_flag("feat-a", "u1", {"level": 3.0})
check("3 与 3.0 相等 -> 开", r["enabled"] is True, r)
code, r = check_flag("feat-a", "u1", {"level": "3"})
check("3 与 \"3\" 不等 -> 不开", r["enabled"] is False, r)
code, r = check_flag("feat-a", "u1", {"level": None})
check("null 属性不命中数字条件", r["enabled"] is False, r)
set_targeting("feat-a", {"note": None})
code, r = check_flag("feat-a", "u1", {"note": None})
check("条件值可以是 null 并精确对上", r["enabled"] is True, r)

print("== 3. 同一人同一身属性：多次来问结果不变（确定性） ==")
set_targeting("feat-a", {"plan": "pro", "level": [3, 5]})
first = None
for i in range(4):
    # 故意打乱 JSON key 顺序、加空白
    raw = '{"level": 3, "plan": "pro"}' if i % 2 else '{"plan":"pro","level":3}'
    url = f"/api/flags/feat-a/check?identity=u1&attrs={urllib.parse.quote(raw)}"
    r = c.get(url).get_json()
    if first is None:
        first = (r["enabled"], r["reason"])
    check(f"第{i+1}次来问结果一致（key 顺序无关）",
          (r["enabled"], r["reason"]) == (True, "targeting"), first)
# 换个人带同样属性也开；同一个人换属性结果可以不同
check("另一个人同样属性也开", check_flag("feat-a", "u2", PRO)[1]["enabled"] is True)
check("同一人换属性结果可以不同",
      check_flag("feat-a", "u1", FREE)[1]["enabled"] is False)

print("== 4. 优先级：全关 / 单人强制 高于属性条件 ==")
code, r = check_flag("feat-a", "u1", PRO)
check("前提：属性命中为开", r["enabled"] is True)
c.patch("/api/flags/feat-a", headers=H, json={"kill_switch": True})
code, r = check_flag("feat-a", "u1", PRO)
check("全关压过属性命中 -> 关，reason=kill_switch",
      r["enabled"] is False and r["reason"] == "kill_switch", r)
c.patch("/api/flags/feat-a", headers=H, json={"kill_switch": False})
c.put("/api/flags/feat-a/overrides", headers=H,
      json={"identity": "u1", "enabled": False})
code, r = check_flag("feat-a", "u1", PRO)
check("单人强制关压过属性命中 -> 关，reason=override",
      r["enabled"] is False and r["reason"] == "override", r)
code, r = check_flag("feat-a", "u2", PRO)
check("只强制了 u1：u2 属性命中仍开", r["enabled"] is True and r["reason"] == "targeting")
c.delete("/api/flags/feat-a/overrides", headers=H, json={"identity": "u1"})

print("== 5. 属性条件高于放量 / 默认值 ==")
# feat-b: 50% 放量、默认开。属性条件命中时不看分桶，直接开；
# 对不上时按放量/默认值的原规则走。
set_targeting("feat-b", {"plan": "pro"})
buckets_on = buckets_off = 0
sample_ids = [f"user-{i}" for i in range(60)]
for uid in sample_ids:
    hit = check_flag("feat-b", uid, PRO)[1]
    check_all = hit["enabled"] is True and hit["reason"] == "targeting"
    buckets_on += check_all
check("60 个用户带命中属性全部由属性层开（不看分桶）", buckets_on == 60, buckets_on)
missed = [check_flag("feat-b", uid, FREE)[1] for uid in sample_ids]
check("对不上的全部回落放量层（reason=rollout，结果随分桶）",
      all(r["reason"] == "rollout" for r in missed))
check("对不上时开/关分布两边都有（分桶确实在起作用）",
      any(r["enabled"] for r in missed) and not all(r["enabled"] for r in missed))
check("属性条件没动开关的放量配置",
      next(f for f in c.get("/api/flags", headers=H).get_json()
           if f["name"] == "feat-b")["rollout_percent"] == 50)

print("== 6. 属性命中仍过互斥组裁决（与放量同一语义） ==")
set_targeting("grp-x", {"plan": "pro"})
# u3 先带属性把 grp-x 在组内落定
code, r1 = check_flag("grp-x", "u3", PRO)
check("第一次属性命中且组内空位 -> 开，落定（reason=group）",
      r1["enabled"] is True and r1["reason"] == "group", r1)
code, r2 = check_flag("grp-y", "u3", PRO)
check("组内已有别人落定 -> 关，reason=group（属性/默认开都要让位）",
      r2["enabled"] is False and r2["reason"] == "group", r2)
code, r3 = check_flag("grp-x", "u3", FREE)
check("同一人换属性后对不上 -> 自然结果为关，reason=default",
      r3["enabled"] is False and r3["reason"] == "default")
code, r4 = check_flag("grp-x", "u3", PRO)
check("再换回同一身属性 -> 还是开（结果恒定；落定只认身份，reason=group）",
      r4["enabled"] is True and r4["reason"] == "group", r4)
# 组内空位、没有落定记录时，属性命中且无组竞争 -> 直接由属性层定论
code, r5 = check_flag("grp-x", "u4", PRO)
check("另一人首次问 grp-x 也是组落定开", r5["enabled"] is True, r5)
# 一个不在组内、默认关的开关：属性命中不经过组，reason 就是 targeting
code, r6 = check_flag("feat-c", "u3", PRO)
check("不在组内：属性命中直接 targeting 层定论",
      r6["enabled"] is True and r6["reason"] == "targeting", r6)

print("== 7. 整包带属性：形态、确定性、按属性分包 ==")
set_targeting("feat-a", {"plan": "pro"})
set_targeting("grp-x", {"plan": "pro"})
b = bundle("u1", PRO)
check("整包带属性返回版本", isinstance(b["version"], str) and len(b["version"]) == 16)
check("整包回显属性", b["attrs"] == PRO)
check("feat-a 命中为开", b["flags"]["feat-a"]["enabled"] is True
      and b["flags"]["feat-a"]["reason"] == "targeting")
b2 = bundle("u1", PRO)
check("同人同属性多次拿：版本不变", b2["version"] == b["version"])
check("同人同属性多次拿：结果不变", b2["flags"] == b["flags"])
b_free = bundle("u1", FREE)
check("同人不同属性：是另一个版本", b_free["version"] != b["version"])
check("不同属性的包里 feat-a 为关", b_free["flags"]["feat-a"]["enabled"] is False)
b_none = bundle("u1")
check("不带属性的包又一个版本", b_none["version"] not in (b["version"], b_free["version"]))
check("不带属性的包没有 attrs 字段", "attrs" not in b_none)
# key 顺序不同仍是同一个包（注意 true 是 JSON 布尔，和 PRO 里的 vip:true 同值）
raw = '{"level":3,"plan":"pro","vip":true}'
r = c.get(f"/api/bundle?identity=u1&attrs={urllib.parse.quote(raw)}").get_json()
check("key 顺序不同版本相同", r["version"] == b["version"])
# check 与 bundle 同一身属性结果一致
for fname in ("feat-a", "feat-b", "feat-c"):
    rr = check_flag(fname, "u1", PRO)[1]
    check(f"{fname}：check 与整包结果一致",
          rr["enabled"] == b["flags"][fname]["enabled"]
          and rr["reason"] == b["flags"][fname]["reason"])

print("== 8. 改条件：按新规则、整包新版本、旧包过期 ==")
v_pro = bundle("u1", PRO)["version"]
v_free = bundle("u1", FREE)["version"]
v_none = bundle("u1")["version"]
bundle("u2", PRO)  # u2 也拿过命中属性的整包，改条件后应一起失效
set_targeting("feat-a", {"plan": "enterprise"}, headers=H_BOB)
b_new = bundle("u1", PRO)
check("条件改了：命中属性变了 -> 按新规则为关",
      b_new["flags"]["feat-a"]["enabled"] is False
      and b_new["flags"]["feat-a"]["reason"] == "default")
check("条件改了：带属性的包换新版本", b_new["version"] != v_pro)
r = bundle("u1", PRO, version=v_pro)
check("拿着改前那包来问 -> valid=false（过期）", r["valid"] is False)
check("过期响应附当前新版本", r["version"] == b_new["version"])
check("拿着当前版本来问 -> valid=true",
      bundle("u1", PRO, version=b_new["version"])["valid"] is True)
check("条件改动不影响「对不上」那身属性的包",
      bundle("u1", FREE)["version"] == v_free)
check("条件改动不影响不带属性的老包（版本一字不变）",
      bundle("u1")["version"] == v_none)
check("拿不带属性包的旧版本来问仍有效",
      bundle("u1", version=v_none)["valid"] is True)
# 企业属性现在能开
check("新条件：enterprise 对上 -> 开",
      check_flag("feat-a", "u1", {"plan": "enterprise"})[1]["enabled"] is True)

print("== 9. 失效记录：谁改的、让哪些人哪身属性的包过期 ==")
inv = c.get("/api/bundles/invalidations?limit=200", headers=H).get_json()
last = [x for x in inv if "targeting=" in (x["change"] or "")]
check("改条件产生了失效记录", len(last) > 0)
check("失效记录的操作人是 bob", all(x["actor"] == "bob" for x in last))
attrs_hashes = {x["attrs_hash"] for x in last}
check("失效记录只带非空 attrs_hash（只波及带属性的包）",
      attrs_hashes and "" not in attrs_hashes, attrs_hashes)
identities = {x["identity"] for x in last}
check("所有拿过命中属性包的人都在记录里", {"u1", "u2"} <= identities, identities)
# FREE 那身属性不受这次条件改动影响（feat-a 本来就不命中）
check("FREE 那身属性没有因这次改条件失效",
      not any(x["identity"] == "u1"
              and x["attrs_hash"] == flag_app.attrs_hash_of(FREE) for x in last))
check("失效记录接口要鉴权",
      c.get("/api/bundles/invalidations").status_code == 401)
# 管理端记下的新版本 == 本人带同一身属性再来拿拿到的
u2_rec = next(x for x in last if x["identity"] == "u2"
              and x["attrs_hash"] == flag_app.attrs_hash_of(PRO))
u2_new = bundle("u2", PRO)["version"]
check("记下的新版本与本人来拿一致", u2_rec["new_version"] == u2_new)
check("记下的旧版本不是当前版本", u2_rec["old_version"] != u2_new)

print("== 10. 清除条件 / 改回：旧包不复活 ==")
set_targeting("feat-a", {})
check("空对象清除条件",
      next(f for f in c.get("/api/flags", headers=H).get_json()
           if f["name"] == "feat-a")["targeting"] == {})
check("清除后带属性也按原规则",
      check_flag("feat-a", "u1", {"plan": "enterprise"})[1]["reason"] == "default")
set_targeting("feat-a", {"plan": "enterprise"})  # 设回和第 8 节一样的条件
b_now = bundle("u1", {"plan": "enterprise"})
# 此时内容摘要与曾发出过的旧包相同，但 generation 已推进——旧版本不得复活
old_versions = {x["old_version"] for x in inv}
check("曾经发出的旧版本不再有效（序号只增不减）",
      bundle("u1", {"plan": "enterprise"},
             version=next(iter(old_versions)))["valid"] is False
      if old_versions else True)
check("当前版本有效",
      bundle("u1", {"plan": "enterprise"}, version=b_now["version"])["valid"] is True)
r = set_targeting("feat-a", None)
check("null 也表示清除条件", r.status_code == 200 and
      next(f for f in c.get("/api/flags", headers=H).get_json()
           if f["name"] == "feat-a")["targeting"] == {})

print("== 11. 改条件不影响别人的其他层；改别的层照样让属性包失效 ==")
set_targeting("feat-a", {"plan": "pro"})
v = bundle("u1", PRO)["version"]
c.patch("/api/flags/feat-a", headers=H, json={"description": "改个描述"})
check("改描述：属性包版本也不变", bundle("u1", PRO)["version"] == v)
c.patch("/api/flags/feat-a", headers=H, json={"kill_switch": True})
b = bundle("u1", PRO)
check("全关让属性包也换版本", b["version"] != v)
check("全关后属性包内 feat-a 为关", b["flags"]["feat-a"]["reason"] == "kill_switch")
c.patch("/api/flags/feat-a", headers=H, json={"kill_switch": False})

print("== 12. 定时生效：到点前照旧，到点按新条件、旧包过期 ==")
set_targeting("feat-a", {"plan": "pro"})
b0 = bundle("u7", {"plan": "beta"})
check("到点前 beta 属性不命中", b0["flags"]["feat-a"]["enabled"] is False)
r = set_targeting("feat-a", {"plan": "beta"}, effective_at=time.time() + 0.4)
check("约条件返回 202", r.status_code == 202 and r.get_json().get("scheduled") is True,
      r.status_code)
check("到点前 check 仍按旧条件",
      check_flag("feat-a", "u7", {"plan": "beta"})[1]["enabled"] is False)
check("到点前整包版本不换", bundle("u7", {"plan": "beta"})["version"] == b0["version"])
time.sleep(0.6)
code, rr = check_flag("feat-a", "u7", {"plan": "beta"})
check("到点后按新条件命中", rr["enabled"] is True and rr["reason"] == "targeting", rr)
b1 = bundle("u7", {"plan": "beta"})
check("到点后整包换新版本", b1["version"] != b0["version"])
check("到点前那包过期", bundle("u7", {"plan": "beta"}, version=b0["version"])["valid"] is False)

print("== 13. 老路不坏：不带属性 / 一次问一个 / 拿整包 ==")
r = c.get("/api/flags/feat-a/check")
check("check 缺 identity 仍 400", r.status_code == 400)
r = c.get("/api/bundle")
check("bundle 缺 identity 仍 400", r.status_code == 400)
r = c.get("/api/flags/feat-a/check?identity=u1&attrs=")
check("attrs 为空串视同没带", r.status_code == 200 and "attrs" not in r.get_json())
for bad in ("{bad json", "[]", '"pro"', "42",
            '{"plan": ["pro"]}', '{"plan": {"x":1}}', '{"": "pro"}'):
    r = c.get("/api/flags/feat-a/check?identity=u1&attrs="
              + urllib.parse.quote(bad))
    check(f"非法 attrs 400：{bad}", r.status_code == 400, r.status_code)
    r = c.get("/api/bundle?identity=u1&attrs=" + urllib.parse.quote(bad))
    check(f"整包非法 attrs 400：{bad}", r.status_code == 400)

print("== 14. 管理端条件校验 ==")
cur_rule = next(f for f in c.get("/api/flags", headers=H).get_json()
                if f["name"] == "feat-a")["targeting"]
for bad in ([], "pro", 42, {"": "pro"}, {"k": []}, {"k": {"x": 1}}, {"k": [1, []]}):
    r = set_targeting("feat-a", bad)
    check(f"非法条件 400：{json.dumps(bad, ensure_ascii=False)}",
          r.status_code == 400, r.status_code)
r = c.patch("/api/flags/feat-a", headers={"X-Actor": "x"},
            json={"targeting": {"plan": "pro"}})
check("定条件接口要鉴权", r.status_code == 401)
# 校验失败不能把条件改坏
check("校验失败后原条件还在",
      next(f for f in c.get("/api/flags", headers=H).get_json()
           if f["name"] == "feat-a")["targeting"] == cur_rule)
# 设成一样的条件：changed=0，不产生失效
before = len(c.get("/api/bundles/invalidations", headers=H).get_json())
r = set_targeting("feat-a", cur_rule)
check("条件没变化 changed=0", r.get_json().get("changed") == 0, r.get_json())
after = len(c.get("/api/bundles/invalidations", headers=H).get_json())
check("条件没变化不产生失效记录", before == after)
# 定时一个非法条件也要被拦
r = set_targeting("feat-a", {"k": []}, effective_at=time.time() + 60)
check("定时非法条件 400", r.status_code == 400)
check("非法预约没有落库",
      c.get("/api/scheduled-changes", headers=H).get_json() == [])

print("== 15. 建开关时直接带条件 ==")
code, r = check_flag("feat-c", "u1", {"plan": "pro"})
check("feat-c：带条件创建，命中即开",
      r["enabled"] is True and r["reason"] == "targeting", r)
code, r = check_flag("feat-c", "u1", {"plan": "free"})
check("feat-c：对不上按默认关", r["enabled"] is False and r["reason"] == "default")

print("== 16. 整包列表按 (身份, 属性) 分行 ==")
rows = c.get("/api/bundles", headers=H).get_json()
u1_rows = [r for r in rows if r["identity"] == "u1"]
u1_hashes = {r["attrs_hash"] for r in u1_rows}
check("u1 的无属性包与属性包分行", len(u1_rows) >= 3 and "" in u1_hashes, u1_hashes)
pro_row = next(r for r in u1_rows if r["attrs"] == PRO)
check("列表回显属性原文", pro_row["attrs"] == PRO)
check("列表接口要鉴权", c.get("/api/bundles").status_code == 401)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
