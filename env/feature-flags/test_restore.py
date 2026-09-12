"""整份规矩还原（POST /api/restore）的端到端测试。

覆盖：
1. 少写了环境 / 时刻、环境没有、at 非法或在未来 -> 换不成（400/404）并说清楚，
   一个字段都不动
2. 换完后来问（单查 / 整包 / 历史）一律按换过去的那份规矩算
3. 当时还没有（或当时已删）的开关，换完后整个消失，不再按现在的规矩混在结果里
4. 约了还没到点的改动不算进要换的那份，且随这次还原全部取消；没发布的稿也不算
   （稿原样保留，发布与否是以后的事）；到了点（含未触发）的预约、已发布的稿
   都算进那份
5. 还原后已发整包统一换新版本（旧包 valid=false，失效记录可查）
6. 单人强制 / 冻结 / 互斥组与落定 / 对照名单 / 身份合并是环境自己的状态，
   这次不抄也不清
7. 还原是原子的，审计与历史流水有迹可循；再问「还原目标时刻之后的历史」口径连续
"""
import os
import tempfile
import time

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


def j(r):
    return r.get_json()


def post(path, body, headers=H):
    return c.post(path, headers=headers, json=body)


def patch(name, body, headers=H):
    return c.patch(f"/api/flags/{name}", headers=headers, json=body)


def create_flag(name, body=None, headers=H):
    payload = {"name": name}
    payload.update(body or {})
    return c.post("/api/flags", headers=headers, json=payload)


def delete_flag(name, headers=H):
    return c.delete(f"/api/flags/{name}", headers=headers)


def restore(at, headers=H, **extra):
    body = {"at": at}
    body.update(extra)
    return post("/api/restore", body, headers=headers)


def flags_by_name():
    return {f["name"]: f for f in c.get("/api/flags", headers=H).get_json()}


def check_flag(name, identity, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/api/flags/{name}/check?identity={identity}"
    if qs:
        url += "&" + qs
    r = c.get(url)
    assert r.status_code == 200, j(r)
    return j(r)


def bundle(identity, version=None, **params):
    qs = [f"identity={identity}"]
    for k, v in params.items():
        qs.append(f"{k}={v}")
    if version is not None:
        qs.append(f"version={version}")
    r = c.get("/api/bundle?" + "&".join(qs))
    assert r.status_code == 200, j(r)
    return j(r)


def history(identity, at, **params):
    qs = [f"identity={identity}", f"at={at}"]
    for k, v in params.items():
        qs.append(f"{k}={v}")
    r = c.get("/api/history?" + "&".join(qs))
    assert r.status_code == 200, j(r)
    return j(r)


def scheduled():
    return c.get("/api/scheduled-changes", headers=H).get_json()


# 求值相关字段（还原后要与那一刻一致的整套规矩；描述不参与求值）
RULE_FIELDS = ("default_enabled", "rollout_percent", "rollout_condition",
               "rollout_rules", "variants", "kill_switch", "targeting",
               "config", "depends_on")


def rules_of(name):
    f = flags_by_name()[name]
    return {k: f[k] for k in RULE_FIELDS}


# ============================================================ 1. 参数校验
print("== 1. 少写了 / 写岔了 -> 换不成并说清楚，一个字段都不动 ==")
create_flag("keep", {"default_enabled": True})
snapshot_before = c.get("/api/flags", headers=H).get_json()
bundle_before = bundle("u1")

r = c.post("/api/restore", headers={**H, "X-Environment": "test"}, json={})
check("少写 at 400", r.status_code == 400 and "at is required" in j(r)["error"], j(r))
r = restore("not-a-number")
check("at 不是数字 400", r.status_code == 400 and "timestamp" in j(r)["error"], j(r))
r = restore(time.time() + 60)
check("at 在未来 400", r.status_code == 400 and "past" in j(r)["error"], j(r))
r = restore(True)
check("at 写成布尔 400", r.status_code == 400, j(r))
r = c.post("/api/restore", json={"at": time.time()})
check("要管理令牌", r.status_code == 401, j(r))
# c 的 client_class 会兜底注入 X-Environment；用未定制的 FlaskClient 验证「漏带环境」
_raw_client = flag_app.FlaskClient(flag_app.app)
r = _raw_client.post("/api/restore", headers={"X-Admin-Token": "test-token"},
                     json={"at": time.time()})
check("没带环境 400", r.status_code == 400 and "environment is required" in j(r)["error"], j(r))
r = c.post("/api/restore", headers={"X-Admin-Token": "test-token",
                                    "X-Environment": "nope"},
           json={"at": time.time()})
check("环境不存在 404", r.status_code == 404 and "environment not found: nope" in j(r)["error"], j(r))
check("校验失败时一个字段都不动",
      c.get("/api/flags", headers=H).get_json() == snapshot_before)
check("校验失败时整包版本不换", bundle("u1")["version"] == bundle_before["version"])

# ============================================================ 2. 基本还原
print("== 2. 整份换成那一刻的：改回来的、当时没有的 ==")
# 搭一份「过去」：alpha 默认开 + 挂配置 + 属性条件；beta 全关；gamma 依赖 alpha
create_flag("alpha", {"default_enabled": True, "config": {"theme": "dark"},
                      "targeting": {"plan": "pro"}})
create_flag("beta")
create_flag("gamma", {"default_enabled": True})
patch("gamma", {"depends_on": "alpha"})
t_past = time.time()
time.sleep(0.01)

# 「现在」的改动：alpha 改成默认关并清配置、beta 全关、gamma 解除依赖并放量 50%；
# 另建一个过去还没有的开关 delta
patch("alpha", {"default_enabled": False, "config": None, "targeting": {}})
patch("beta", {"kill_switch": True})
patch("gamma", {"depends_on": "", "rollout_percent": 50})
create_flag("delta", {"default_enabled": True})
# alpha 在过去之前就有，但在 t_past 之后被删过又重建：重建的规矩不是「那份」，
# 还原后也必须按 t_past 那份（这里用 keep 验证无关开关不动；alpha 直接覆盖即可）

# 还原前先有人拿过包（还原后旧包要过期）
v_alpha_user = bundle("u2")
check("还原前 delta 在结果里", "delta" in v_alpha_user["flags"])

r = restore(t_past)
check("还原成功", r.status_code == 200 and j(r).get("restored") is True, j(r))
body = j(r)
check("响应列出还原后的开关", body["flags"] == ["alpha", "beta", "gamma", "keep"], body)
check("响应标出新建/更新/删除",
      body["created"] == [] and set(body["updated"]) == {"alpha", "beta", "gamma"}
      and body["unchanged"] == ["keep"] and body["deleted"] == ["delta"], body)
check("响应回显 at", body["at"] == t_past, body)

now_flags = flags_by_name()
check("当时没有的开关整个没了", "delta" not in now_flags)
check("还在的开关数量正确", set(now_flags) == {"alpha", "beta", "gamma", "keep"})

# 与那一刻的 /api/history 逐字段一致（history 只回结果，这里直接用规则对比）
past_bundle = history("u2", t_past)
now_bundle = bundle("u2")
check("换完后整包结果 == 那一刻来问会拿到的",
      now_bundle["flags"] == past_bundle["flags"],
      (now_bundle["flags"], past_bundle["flags"]))
check("换完后 delta 不再混进结果", "delta" not in now_bundle["flags"])

# alpha 按过去那份：默认开、带配置；不带属性时 targeting 不影响，结果为开带配置
r = check_flag("alpha", "u2")
check("alpha 按过去的默认开", r["enabled"] is True and r["reason"] == "default"
      and r.get("config") == {"theme": "dark"}, r)
# beta 过去没全关、默认关
r = check_flag("beta", "u2")
check("beta 回到过去（默认关、不全关）",
      r["enabled"] is False and r["reason"] == "default", r)
# gamma 过去依赖 alpha：alpha 开 -> gamma 开；现在把 alpha 关了它再走依赖关
patch("alpha", {"kill_switch": True})
r = check_flag("gamma", "u2")
check("依赖边也按过去那份回来了", r["enabled"] is False and r["reason"] == "depends_on", r)
patch("alpha", {"kill_switch": False})

# 单查一个已删开关 -> 404（而不是按现在规矩复活）
r = c.get("/api/flags/delta/check?identity=u2")
check("当时没有的开关查不到", r.status_code == 404, j(r))

# ============================================================ 3. 旧包过期
print("== 3. 已发整包统一换新版本 ==")
r = bundle("u2", version=v_alpha_user["version"])
check("旧包过期", r["valid"] is False and r["version"] != v_alpha_user["version"], r)
check("过期响应附带按那份重算的新包", r["flags"] == past_bundle["flags"])
inv = c.get("/api/bundles/invalidations?limit=500", headers=H).get_json()
check("失效记录写明是这次还原",
      any(x["change"].startswith("restore_flags as of")
          and x["identity"] == "u2" for x in inv), inv[:3])
aud = c.get("/api/audit?limit=500", headers=H).get_json()
check("审计记下这次还原", any(x["action"] == "restore_flags" for x in aud), aud)
check("审计逐开关记下还原",
      sum(1 for x in aud if x["action"] == "restore_flag") >= 3
      and any(x["action"] == "restore_delete" and x["flag_name"] == "delta" for x in aud),
      aud[:8])

# ============================================================ 4. 定时与发布稿
print("== 4. 约了没到点的不算、到了点的算；没发布的稿不算 ==")
# 干净场景：两个默认关开关
create_flag("s1")
create_flag("s2")
t0 = time.time()
# t0 之后：给 s1 约一个「未来」的默认开；给 s2 约一个「马上到点」的默认开
future = time.time() + 3600
patch("s1", {"default_enabled": True, "effective_at": future})
due_in = time.time() + 0.3
patch("s2", {"default_enabled": True, "effective_at": due_in})
# 开一个永不发布的稿：s1 全关
dr = post("/api/drafts", {"note": "not yet"})
did = j(dr)["draft_id"]
r = c.put(f"/api/drafts/{did}/flags/s1", headers=H, json={"kill_switch": True})
assert r.status_code == 200, j(r)
# 再开一个稿并立即发布（已发布、published_at<=还原时刻 的要算进那份）：s2 全关
dr = post("/api/drafts", {"note": "shipped"})
did2 = j(dr)["draft_id"]
c.put(f"/api/drafts/{did2}/flags/s2", headers=H, json={"kill_switch": True})
pr = c.post(f"/api/drafts/{did2}/publish", headers=H)
assert pr.status_code == 200, j(pr)
time.sleep(0.4)  # 越过 s2 的预约点（让惰性应用过，语义更明确）
check("到点的预约已生效（s2 被后来的稿全关）",
      check_flag("s2", "u1")["reason"] == "kill_switch")

# 还原到 t0：那时两个开关都是默认关、无稿、无预约
r = restore(t0)
check("还原成功", r.status_code == 200, j(r))
check("未来的预约被取消", scheduled() == [], scheduled())
r = check_flag("s1", "u1")
check("没到点的预约没算进那份（s1 默认关）",
      r["enabled"] is False and r["reason"] == "default", r)
r = check_flag("s2", "u1")
check("t0 之后才到点的预约也不算（s2 默认关、无全关）",
      r["enabled"] is False and r["reason"] == "default", r)
# 没发布的稿原样保留（仍是 open，里面的改动没生效）
drafts = c.get("/api/drafts", headers=H).get_json()
open_draft = next(d for d in drafts if d["id"] == did)
check("没发布的稿不删不改", open_draft["status"] == "open"
      and open_draft["flags"]["s1"]["kill_switch"] is True, open_draft)
# 已发布的稿是 t0 之后才发的，还原后其效果消失；稿记录本身保留（已发布历史）
all_drafts = c.get("/api/drafts?all=1", headers=H).get_json()
check("已发布的稿记录保留但效果不复活",
      any(d["id"] == did2 and d["status"] == "published" for d in all_drafts)
      and check_flag("s2", "u1")["reason"] == "default")

# 等过原来的未来时刻也不会再生效
# （用一条新的预约验证还原之后的新预约正常，旧的已取消）
patch("s1", {"default_enabled": True, "effective_at": time.time() + 3600})
check("还原之后仍可新约", len(scheduled()) == 1, scheduled())

# 到了点但「还没有请求触发」的预约也要算进那份
create_flag("s3")
t1 = time.time()
time.sleep(0.01)
patch("s3", {"default_enabled": True, "effective_at": time.time() + 0.2})
time.sleep(0.35)
# 注意：不发任何触发请求，直接还原到「预约点之后」
r = restore(time.time())
check("还原请求自身先应用到点预约（s3 现为开）", r.status_code == 200, j(r))
check("到点即触发（s3 开）", check_flag("s3", "u1")["enabled"] is True)
# 再还原回 t1：s3 在 t1 是默认关；那条预约已 applied，重放 t1 时 effective_at>t1 不算
r = restore(t1)
check("还原回预约前：s3 默认关",
      check_flag("s3", "u1")["enabled"] is False, j(r))

# ============================================================ 5. 按人/按组状态不动
print("== 5. 单人强制 / 冻结 / 互斥组 / 对照组 / 身份合并不被还原改动 ==")
create_flag("p1", {"default_enabled": False})
# 单人强制：让 u9 强制开
r = c.put("/api/flags/p1/overrides", headers=H,
          json={"identity": "u9", "enabled": True})
assert r.status_code == 200, j(r)
# 冻结：把 u8 此刻（默认关）冻住
r = c.put("/api/flags/p1/freezes", headers=H, json={"identity": "u8"})
assert r.status_code == 200, j(r)
# 互斥组：p1 与 p2 同组，u7 已落定 p2
create_flag("p2", {"default_enabled": True})
c.post("/api/groups", headers=H, json={"name": "g1"})
c.put("/api/groups/g1/flags", headers=H, json={"flag": "p1"})
c.put("/api/groups/g1/flags", headers=H, json={"flag": "p2"})
assert check_flag("p2", "u7")["reason"] == "group"
# 对照组：u6 在对照组里
c.put("/api/control-group", headers=H, json={"identities": ["u6"]})
t2 = time.time()
time.sleep(0.01)
# 现在：p1 改成默认开（规矩变了，按人状态没动）
patch("p1", {"default_enabled": True})
# 还原回 t2：规矩回到「p1 默认关」
r = restore(t2)
check("还原成功", r.status_code == 200, j(r))
check("p1 规矩回到默认关", rules_of("p1")["default_enabled"] is False, rules_of("p1"))
check("单人强制仍然生效（u9 开、reason=override）",
      check_flag("p1", "u9")["enabled"] is True
      and check_flag("p1", "u9")["reason"] == "override")
r = check_flag("p1", "u8")
check("冻结仍然生效（u8 冻在关、reason=freeze）",
      r["enabled"] is False and r["reason"] == "freeze", r)
# 规矩回到「p1 默认关」：u7 对 p1 是自然关，本就不参与组裁决（reason=default）；
# 互斥组的落定仍挂在 p2 上，没被动过
r = check_flag("p1", "u7")
check("p1 回到默认关（自然关，不参与组裁决）",
      r["enabled"] is False and r["reason"] == "default", r)
r = check_flag("p2", "u7")
check("u7 的组落定仍是 p2", r["enabled"] is True and r["reason"] == "group", r)
groups = c.get("/api/groups", headers=H).get_json()
check("互斥组成员关系不变",
      next(g for g in groups if g["name"] == "g1")["flags"] == ["p1", "p2"], groups)
ctrl = c.get("/api/control-group", headers=H).get_json()
check("对照组名单不变", ctrl["identities"] == ["u6"], ctrl)
r = check_flag("p1", "u6")
check("对照组仍只走默认值", r["enabled"] is False and r["reason"] == "control", r)
# 身份合并也不动
r = c.post("/api/identities/merges", headers=H,
           json={"identities": ["m1", "m2"]})
assert r.status_code in (200, 201) and j(r).get("ok") is True, j(r)
t3 = time.time()
restore(t3)
merges = c.get("/api/identities/merges", headers=H).get_json()
check("身份合并不被还原改动",
      any(x["identities"] == ["m1", "m2"] for x in merges), merges)

# ============================================================ 6. 删除的开关与级联
print("== 6. 当时已删的开关：还原后删除，按人状态级联清掉 ==")
create_flag("gone", {"default_enabled": True})
c.put("/api/flags/gone/overrides", headers=H,
      json={"identity": "u5", "enabled": False})
c.put("/api/groups/g1/flags", headers=H, json={"flag": "gone"})
t4 = time.time()
time.sleep(0.01)
delete_flag("gone")
t_after_delete = time.time()
check("删除后 gone 不在了", "gone" not in flags_by_name())
# 此刻把 p1 改一下，确保还原确实在干活
patch("p1", {"kill_switch": True})
# 还原到「gone 已删、p1 还没全关」的时刻：gone 必须保持删除
r = restore(t_after_delete)
check("还原成功", r.status_code == 200, j(r))
check("gone 仍然不在（那一刻它已删）", "gone" not in flags_by_name())
check("p1 的全关被还原掉", rules_of("p1")["kill_switch"] is False)
g1 = next(g for g in c.get("/api/groups", headers=H).get_json()
          if g["name"] == "g1")
check("被删开关不在组里（外键级联）", "gone" not in g1["flags"], g1["flags"])

# 当时还在、后来才删的开关：还原会把它按那一刻的规矩重新建出来
create_flag("revived", {"default_enabled": True, "config": {"v": 1}})
t5 = time.time()
time.sleep(0.01)
delete_flag("revived")
r = restore(t5)
check("还原成功", r.status_code == 200, j(r))
check("当时还在的开关被重新建出", "revived" in flags_by_name())
check("重建的规矩与那一刻一致",
      rules_of("revived") == {"default_enabled": True, "rollout_percent": 0,
                              "rollout_condition": {}, "rollout_rules": [],
                              "variants": [], "kill_switch": False,
                              "targeting": {}, "config": {"v": 1},
                              "depends_on": ""},
      rules_of("revived"))
r = check_flag("revived", "u1")
check("重建的开关来问按那一刻规矩（默认开带配置）",
      r["enabled"] is True and r["reason"] == "default"
      and r.get("config") == {"v": 1}, r)

# 要删的两个开关之间还挂着依赖边：删除顺序不能踩外键 / 留下悬空边
create_flag("newdep", {"default_enabled": True})
create_flag("latedep", {"default_enabled": True})
patch("latedep", {"depends_on": "newdep"})
# keep 指向 newdep（保留开关依赖一个要删的）：还原后这条边必须解除
patch("keep", {"depends_on": "newdep"})
r = restore(t5)
check("还原成功（删除链上有依赖边）", r.status_code == 200, j(r))
check("两个后建的开关都删掉",
      "newdep" not in flags_by_name() and "latedep" not in flags_by_name())
check("保留开关指向已删开关的边被解除", rules_of("keep")["depends_on"] == "",
      rules_of("keep"))
r = check_flag("keep", "u1")
check("解除后 keep 正常求值", r["enabled"] is True and r["reason"] == "default", r)
patch("keep", {"depends_on": ""})

# ============================================================ 7. 还原后历史口径连续
print("== 7. 历史重放：还原前的时刻照旧、还原后来问按新的算 ==")
# t4 那一刻 gone 还存在
h_t4 = history("u5", t4)
check("t4 那一刻历史里还有 gone", "gone" in h_t4["flags"], h_t4["flags"])
# gone 删除之后、还原之前的时刻：gone 已删
h_mid = history("u5", t_after_delete)
check("删除之后的历史里 gone 不在", "gone" not in h_mid["flags"], h_mid["flags"])
# 现在（还原之后）问历史 at=now：gone 仍不在
h_now = history("u5", time.time())
check("还原之后的历史里 gone 不在", "gone" not in h_now["flags"], h_now["flags"])
check("还原之后的历史里 p1 不全关",
      h_now["flags"]["p1"]["reason"] != "kill_switch", h_now["flags"]["p1"])

# 幂等：把同一份再还原一次 -> 没有开关变化，只是再取消一遍（此时无待生效预约）
r = restore(t5)
body = j(r)
check("原样再还原是幂等 no-op",
      r.status_code == 200 and body["updated"] == [] and body["deleted"] == []
      and body["created"] == [], body)
v_before = bundle("u1")["version"]
restore(t5)
check("幂等还原不推进整包版本", bundle("u1")["version"] == v_before)

# ============================================================ 8. 环境隔离
print("== 8. 只动指定环境 ==")
flag_app.init_environment_db("other", "bob")
create_flag("other-only", {"default_enabled": True})
r_other_before = c.get("/api/bundle?identity=u1",
                       headers={**H, "X-Environment": "other"}).get_json()
# test 环境再做一次还原
restore(t0)
r_other_after = c.get("/api/bundle?identity=u1",
                      headers={**H, "X-Environment": "other"}).get_json()
check("另一个环境的结果与版本一字不变",
      r_other_after == r_other_before, (r_other_after, r_other_before))

# ============================================================ 9. 还原到环境最初（整份清空）
print("== 9. 那一刻一个开关都没有：整份清空 ==")
# 本环境所有开关都是这次测试建的，0.0 早于库内任何事件 -> 那份是空的
r = restore(0.0)
body = j(r)
check("还原到空状态成功", r.status_code == 200 and body["flags"] == [], body)
check("现存开关全部被删", body["deleted"] != [] and flags_by_name() == {}, body)
check("整包是空的", bundle("u1")["flags"] == {})
# 按人状态仍在（不属于规矩）：只是没有开关可作用了
ctrl = c.get("/api/control-group", headers=H).get_json()
check("对照组名单仍在", "u6" in ctrl["identities"], ctrl)

print(f"\n{'=' * 40}\nPASS {passed}  FAIL {failed}")
if failed:
    raise SystemExit(1)
