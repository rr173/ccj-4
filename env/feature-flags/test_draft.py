"""发布稿（draft：先收一稿、发布时一起生效）的端到端测试。

覆盖需求（口语版）：
1. 管理端能把好几处改动先收成一稿。
2. 没发布前来问（单查、带属性问、拿整包）还按现在的，整包版本一字不变。
3. 一发布，稿里的都生效，整包换新版本，拿着发布前那包来问算过期。
4. 发布时要是会互相绕着依赖（合并成环），这一稿全都不能生效（原子拒绝）。
5. 没发布的稿能丢掉（整稿丢弃 / 摘掉某个开关），不影响任何结果。
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


def new_draft(note="", headers=H):
    r = c.post("/api/drafts", headers=headers, json={"note": note})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["draft_id"]


def stage(did, flag, body, headers=H):
    return c.put(f"/api/drafts/{did}/flags/{flag}", headers=headers, json=body)


def publish(did, headers=H):
    return c.post(f"/api/drafts/{did}/publish", headers=headers)


def draft_get(did):
    r = c.get(f"/api/drafts/{did}", headers=H)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


# ---------------------------------------------------------------- 准备开关
c.post("/api/flags", headers=H, json={"name": "pay", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "promo", "default_enabled": False})
c.post("/api/flags", headers=H, json={"name": "beta", "default_enabled": False})
c.post("/api/flags", headers=H, json={"name": "g-a", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "g-b", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "dep-x", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "dep-y", "default_enabled": True})

print("== 1. 开稿、往里收改动；同字段合并、后收覆盖先收 ==")
did = new_draft("秋季放量一稿")
r = c.get(f"/api/drafts/{did}", headers=H)
check("新稿是 open 且为空", r.status_code == 200
      and r.get_json()["status"] == "open" and r.get_json()["flags"] == {})
r = stage(did, "promo", {"default_enabled": True, "rollout_percent": 30})
check("收第一条 200", r.status_code == 200, r.get_json())
body = r.get_json()
check("回显合并后的改动", body["changes"]["default_enabled"] is True
      and body["changes"]["rollout_percent"] == 30)
r = stage(did, "promo", {"rollout_percent": 60})
check("再收同开关：200", r.status_code == 200)
check("同字段后收覆盖先收、其余保留",
      r.get_json()["changes"]["rollout_percent"] == 60
      and r.get_json()["changes"]["default_enabled"] is True)
r = stage(did, "beta", {"kill_switch": True, "targeting": {"plan": "pro"}})
check("第二个开关也能收进来", r.status_code == 200
      and set(r.get_json()["changes"]) == {"kill_switch", "targeting"})
d = draft_get(did)
check("稿里有两个开关的改动", set(d["flags"]) == {"promo", "beta"}, d)
check("targeting 回显为对象", d["flags"]["beta"]["targeting"] == {"plan": "pro"})

print("== 2. 没发布：单查 / 带属性问 / 整包全按现在的 ==")
check("promo 还是默认关（稿未发布）",
      check_flag("promo", "u1")["enabled"] is False)
check("beta 没全关、按默认关",
      check_flag("beta", "u1")["reason"] == "default"
      and check_flag("beta", "u1")["enabled"] is False)
b0 = bundle("u1")
v0 = b0["version"]
check("整包里 promo 仍关", b0["flags"]["promo"]["enabled"] is False)
check("整包里 beta 仍关（无全关）", b0["flags"]["beta"]["enabled"] is False)
bp_before = bundle("u9", {"plan": "pro"})
check("带属性的包也按现在（beta 属性条件没生效）",
      bp_before["flags"]["beta"]["enabled"] is False
      and bp_before["flags"]["beta"]["reason"] == "default")
for i in range(2):
    check(f"稿存在期间第{i+2}次拿整包版本不变", bundle("u1")["version"] == v0)
inv_count_before = len(c.get("/api/bundles/invalidations?limit=500",
                             headers=H).get_json())
check("稿不产生任何失效记录",
      len(c.get("/api/bundles/invalidations?limit=500", headers=H).get_json())
      == inv_count_before)

print("== 3. 发布：稿里的一起生效，整包换新版本，旧包过期 ==")
r = publish(did, headers=H_BOB)
check("发布 200", r.status_code == 200, r.get_json())
pb = r.get_json()
check("发布响应列出稿里的开关", set(pb["flags"]) == {"beta", "promo"})
check("发布后稿状态为 published", draft_get(did)["status"] == "published")
check("promo 发布后按新配置：默认开",
      check_flag("promo", "u1")["enabled"] is True)
# promo 默认开 + 放量 60%：结果按新规则，reason 是 rollout
r_promo = check_flag("promo", "u1")
check("promo 走放量层（默认值不再兜底）", r_promo["reason"] == "rollout")
check("beta 全关生效",
      check_flag("beta", "u1")["enabled"] is False
      and check_flag("beta", "u1")["reason"] == "kill_switch")
r_beta = check_flag("beta", "u9", {"plan": "pro"})
check("全关压过属性条件：beta 仍 kill_switch",
      r_beta["enabled"] is False and r_beta["reason"] == "kill_switch")
b1 = bundle("u1")
check("发布后整包换新版本", b1["version"] != v0, (b1["version"], v0))
check("发布后整包 promo 开", b1["flags"]["promo"]["enabled"] is True)
check("发布后整包 beta 关（全关）", b1["flags"]["beta"]["enabled"] is False)
r = bundle("u1", version=v0)
check("拿着发布前那包来问算过期", r["valid"] is False)
check("过期响应附带当前版本", r["version"] == b1["version"])
check("拿着新版本来问有效", bundle("u1", version=b1["version"])["valid"] is True)
# 失效记录：一次发布记一批，操作人是发布的人
inv = c.get("/api/bundles/invalidations?limit=500", headers=H).get_json()
pub_inv = [x for x in inv if x["change"].startswith(f"publish_draft #{did}")]
check("发布产生了失效记录", len(pub_inv) >= 1)
check("失效记录操作人是发布者 bob", all(x["actor"] == "bob" for x in pub_inv))
check("失效记录里有 u1 且新旧版本不同",
      any(x["identity"] == "u1" and x["old_version"] == v0
          and x["new_version"] == b1["version"] for x in pub_inv))
b = bundle("u1")
check("记下的新版本与本人再来拿一致", b["version"] == b1["version"])
audit_rows = c.get("/api/audit?limit=500", headers=H).get_json()
check("审计里有发布动作",
      any(x["action"] == "publish_draft" and x["actor"] == "bob"
          for x in audit_rows))
check("稿内每处变更都入审计并标注发布稿",
      any(x["action"] == "set_default" and f"#{did}" in x["detail"]
          for x in audit_rows)
      and any(x["action"] == "enable_kill_switch" and f"#{did}" in x["detail"]
              for x in audit_rows))

print("== 4. 已发布的稿不能再改 / 再发布 / 再丢弃 ==")
check("已发布稿不能再收改动", stage(did, "promo", {"rollout_percent": 10}).status_code == 409)
check("已发布稿不能再发布", publish(did).status_code == 409)
check("已发布稿不能丢弃", c.delete(f"/api/drafts/{did}", headers=H).status_code == 409)

print("== 5. 发布时合并成环：整稿全都不生效（原子） ==")
# 现网 dep-x、dep-y 互不依赖。稿里同时收 x->y 与 y->x：
# 单条看各自合法（能收进来），发布时合并成环，整稿拒绝。
did2 = new_draft("会成环的一稿")
r = stage(did2, "dep-x", {"depends_on": "dep-y"})
check("收 x 依赖 y：单条合法 200", r.status_code == 200, r.get_json())
r = stage(did2, "dep-y", {"depends_on": "dep-x"})
check("收 y 依赖 x：单条也合法 200", r.status_code == 200, r.get_json())
v_before = bundle("u1")["version"]
r = publish(did2)
check("成环发布被拒 400", r.status_code == 400
      and "circular" in r.get_json()["error"], r.get_json())
check("被拒后稿仍是 open", draft_get(did2)["status"] == "open")
check("被拒后 dep-x 没被改（无依赖）",
      check_flag("dep-x", "u1")["reason"] == "default")
check("被拒后 dep-y 没被改（无依赖）",
      check_flag("dep-y", "u1")["reason"] == "default")
check("被拒后整包版本一字不变", bundle("u1")["version"] == v_before)
check("拒绝入审计",
      any(x["action"] == "publish_rejected" for x in
          c.get("/api/audit?limit=500", headers=H).get_json()))

print("== 6. 自依赖 / 指向不存在的开关：发布同样整稿拒绝 ==")
did2b = new_draft("自依赖")
stage(did2b, "dep-x", {"depends_on": "dep-x"})
r = publish(did2b)
check("自依赖发布 400", r.status_code == 400 and "circular" in r.get_json()["error"])
check("自依赖拒绝也一字不改", bundle("u1")["version"] == v_before)
# 稿里收一个依赖，目标开关在发布前被删掉
did2c = new_draft("目标会被删")
c.post("/api/flags", headers=H, json={"name": "ghost-soon", "default_enabled": True})
stage(did2c, "promo", {"depends_on": "ghost-soon"})
c.delete("/api/flags/ghost-soon", headers=H)
r = publish(did2c)
check("依赖目标没了：发布 400 且整稿拒绝",
      r.status_code == 400 and "not found" in r.get_json()["error"],
      r.get_json())
check("拒绝后 promo 配置不变（仍发布态的默认开+放量）",
      check_flag("promo", "u1")["reason"] == "rollout")

print("== 7. 成环稿改好后能发布：边整体转向，最终无环 ==")
# dep-y 摘掉错误的那条，换成不冲突的改动，稿变成 x->y 一条边：应发布成功
r = c.delete(f"/api/drafts/{did2}/flags/dep-y", headers=H)
check("从稿里摘掉 dep-y 200", r.status_code == 200)
check("摘掉后稿里只剩 dep-x", set(draft_get(did2)["flags"]) == {"dep-x"})
v_before2 = bundle("u1")["version"]
r = publish(did2)
check("无环后发布成功", r.status_code == 200, r.get_json())
check("dep-x 现在依赖 dep-y",
      any(x["name"] == "dep-x" and x["depends_on"] == "dep-y"
          for x in c.get("/api/flags", headers=H).get_json()))
check("发布后整包换新版本", bundle("u1")["version"] != v_before2)

print("== 8. 没发布的稿能整条丢弃：不影响任何结果 ==")
did3 = new_draft("要丢掉的稿")
v_live = bundle("u2")["version"]
stage(did3, "pay", {"kill_switch": True})
stage(did3, "g-a", {"default_enabled": False})
check("丢弃前来问还是现在的（pay 开、g-a 开）",
      check_flag("pay", "u2")["enabled"] is True
      and check_flag("g-a", "u2")["enabled"] is True)
r = c.delete(f"/api/drafts/{did3}", headers=H)
check("丢弃 200", r.status_code == 200)
check("丢弃后状态 discarded", draft_get(did3)["status"] == "discarded")
check("丢弃后 pay 仍开", check_flag("pay", "u2")["enabled"] is True)
check("丢弃后 g-a 仍开", check_flag("g-a", "u2")["enabled"] is True)
check("丢弃不换整包版本", bundle("u2")["version"] == v_live)
check("丢弃的稿不能发布", publish(did3).status_code == 409)
audit_rows = c.get("/api/audit?limit=500", headers=H).get_json()
check("丢弃入审计", any(x["action"] == "discard_draft" for x in audit_rows))

print("== 9. 摘条目：404 与稿状态 ==")
did4 = new_draft("摘条目")
check("摘一个没收过的开关 404",
      c.delete(f"/api/drafts/{did4}/flags/pay", headers=H).status_code == 404)
check("不存在的稿 GET 404", c.get("/api/drafts/99999", headers=H).status_code == 404)
check("不存在的稿发布 404", publish(99999).status_code == 404)
check("空稿发布 400", publish(did4).status_code == 400)

print("== 10. 收入时的校验：字段级非法 / 不支持定时 ==")
check("放量越界 400", stage(did4, "pay", {"rollout_percent": 120}).status_code == 400)
check("放量不是整数 400", stage(did4, "pay", {"rollout_percent": "50"}).status_code == 400)
check("targeting 不是对象 400", stage(did4, "pay", {"targeting": [1, 2]}).status_code == 400)
check("depends_on 不是字符串 400", stage(did4, "pay", {"depends_on": 123}).status_code == 400)
check("depends_on 指向不存在的开关 400",
      stage(did4, "pay", {"depends_on": "nope"}).status_code == 400)
check("稿不支持 effective_at",
      stage(did4, "pay", {"kill_switch": True, "effective_at": time.time() + 60})
      .status_code == 400)
check("没有可收字段 400", stage(did4, "pay", {"note": "x"}).status_code == 400)
check("往不存在的开关收改动 404",
      stage(did4, "nope-flag", {"kill_switch": True}).status_code == 404)
check("非法输入没有污染稿", draft_get(did4)["flags"] == {})
# 收一条合法的，证明被拒后稿仍可用
check("拒绝后仍可正常收改动",
      stage(did4, "pay", {"description": "改个描述"}).status_code == 200)

print("== 11. 多稿并存：发布一稿不影响另一稿 ==")
da = new_draft("稿A")
db_ = new_draft("稿B")
stage(da, "pay", {"description": "稿A改描述"})
stage(db_, "g-a", {"description": "稿B改描述"})
r = publish(da)
check("发布稿A成功", r.status_code == 200)
check("稿B仍是 open", draft_get(db_)["status"] == "open")
check("稿A published", draft_get(da)["status"] == "published")
# 稿B仍可发布
stage(db_, "g-b", {"description": "稿B再收一条"})
check("稿B继续收改动正常", publish(db_).status_code == 200)

print("== 12. 列表与鉴权 ==")
r = c.get("/api/drafts", headers=H)
check("默认列表只列 open",
      r.status_code == 200
      and all(d["status"] == "open" for d in r.get_json()))
r = c.get("/api/drafts?all=1", headers=H)
statuses = {d["status"] for d in r.get_json()}
check("all=1 能看到 published/discarded",
      {"published", "discarded"} <= statuses, statuses)
check("稿列表需要鉴权", c.get("/api/drafts").status_code == 401)
check("发布需要鉴权", c.post(f"/api/drafts/{da}/publish").status_code == 401)
check("开稿需要鉴权", c.post("/api/drafts", json={}).status_code == 401)

print("== 13. 清除语义也能收进稿（解除依赖、清条件） ==")
# dep-x 当前依赖 dep-y；收一条解除依赖
did5 = new_draft("解除")
r = stage(did5, "dep-x", {"depends_on": ""})
check("收解除依赖 200", r.status_code == 200 and r.get_json()["changes"]["depends_on"] == "")
r = publish(did5)
check("发布成功", r.status_code == 200)
check("dep-x 依赖已解除",
      next(f for f in c.get("/api/flags", headers=H).get_json()
           if f["name"] == "dep-x")["depends_on"] == "")
# 清除 targeting
did6 = new_draft("清条件")
stage(did6, "beta", {"targeting": {}})
check("清条件收入稿", draft_get(did6)["flags"]["beta"]["targeting"] == {})

print("== 14. 原来问一个、拿整包、带属性问都不能坏 ==")
r = c.get("/api/flags/pay/check?identity=u1")
check("单查返回结构不变",
      r.status_code == 200 and set(r.get_json()) == {"flag", "identity", "enabled", "reason"})
r = c.get("/api/flags/nope/check?identity=u1")
check("未知开关仍 404", r.status_code == 404)
r = c.get("/api/bundle")
check("整包缺 identity 仍 400", r.status_code == 400)
r = c.get("/api/flags/pay/check?identity=u1&attrs=%7B%22plan%22%3A%22pro%22%7D")
check("带属性单查正常且回显 attrs",
      r.status_code == 200 and r.get_json()["attrs"] == {"plan": "pro"})
# 单查与整包结果一致
b = bundle("u1")["flags"]
for f in ("pay", "promo", "g-a", "g-b", "dep-x", "dep-y", "beta"):
    chk = check_flag(f, "u1")
    check(f"{f} 单查与整包一致",
          chk["enabled"] == b[f]["enabled"] and chk["reason"] == b[f]["reason"],
          (chk, b[f]))
# 立即生效的老路径不受影响
r = patch("pay", {"kill_switch": True})
check("立即 PATCH 仍然立即生效", r.status_code == 200
      and check_flag("pay", "u1")["reason"] == "kill_switch")
patch("pay", {"kill_switch": False})
# 定时生效老路径不受影响
r = patch("g-a", {"kill_switch": True, "effective_at": time.time() + 60})
check("定时 PATCH 仍返回 202", r.status_code == 202, r.get_json())
check("到点前一切照旧", check_flag("g-a", "u1")["enabled"] is True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
