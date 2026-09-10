"""开关依赖（depends_on）的端到端测试。

覆盖需求（口语版）：
1. 管理端能说这个开关要先看另一个开不开：来问时被依赖的对这个人、
   同一身属性也是开，这个才可能开；被依赖的是关，这个必须关。
2. 不能互相绕着依赖：自依赖 / 成环在写入时直接拒绝。
3. 改了依赖再问按新的（立即生效）；整包换新版本，拿着改前那包来问算过期。
4. 原来问一个、拿整包、带属性问都不能坏。
另含：依赖成链、强制开/全关与依赖的先后、解除依赖、删除被依赖开关、
依赖关系进内容摘要（只影响相关包）、与互斥组、定时生效的配合。
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


def patch(flag, body, headers=H):
    return c.patch(f"/api/flags/{flag}", headers=headers, json=body)


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


def flag_row(name):
    rows = c.get("/api/flags", headers=H).get_json()
    return next(f for f in rows if f["name"] == name)


# ---------------------------------------------------------------- 准备开关
c.post("/api/flags", headers=H, json={"name": "base", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "mid", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "leaf", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "solo", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "gate", "default_enabled": True})

print("== 1. 基本形态：没配依赖时一切照旧 ==")
r = check_flag("leaf", "u1")
check("没配依赖的开关按自己的默认值开", r["enabled"] is True and r["reason"] == "default")
check("列表里 depends_on 为空串", flag_row("leaf")["depends_on"] == "")

print("== 2. 配依赖：被依赖的关，这个必须关 ==")
r = patch("leaf", {"depends_on": "base"})
check("配依赖返回成功", r.status_code == 200, r.get_json())
leaf_row = c.get("/api/flags", headers=H).get_json()
check("列表回显被依赖的开关名",
      next(f for f in leaf_row if f["name"] == "leaf")["depends_on"] == "base")
r = check_flag("leaf", "u1")
check("base 开时 leaf 仍可开", r["enabled"] is True and r["reason"] == "default")
# 把 base 对 u1 关掉：强制关
c.put("/api/flags/base/overrides", headers=H, json={"identity": "u1", "enabled": False})
r = check_flag("leaf", "u1")
check("base 对 u1 关时 leaf 必须关", r["enabled"] is False)
check("leaf 的 reason 是 depends_on", r["reason"] == "depends_on", r)
# base 对另一个人仍开 -> leaf 对另一个人也仍开
r = check_flag("leaf", "u2")
check("base 对 u2 开 -> leaf 对 u2 也开（按同一身份求值）",
      r["enabled"] is True and r["reason"] == "default")
c.delete("/api/flags/base/overrides", headers=H, json={"identity": "u1"})

print("== 3. 依赖成链：根关了，链上的都关 ==")
patch("mid", {"depends_on": "base"})
r = check_flag("mid", "u1")
check("链中段：base 开 mid 开", r["enabled"] is True)
patch("leaf", {"depends_on": "mid"})  # leaf -> mid -> base
r = check_flag("leaf", "u1")
check("链末端：上游都开则开", r["enabled"] is True and r["reason"] == "default")
patch("base", {"default_enabled": False})
r_mid = check_flag("mid", "u1")
r_leaf = check_flag("leaf", "u1")
check("根关：mid 关", r_mid["enabled"] is False and r_mid["reason"] == "depends_on")
check("根关：leaf 也关（沿链传播）",
      r_leaf["enabled"] is False and r_leaf["reason"] == "depends_on")
patch("base", {"default_enabled": True})
check("根恢复开：leaf 恢复可开", check_flag("leaf", "u1")["enabled"] is True)

print("== 4. 依赖用同一身属性判定 ==")
# base 只对 plan=pro 的人开（默认关 + 属性条件）
patch("base", {"default_enabled": False, "targeting": {"plan": "pro"}})
r = check_flag("leaf", "u3", {"plan": "free"})
check("free 用户：base 属性条件对不上 -> base 关 -> leaf 关",
      r["enabled"] is False and r["reason"] == "depends_on")
r_base = check_flag("base", "u3", {"plan": "pro"})
r_leaf = check_flag("leaf", "u3", {"plan": "pro"})
check("pro 用户：base 属性命中开",
      r_base["enabled"] is True and r_base["reason"] == "targeting")
check("pro 用户：leaf 也开（同一身属性）",
      r_leaf["enabled"] is True and r_leaf["reason"] == "default")
# 还原：base 默认开、无条件
patch("base", {"default_enabled": True, "targeting": {}})

print("== 5. 强制开救不回依赖关着的开关 ==")
patch("base", {"kill_switch": True})  # base 对所有人关
c.put("/api/flags/leaf/overrides", headers=H, json={"identity": "u1", "enabled": True})
r = check_flag("leaf", "u1")
check("base 全关时，对 leaf 强制开仍是关",
      r["enabled"] is False and r["reason"] == "depends_on", r)
# 但本开关自己的全关仍然优先（不看依赖也关，reason=kill_switch）
patch("leaf", {"kill_switch": True})
r = check_flag("leaf", "u1")
check("leaf 自己全关时 reason=kill_switch（全关在依赖之前）",
      r["enabled"] is False and r["reason"] == "kill_switch", r)
patch("leaf", {"kill_switch": False})
patch("base", {"kill_switch": False})
check("上游恢复后强制开生效", check_flag("leaf", "u1")["enabled"] is True)
c.delete("/api/flags/leaf/overrides", headers=H, json={"identity": "u1"})

print("== 6. 自依赖与成环拒绝 ==")
r = patch("base", {"depends_on": "base"})
check("自依赖 400", r.status_code == 400 and "itself" in r.get_json()["error"])
r = patch("base", {"depends_on": "leaf"})  # base -> leaf -> mid -> base 成环
check("成环 400", r.status_code == 400 and "circular" in r.get_json()["error"],
      r.get_json())
r = patch("leaf", {"depends_on": "ghost-flag"})
check("依赖不存在的开关 400", r.status_code == 400 and "not found" in r.get_json()["error"])
r = patch("leaf", {"depends_on": 123})
check("依赖名不是字符串 400", r.status_code == 400)
# 被拒后旧依赖还在、求值不变
check("拒绝后 leaf 仍依赖 mid", check_flag("leaf", "u1")["enabled"] is True)

print("== 7. 改依赖立即按新的算 ==")
r = check_flag("leaf", "u1")
check("改前 leaf->mid 开", r["enabled"] is True)
patch("leaf", {"depends_on": "solo"})
patch("solo", {"default_enabled": False})
r = check_flag("leaf", "u1")
check("改依赖到关着的 solo 后，再问立即关",
      r["enabled"] is False and r["reason"] == "depends_on")
patch("solo", {"default_enabled": True})
check("solo 恢复开，leaf 立即恢复", check_flag("leaf", "u1")["enabled"] is True)

print("== 8. 解除依赖 ==")
r = patch("leaf", {"depends_on": ""})
check("空串解除依赖 200", r.status_code == 200)
check("解除后 depends_on 回空串", flag_row("leaf")["depends_on"] == "")
patch("solo", {"default_enabled": False})
r = check_flag("leaf", "u1")
check("解除后 solo 关也不影响 leaf",
      r["enabled"] is True and r["reason"] == "default")
patch("leaf", {"depends_on": "mid"})  # 恢复 leaf->mid 供后续用

print("== 9. 整包：结果、版本、过期 ==")
patch("solo", {"default_enabled": True})
b0 = bundle("u1")
v0 = b0["version"]
check("整包里 leaf 按依赖算（上游开）", b0["flags"]["leaf"]["enabled"] is True)
# 改依赖关系 -> 新版本
r = patch("mid", {"depends_on": "solo"})  # mid->solo（solo 开着）
check("改依赖本身是一次变更", r.status_code == 200)
b1 = bundle("u1")
check("改了依赖整包换新版本", b1["version"] != v0, (b1["version"], v0))
check("上游仍开，结果暂时不变", b1["flags"]["mid"]["enabled"] is True)
r = bundle("u1", version=v0)
check("拿着改前那包来问算过期", r["valid"] is False)
check("过期响应附带当前版本", r["version"] == b1["version"])
r = bundle("u1", version=b1["version"])
check("拿着新版本来问有效", r["valid"] is True)
# 让被依赖的 solo 关 -> mid 与 leaf 都关
patch("solo", {"default_enabled": False})
b2 = bundle("u1")
check("solo 关：整包里 mid 关、leaf 关",
      b2["flags"]["mid"]["enabled"] is False
      and b2["flags"]["leaf"]["enabled"] is False)
check("两个关的 reason 都是 depends_on",
      b2["flags"]["mid"]["reason"] == "depends_on"
      and b2["flags"]["leaf"]["reason"] == "depends_on")
check("与依赖无关的 gate 不受影响", b2["flags"]["gate"]["enabled"] is True)

print("== 10. 配置不变，结果与版本恒定 ==")
for i in range(3):
    b = bundle("u1")
    check(f"第{i+2}次拿版本不变", b["version"] == b2["version"])
    check(f"第{i+2}次拿结果不变", b["flags"] == b2["flags"])

print("== 11. 带属性的包：依赖按同一身属性算，且分包记账 ==")
patch("base", {"default_enabled": False, "targeting": {"plan": "pro"}})
# 重建一条简单链：leaf -> base（base 仅 pro 开）
patch("mid", {"depends_on": None})
patch("leaf", {"depends_on": "base"})
bp = bundle("u9", {"plan": "pro"})
bf = bundle("u9", {"plan": "free"})
check("属性包 pro：base 命中开 -> leaf 开",
      bp["flags"]["base"]["enabled"] is True
      and bp["flags"]["leaf"]["enabled"] is True)
check("属性包 free：base 对不上关 -> leaf 关（depends_on）",
      bf["flags"]["base"]["enabled"] is False
      and bf["flags"]["leaf"]["enabled"] is False
      and bf["flags"]["leaf"]["reason"] == "depends_on")
# 改一个「free 用户对不上」的属性条件：free 包不变，pro 包换版本
v_free = bf["version"]
v_pro = bp["version"]
patch("base", {"targeting": {"plan": "pro", "tier": [1, 2]}})
check("free 用户对不上新条件：free 包版本不变",
      bundle("u9", {"plan": "free"})["version"] == v_free)
check("pro 用户带 tier 时仍对上：leaf 开",
      bundle("u9", {"plan": "pro", "tier": 1})["flags"]["leaf"]["enabled"] is True)
check("pro 用户没带 tier 对不上：leaf 关",
      bundle("u9", {"plan": "pro"})["flags"]["leaf"]["enabled"] is False)
# 还原
patch("base", {"default_enabled": True, "targeting": {}})
check("改依赖图（解除）让 pro 包也换版本",
      bundle("u9", {"plan": "pro"})["version"] != v_pro)
patch("leaf", {"depends_on": None})

print("== 12. 删除被依赖开关：依赖自动解除，不报错 ==")
c.post("/api/flags", headers=H, json={"name": "temp-base", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "temp-child", "default_enabled": True})
patch("temp-child", {"depends_on": "temp-base"})
rows = c.get("/api/flags", headers=H).get_json()
check("删除前依赖存在",
      next(f for f in rows if f["name"] == "temp-child")["depends_on"] == "temp-base")
r = c.delete("/api/flags/temp-base", headers=H)
check("删除被依赖开关成功", r.status_code == 200)
rows = c.get("/api/flags", headers=H).get_json()
check("删除后依赖自动解除",
      next(f for f in rows if f["name"] == "temp-child")["depends_on"] == "")
r = check_flag("temp-child", "u1")
check("解除后单查正常、按自己默认值开",
      r["enabled"] is True and r["reason"] == "default")
r = bundle("u1")
check("整包不再包含已删开关，且求值正常",
      "temp-base" not in r["flags"] and r["flags"]["temp-child"]["enabled"] is True)
c.delete("/api/flags/temp-child", headers=H)

print("== 13. 失效记录：改依赖让相关整包失效 ==")
c.post("/api/flags", headers=H, json={"name": "dep-a", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "dep-b", "default_enabled": True})
bundle("u7")
r = patch("dep-b", {"depends_on": "dep-a"}, headers=H_BOB)
check("配依赖 200", r.status_code == 200)
inv = c.get("/api/bundles/invalidations?limit=500", headers=H).get_json()
mine = [x for x in inv if x["identity"] == "u7"
        and x["change"].startswith("update_flag dep-b")]
check("改依赖产生了 u7 的失效记录", len(mine) >= 1, mine[-1] if mine else "")
check("失效记录操作人是 bob", mine[-1]["actor"] == "bob")
check("新旧版本都在且不同",
      mine[-1]["old_version"] and mine[-1]["new_version"]
      and mine[-1]["old_version"] != mine[-1]["new_version"])
b = bundle("u7")
check("记下的新版本与本人再来拿一致", b["version"] == mine[-1]["new_version"])

print("== 14. 依赖与互斥组 ==")
c.post("/api/flags", headers=H, json={"name": "g-a"})
c.post("/api/flags", headers=H, json={"name": "g-b"})
c.post("/api/groups", headers=H, json={"name": "gx"})
c.put("/api/groups/gx/flags", headers=H, json={"flag": "g-a"})
c.put("/api/groups/gx/flags", headers=H, json={"flag": "g-b"})
patch("g-a", {"default_enabled": True})
patch("g-b", {"default_enabled": True})
patch("g-b", {"depends_on": "g-a"})
# g-b 依赖 g-a：组内 g-a 先落定为开 -> g-b 自然为开但组裁决关；
# 即便先问 g-b，也会先沿依赖把 g-a 算开（先落定），g-b 仍关。
r_b = check_flag("g-b", "u5")
check("依赖先算：g-b 被依赖的 g-a 在组内占位，g-b 组裁决为关",
      r_b["enabled"] is False and r_b["reason"] == "group", r_b)
r_a = check_flag("g-a", "u5")
check("g-a 开", r_a["enabled"] is True and r_a["reason"] == "group")

print("== 15. 定时生效的依赖变更：到点前不变，到点后按新的 ==")
c.post("/api/flags", headers=H, json={"name": "sc-base"})
c.post("/api/flags", headers=H, json={"name": "sc-child", "default_enabled": True})
v_before = bundle("u1")["version"]
before = check_flag("sc-child", "u1")
future = time.time() + 0.4
r = patch("sc-child", {"depends_on": "sc-base", "effective_at": future})
check("预约依赖变更 202", r.status_code == 202, r.get_json())
check("到点前单查结果不变",
      check_flag("sc-child", "u1")["enabled"] == before["enabled"])
check("到点前整包版本不变", bundle("u1")["version"] == v_before)
time.sleep(0.6)
r = check_flag("sc-child", "u1")
check("到点后依赖生效：sc-base 默认关 -> sc-child 关",
      r["enabled"] is False and r["reason"] == "depends_on", r)
r = bundle("u1", version=v_before)
check("到点后旧版本过期", r["valid"] is False)
# 定时成环同样被预约接口拒绝
r = patch("sc-base", {"depends_on": "sc-child", "effective_at": time.time() + 60})
check("定时成环同样 400", r.status_code == 400)
check("定时自依赖也 400",
      patch("sc-base", {"depends_on": "sc-base",
                        "effective_at": time.time() + 60}).status_code == 400)

print("== 16. 原有用法不坏 ==")
# 单查与整包沿同一条依赖链，结果必须一致
chk = {f: check_flag(f, "u1") for f in ("base", "mid", "leaf")}
bnd = bundle("u1")["flags"]
check("单查与整包在依赖链上结果一致",
      all(chk[f]["enabled"] == bnd[f]["enabled"]
          and chk[f]["reason"] == bnd[f]["reason"] for f in chk),
      {f: (chk[f], bnd[f]) for f in chk})
r = c.get("/api/flags/gate/check?identity=u1")
check("单查返回结构不变",
      set(r.get_json()) == {"flag", "identity", "enabled", "reason"})
r = c.get("/api/flags/nope/check?identity=u1")
check("未知开关仍 404", r.status_code == 404)
r = c.get("/api/bundle")
check("整包缺 identity 仍 400", r.status_code == 400)
r = c.get("/api/flags/gate/check?identity=u1&attrs=%7B%22plan%22%3A%22pro%22%7D")
check("带属性单查正常", r.status_code == 200 and r.get_json()["attrs"] == {"plan": "pro"})
r = c.get("/api/flags")
check("管理端列表需要鉴权", r.status_code == 401)
rows = c.get("/api/flags", headers=H).get_json()
check("每个开关都带 depends_on 字段",
      all("depends_on" in f for f in rows))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
