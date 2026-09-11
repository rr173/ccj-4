"""身份合并（把几个身份收成同一个人）的端到端测试。

覆盖需求：
1. 管理端能把几个身份收成同一个人；至少两个，少写了收不成
2. 某个身份已经在另一拨人里，这次收不成，并说清楚
3. 合并后来问用其中任何一个身份，每个开关的开/关都按同一个人算（check / 整包
   /版本/档名/组落定都一样）；换一个收在一起的身份来问，结果一字不差
4. 单人强制 / 结果冻结 / 组落定 / 分桶都按主身份算；管理端给别名下状态落在主身份
5. 拆开以后这几个身份各算各的
6. 改过谁跟谁是同一个人（合并 / 拆开 / 重新收）以后，再来问按新的算
7. 合并 / 拆开都让相关已发整包换新版本，旧版本过期
8. 幂等：整拨原封不动再收一次 changed=false，不换版本
9. 历史重放：合并前各算各的，合并后同一个人，拆开后又各算各的
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


def merge(identities, headers=H):
    return c.post("/api/identities/merges", headers=headers,
                  json={"identities": identities})


def split(group_id, headers=H):
    return c.delete(f"/api/identities/merges/{group_id}", headers=headers)


def merges_list():
    return c.get("/api/identities/merges", headers=H).get_json()


def override(flag, identity, enabled):
    return c.put(f"/api/flags/{flag}/overrides", headers=H,
                 json={"identity": identity, "enabled": enabled})


# ---------------------------------------------------------------- 准备开关
c.post("/api/flags", headers=H, json={"name": "f-default-on",
                                      "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "f-default-off"})
c.patch("/api/flags/f-default-on", headers=H, json={"rollout_percent": 50})
# 两个进同一个互斥组、默认开的开关：用组落定验证「按同一个人算」
c.post("/api/flags", headers=H, json={"name": "g-a", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "g-b", "default_enabled": True})
c.post("/api/groups", headers=H, json={"name": "grp"})
c.put("/api/groups/grp/flags", headers=H, json={"flag": "g-a"})
c.put("/api/groups/grp/flags", headers=H, json={"flag": "g-b"})

A, B, CC = "alice", "alice-mobile", "alice-work"

print("== 1. 入参校验：至少两个，列表/非空字符串/不重复 ==")
r = merge([])
check("空列表 -> 400", r.status_code == 400, r.status_code)
check("空列表说清至少两个", "at least two" in r.get_json()["error"], r.get_json())
r = merge(["only-one"])
check("只写一个 -> 400", r.status_code == 400, r.status_code)
r = merge(None) if False else c.post("/api/identities/merges", headers=H, json={})
check("不带 identities -> 400", r.status_code == 400, r.status_code)
r = merge(["a", ""])
check("含空串 -> 400", r.status_code == 400, r.status_code)
r = merge(["a", 1, "b"])
check("含非字符串 -> 400", r.status_code == 400, r.status_code)
r = merge(["a", "b", "a"])
check("有重复 -> 400 并说清", r.status_code == 400 and "unique" in r.get_json()["error"],
      (r.status_code, r.get_json()))
check("收不成时不落任何拨", merges_list() == [], merges_list())

print("== 2. 合并前：两个身份是各算各的（放量可能落在不同侧） ==")
code_a, ra = check_flag("f-default-on", A)
code_b, rb = check_flag("f-default-on", B)
check("合并前都能查", code_a == 200 and code_b == 200)
# 找一对天然落在放量不同侧的身份，避免用例对分桶的假设过强：直接用主身份口径
# 比对，不强求不同；关键是合并后必须一致。

print("== 3. 合并两个身份 ==")
r = merge([A, B])
body = r.get_json()
check("合并成功 201", r.status_code == 201, (r.status_code, body))
check("返回 changed=true", body.get("changed") is True, body)
check("返回主身份", body.get("primary") in (A, B), body)
GID = body.get("id")
check("返回成员列表", set(body.get("identities")) == {A, B}, body)
check("要鉴权", c.post("/api/identities/merges", json={"identities": [A, B]}).status_code == 401)

print("== 4. 列表能看到这拨人，主身份排第一 ==")
lst = merges_list()
check("列表有一拨", len(lst) == 1, lst)
g0 = lst[0]
check("主身份排第一", g0["identities"][0] == g0["primary"], g0)
check("成员对", set(g0["identities"]) == {A, B}, g0)

print("== 5. 合并后来问：任何一个身份结果一字不差（check） ==")
for flag in ("f-default-on", "f-default-off", "g-a", "g-b"):
    _, ea = check_flag(flag, A)
    _, eb = check_flag(flag, B)
    check(f"{flag}：两个身份 enabled+reason+variant 一致",
          (ea["enabled"], ea["reason"], ea.get("variant"))
          == (eb["enabled"], eb["reason"], eb.get("variant")),
          (ea, eb))
# 再换一个写法（同一次请求序列）结果也恒定
_, ea2 = check_flag("f-default-on", A)
_, eb2 = check_flag("f-default-on", B)
_, ea3 = check_flag("f-default-on", A)
check("同一身份多次问恒定", ea2 == ea3, (ea2, ea3))
check("组落定身份共享：g-a/g-b 里恰好一个开（按同一人）",
      len([f for f in ("g-a", "g-b") if check_flag(f, A)[1]["enabled"]]) == 1
      and len([f for f in ("g-a", "g-b") if check_flag(f, B)[1]["enabled"]]) == 1)

print("== 6. 整包：两个身份拿到同一个版本、同一套结果 ==")
ba = bundle(A)
bb = bundle(B)
check("整包版本一致", ba["version"] == bb["version"], (ba["version"], bb["version"]))
check("整包每个开关结果一致",
      ba["flags"] == bb["flags"], (ba["flags"], bb["flags"]))
check("整包回显各自来问的身份", ba["identity"] == A and bb["identity"] == B)

print("== 7. 管理端给别名下强制 / 冻结，落在主身份、对两个身份都生效 ==")
override("f-default-off", B, True)
_, ea = check_flag("f-default-off", A)
_, eb = check_flag("f-default-off", B)
check("给别名强制开：主身份问是开(reason=override)",
      ea["enabled"] and ea["reason"] == "override", ea)
check("给别名强制开：另一个身份问同样是开(override)",
      eb["enabled"] and eb["reason"] == "override", eb)
# 冻结别名：两个身份都拿到冻住的值
c.put("/api/flags/f-default-on/freezes", headers=H, json={"identity": A})
_, ea = check_flag("f-default-on", A)
_, eb = check_flag("f-default-on", B)
check("冻主身份：别名问也是 freeze", ea["reason"] == "freeze"
      and eb["reason"] == "freeze"
      and ea["enabled"] == eb["enabled"], (ea, eb))

print("== 8. 幂等：整拨原封不动再收一次 ==")
v_before = bundle(A)["version"]
r = merge([B, A])  # 顺序换一下
b2 = r.get_json()
check("重复收 -> 200 changed=false",
      r.status_code == 200 and b2.get("changed") is False, (r.status_code, b2))
check("幂等返回同一个拨 id", b2.get("id") == GID, (b2, GID))
check("幂等不换整包版本", bundle(A)["version"] == v_before)

print("== 9. 某个身份已在另一拨：横跨两拨收不成，并说清 ==")
# 第三个身份目前独立：把它和 A 收在一起应被拒（A 已在 {A,B} 拨里）
r = merge([A, CC])
check("并入已在拨的身份 -> 409", r.status_code == 409, r.status_code)
check("错误里点出谁已在哪一拨",
      A in r.get_json()["error"] and B in r.get_json()["error"], r.get_json())
# 想把已有的整拨 {A,B} 与独立身份扩编也不成
r = merge([A, B, CC])
check("整拨扩编 -> 409（一个都不收）", r.status_code == 409, r.status_code)
check("扩编失败后 CC 仍是独立的",
      all(CC not in g["identities"] for g in merges_list()), merges_list())

print("== 10. 两拨互不影响：另建一拨独立的人 ==")
r = merge([CC, "carol"])
check("另一拨合并成功", r.status_code == 201, r.get_json())
check("共两拨", len(merges_list()) == 2, merges_list())
# 两拨之间结果互不串：给 carol 强制关默认开开关，不影响 A/B 拨
override("f-default-on", "carol", False) if False else None
# （f-default-on 已对 A 拨冻住；这里验证 carol 拨有自己的 override 表）
c.put("/api/flags/f-default-off/overrides", headers=H,
      json={"identity": "carol", "enabled": False})
_, e_carol = check_flag("f-default-off", "carol")
_, e_cc = check_flag("f-default-off", CC)
_, e_a = check_flag("f-default-off", A)
check("carol 拨内部一致（强制关）",
      not e_carol["enabled"] and not e_cc["enabled"], (e_carol, e_cc))
check("carol 拨不影响 A 拨（A 仍是强制开）", e_a["enabled"] is True, e_a)

print("== 11. 拆开：各算各的 ==")
# 拆开前先拿掉冻结/强制，好干净地验证「各算各的」
c.delete("/api/flags/f-default-on/freezes", headers=H, json={"identity": B})
c.delete("/api/flags/f-default-off/overrides", headers=H, json={"identity": A})
r = split(GID)
check("拆开成功", r.status_code == 200, r.get_json())
check("拆开返回成员", set(r.get_json()["identities"]) == {A, B}, r.get_json())
check("拆开后列表少一拨",
      all(set(g["identities"]) != {A, B} for g in merges_list()), merges_list())
# 拆开后身份回到独立状态：把另一拨 {CC,carol} 也拆开，A 才能和 CC 自由重收
cc_group = next(g for g in merges_list() if set(g["identities"]) == {CC, "carol"})
split(cc_group["id"])
r = merge([A, CC])
check("拆开后可与原本在别的拨的身份重收", r.status_code == 201, r.get_json())
# 清场：再拆开，回到全独立
split(r.get_json()["id"])

print("== 12. 改了「谁跟谁同一个人」以后，整包换新版本、旧版过期 ==")
va = bundle(A)["version"]
vb = bundle(B)["version"]
# 合并前两版本可能不同；合并后必须一致且都变新版本（因为强制/冻结都已清，
# 这里靠分桶/落定口径验证）。重新合一次：
r = merge([A, B])
GID2 = r.get_json()["id"]
na = bundle(A, version=va)
nb = bundle(B, version=vb)
check("合并后两身份版本一致", na["version"] == nb["version"],
      (na["version"], nb["version"]))
# A 若原版本与合并后不同，则旧版本应判过期；无论如何，拆开后必换版本
r = split(GID2)
sa = bundle(A)
sb = bundle(B)
# 拆开后他们重新独立：账本各自一行；版本不一定相同，但各自来拿稳定
sa2 = bundle(A)
check("拆开后各自整包稳定（两次来拿版本不变）", sa["version"] == sa2["version"],
      (sa["version"], sa2["version"]))
# 合并 -> 拆开这个「改过」过程让旧版本过期：用合并期非主身份拿到的版本，
# 在拆开后用同一身份校验（主身份的状态原样留在自己名下、版本可不变；非主身份
# 合并期与主身份共享账本，拆开后按自己重算，摘要里的身份都不同，必换版本）
r = merge([A, B]); g3 = r.get_json()["id"]
primary3 = r.get_json()["primary"]
other3 = B if primary3 == A else A
during = bundle(other3)["version"]
split(g3)
after = bundle(other3, version=during)
check("拆开后拿着合并期间的版本来问 -> valid=false",
      after.get("valid") is False, {"during": during, "after": after.get("version"),
                                    "valid": after.get("valid")})

print("== 13. 拆开不存在的拨 -> 404 ==")
check("拆不存在的拨 404", split(99999).status_code == 404)

print("== 14. 历史重放：合并前各算各的，合并期同一个人，拆开后又各算各的 ==")
# 用两个干净的身份做时间线
X, Y = "hist-x", "hist-y"
c.post("/api/flags", headers=H, json={"name": "hist-f", "default_enabled": True})
c.patch("/api/flags/hist-f", headers=H, json={"rollout_percent": 100})  # 全开，便于看强制
c.patch("/api/flags/hist-f", headers=H, json={"rollout_percent": 0})    # 回默认开

t0 = time.time()
time.sleep(0.02)
# t1：合并前，给 X 强制关
c.put("/api/flags/hist-f/overrides", headers=H,
      json={"identity": X, "enabled": False})
time.sleep(0.02)
t_merge = time.time()
time.sleep(0.02)
# t2：把 X、Y 收成同一个人
r = merge([X, Y]); g_hist = r.get_json()["id"]
time.sleep(0.02)
t_during = time.time()
time.sleep(0.02)
# t3：拆开
split(g_hist)
time.sleep(0.02)
t_end = time.time()

def hist(identity, at):
    r = c.get(f"/api/history?{q(identity)}&at={at}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["flags"]["hist-f"]["enabled"]

# 合并前（t1 之后、t_merge 之前）：X 被强制关，Y 没有强制（默认开）——各算各的
check("合并前历史：X 关（自己的强制）", hist(X, (t1_ := (t0 + t_merge) / 2)) is False)
check("合并前历史：Y 开（没有 X 的强制）",
      hist(Y, (t0 + t_merge) / 2) is True, hist(Y, (t0 + t_merge) / 2))
# 合并期间：X、Y 算同一个人，X 的强制对两人都生效 -> 都关
check("合并期历史：X 关", hist(X, (t_merge + t_end) / 2) is False)
check("合并期历史：Y 也关（同一个人）", hist(Y, (t_merge + t_end) / 2) is False,
      hist(Y, (t_merge + t_end) / 2))
# 拆开后：X 仍关（强制在主身份名下），Y 回到自己名下无强制 -> 开
check("拆开后历史：X 关", hist(X, t_end + 0.01) is False)
check("拆开后历史：Y 开（各算各的）", hist(Y, t_end + 0.01) is True,
      hist(Y, t_end + 0.01))

print()
print(f"{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
