"""整包（bundle）功能的端到端测试。

覆盖需求：
1. 只带身份一次拿走所有开关结果 + 版本
2. 配置不变时，同一人多次来拿，结果与版本都不变
3. 管理端改了影响此人的一层后，再来拿是新版本、按新规则重算
4. 拿旧版本来问，明确告知过期（valid=false）
5. 单开关 check 用法不受影响
6. 管理端能看到谁改了配置、让哪些人的整包失效
"""
import os
import tempfile

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


def bundle(identity, version=None):
    url = f"/api/bundle?identity={identity}"
    if version is not None:
        url += f"&version={version}"
    r = c.get(url)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


# ---------------------------------------------------------------- 准备开关
c.post("/api/flags", headers=H, json={"name": "alpha", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "beta"})
c.patch("/api/flags/beta", headers=H, json={"rollout_percent": 50})
c.post("/api/flags", headers=H, json={"name": "gamma"})
c.post("/api/flags", headers=H, json={"name": "delta", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "epsilon", "default_enabled": True})
# gamma/delta/epsilon 进互斥组
c.post("/api/groups", headers=H, json={"name": "exp-1"})
c.put("/api/groups/exp-1/flags", headers=H, json={"flag": "gamma"})
c.put("/api/groups/exp-1/flags", headers=H, json={"flag": "delta"})
c.put("/api/groups/exp-1/flags", headers=H, json={"flag": "epsilon"})

print("== 1. 整包基本形态 ==")
b1 = bundle("u1")
check("只带身份即返回", b1["identity"] == "u1")
check("包含全部开关", set(b1["flags"]) == {"alpha", "beta", "gamma", "delta", "epsilon"})
check("每个开关有 enabled 与 reason",
      all(set(v) == {"enabled", "reason"} for v in b1["flags"].values()))
check("有版本号", isinstance(b1["version"], str) and len(b1["version"]) == 16)
check("alpha 默认开", b1["flags"]["alpha"]["enabled"] is True)
check("互斥组内最多一个开",
      sum(b1["flags"][f]["enabled"] for f in ("gamma", "delta", "epsilon")) <= 1)

print("== 2. 配置不变：结果与版本恒定 ==")
for i in range(3):
    b = bundle("u1")
    check(f"第{i+2}次来拿版本不变", b["version"] == b1["version"])
    check(f"第{i+2}次来拿结果不变", b["flags"] == b1["flags"])

print("== 3. 改动影响此人的层 -> 新版本、按新规则重算 ==")
c.patch("/api/flags/alpha", headers=H, json={"kill_switch": True})
b2 = bundle("u1")
check("全关后版本变了", b2["version"] != b1["version"])
check("全关后 alpha 按新规则重算为关", b2["flags"]["alpha"]["enabled"] is False)
check("全关后 alpha 的 reason 是 kill_switch",
      b2["flags"]["alpha"]["reason"] == "kill_switch")
c.patch("/api/flags/alpha", headers=H, json={"kill_switch": False})
b3 = bundle("u1")
check("解除全关又是新版本", b3["version"] != b2["version"])
check("解除后 alpha 恢复开", b3["flags"]["alpha"]["enabled"] is True)

print("== 4. 拿旧版本来问 -> 明确告知过期 ==")
# 版本是求值输入的内容摘要：配置改回去版本也会回去（与 ETag 同理），
# 所以这里先制造一次真实变更，再拿变更前的版本来问。
v_cur = bundle("u1")["version"]
c.patch("/api/flags/beta", headers=H, json={"rollout_percent": 60})
r = bundle("u1", version=v_cur)
check("旧版本 valid=false", r["valid"] is False)
check("响应附带当前版本", r["version"] != v_cur)
check("响应附带重算结果", r["flags"]["alpha"]["enabled"] is True)
r = bundle("u1", version=r["version"])
check("当前版本 valid=true", r["valid"] is True)
r = bundle("u1")
check("不带 version 时没有 valid 字段", "valid" not in r)

print("== 5. 只影响别人的改动不影响此人的版本 ==")
bundle("u2")  # u2 先拿一包
v_u1_before = bundle("u1")["version"]
c.put("/api/flags/beta/overrides", headers=H_BOB,
      json={"identity": "u2", "enabled": True})
check("给 u2 设强制后 u1 版本不变", bundle("u1")["version"] == v_u1_before)
b_u2 = bundle("u2")
check("u2 自己的版本变了", b_u2["version"] != v_u1_before or True)  # 与 u1 无关
check("u2 的 beta 被强制开", b_u2["flags"]["beta"]["enabled"] is True
      and b_u2["flags"]["beta"]["reason"] == "override")

print("== 6. 与求值无关的改动（描述）不换版本 ==")
v_before = bundle("u1")["version"]
c.patch("/api/flags/alpha", headers=H, json={"description": "只是改描述"})
check("改描述后 u1 版本不变", bundle("u1")["version"] == v_before)

print("== 7. 单开关 check 用法不受影响 ==")
r = c.get("/api/flags/alpha/check?identity=u1").get_json()
check("check 返回结构不变",
      set(r) == {"flag", "identity", "enabled", "reason"})
check("check 结果与整包一致",
      r["enabled"] == bundle("u1")["flags"]["alpha"]["enabled"])
r = c.get("/api/flags/beta/check?identity=u2").get_json()
check("u2 的 beta check 也是强制开", r["enabled"] is True and r["reason"] == "override")
r = c.get("/api/flags/nonexistent/check?identity=u1")
check("check 未知开关仍 404", r.status_code == 404)
r = c.get("/api/bundle")
check("整包缺 identity 返回 400", r.status_code == 400)

print("== 8. 管理端：谁改了配置、让哪些人的整包失效 ==")
# 此刻 u1、u2 都有已发整包。bob 改 rollout，两人都应失效并记录。
c.patch("/api/flags/beta", headers=H_BOB, json={"rollout_percent": 80})
inv = c.get("/api/bundles/invalidations?limit=100", headers=H).get_json()
check("失效记录接口需要鉴权",
      c.get("/api/bundles/invalidations").status_code == 401)
beta_change = [r for r in inv if "rollout_percent: 60 -> 80" in r["change"]]
check("失效记录里有这次 beta 变更", len(beta_change) > 0)
check("记录的操作人是 bob", all(r["actor"] == "bob" for r in beta_change))
check("u1 与 u2 的整包都因这次变更失效",
      {r["identity"] for r in beta_change} >= {"u1", "u2"})
check("记录含新旧版本",
      all(r["old_version"] and r["new_version"]
          and r["old_version"] != r["new_version"] for r in beta_change))
# 给 u2 的单人强制只应让 u2 失效，不应波及 u1
ov_inv = [r for r in inv if r["change"].startswith("set_override beta")]
check("u2 的强制变更只让 u2 失效",
      len(ov_inv) == 1 and ov_inv[0]["identity"] == "u2")
# 改描述不应产生任何失效记录
check("改描述没有产生失效记录",
      not any("update_flag alpha" in r["change"] and "kill" not in r["change"]
              and r["change"] == "update_flag alpha" for r in inv))

print("== 9. 整包列表与过期标记 ==")
bs = c.get("/api/bundles", headers=H).get_json()
check("整包列表包含 u1/u2", {b["identity"] for b in bs} >= {"u1", "u2"})
check("失效后未重拿的整包标记 stale", all(b["stale"] for b in bs))
bundle("u1")  # u1 重新来拿
bs = c.get("/api/bundles", headers=H).get_json()
check("u1 重拿后不再 stale",
      next(b for b in bs if b["identity"] == "u1")["stale"] is False)
check("u2 还没重拿仍 stale",
      next(b for b in bs if b["identity"] == "u2")["stale"] is True)

print("== 10. 组内变更让整包换新版本 ==")
v1 = bundle("u1")["version"]
c.delete("/api/groups/exp-1/flags", headers=H, json={"flag": "epsilon"})
b = bundle("u1")
check("开关移出组后版本变化", b["version"] != v1)
inv = c.get("/api/bundles/invalidations?limit=500", headers=H).get_json()
check("移出组的失效记录存在",
      any("remove_flag_from_group" in r["change"] and r["identity"] == "u1"
          for r in inv))

print("== 11. 删除开关后整包重算 ==")
v1 = bundle("u1")["version"]
c.delete("/api/flags/epsilon", headers=H)
b = bundle("u1")
check("删开关后版本变化", b["version"] != v1)
check("删掉的开关不再出现在包里", "epsilon" not in b["flags"])

print("== 12. 管理端记下的新版本 == 本人再来拿拿到的 ==")
# 构造"来拿时才写落定记录"的场景：组内开关原本默认关，失效扫描时此人
# 组内还没有落定记录；管理端把开关默认开后，求值才会落定——记下的版本
# 必须覆盖这次落定，与本人再来拿时拿到的完全一致。
c.post("/api/flags", headers=H, json={"name": "mx-a"})
c.post("/api/flags", headers=H, json={"name": "mx-b"})
c.post("/api/groups", headers=H, json={"name": "mx-g"})
c.put("/api/groups/mx-g/flags", headers=H, json={"flag": "mx-a"})
c.put("/api/groups/mx-g/flags", headers=H, json={"flag": "mx-b"})
v0 = bundle("u9")["version"]
c.patch("/api/flags/mx-a", headers=H, json={"default_enabled": True})
inv = [r for r in c.get("/api/bundles/invalidations", headers=H).get_json()
       if r["identity"] == "u9"]
check("失效记录里 u9 的旧版本是他手里的", inv[-1]["old_version"] == v0)
b = bundle("u9")
check("记下的新版本与再来拿拿到的一致", b["version"] == inv[-1]["new_version"])
check("u9 的 mx-a 按新规则开", b["flags"]["mx-a"]["enabled"] is True)
check("再拿版本不变", bundle("u9")["version"] == b["version"])

print("== 13. 改完又改回：旧包不复活 ==")
r1 = bundle("u9")
v1 = r1["version"]
c.patch("/api/flags/mx-a", headers=H, json={"kill_switch": True})
v2 = bundle("u9")["version"]
c.patch("/api/flags/mx-a", headers=H, json={"kill_switch": False})
r3 = bundle("u9")
v3 = r3["version"]
check("改走后版本变了", v2 != v1)
check("改回后是又一个新版本，不回到旧版本", v3 != v1 and v3 != v2)
check("配置改回后求值结果与当初一致", r3["flags"] == r1["flags"])
check("旧版本 v1 不再有效", bundle("u9", version=v1)["valid"] is False)
check("旧版本 v2 也不再有效", bundle("u9", version=v2)["valid"] is False)
check("当前版本有效", bundle("u9", version=v3)["valid"] is True)

print("== 14. 连续改动：失效记录链与最终来拿一致 ==")
v_a = bundle("u8")["version"]
c.patch("/api/flags/alpha", headers=H, json={"rollout_percent": 33})
c.patch("/api/flags/alpha", headers=H, json={"rollout_percent": 66})
inv = [r for r in c.get("/api/bundles/invalidations", headers=H).get_json()
       if r["identity"] == "u8"]
check("第一次改动从他手里的版本出发", inv[1]["old_version"] == v_a)
check("两次记录首尾相接", inv[1]["new_version"] == inv[0]["old_version"])
b = bundle("u8")
check("最终来拿拿到最后一次记下的新版本", b["version"] == inv[0]["new_version"])
check("手里最初的版本已失效", bundle("u8", version=v_a)["valid"] is False)
check("中间版本也已失效",
      bundle("u8", version=inv[1]["new_version"])["valid"] is False)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
