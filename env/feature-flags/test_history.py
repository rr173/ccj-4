"""历史时刻查询（GET /api/history?identity=&at=&attrs=）的端到端测试。

覆盖需求（口语版）：
1. 调用方能指定一个过去的时刻，问某个人当时每个开关开还是关；开着的要带上
   当时那份配置。
2. 当时已冻住的按冻住的算（含冻住那一刻的配置快照）；当时全关 / 全关判关的
   不带配置。
3. 问出来必须跟那一刻真来问（整包口径）会拿到的一字不差。
4. 约了时间还没到点的改动不能算进去；到了点的算（哪怕没人触发过惰性应用）；
   没发布的稿不能算，发布了的按发布时刻算。
5. 原来问现在的（check）、拿现在的整包（bundle）、带属性问都不能坏。
另含：删除开关 / 删依赖 / 组与落定 / 强制 / 改配置 / 鉴权与参数校验。
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


def history(identity, at, attrs=None):
    r = c.get(f"/api/history?{q(identity, attrs)}&at={at}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def patch(flag, body, headers=H):
    return c.patch(f"/api/flags/{flag}", headers=headers, json=body)


def freeze(flag, identity):
    return c.put(f"/api/flags/{flag}/freezes", headers=H,
                 json={"identity": identity})


def unfreeze(flag, identity):
    return c.delete(f"/api/flags/{flag}/freezes", headers=H,
                    json={"identity": identity})


PRO = {"plan": "pro"}
FREE = {"plan": "free"}

# ---------------------------------------------------------------- 准备开关
c.post("/api/flags", headers=H, json={"name": "fon", "default_enabled": True,
                                      "config": {"v": "init"}})
c.post("/api/flags", headers=H, json={"name": "foff"})
c.post("/api/flags", headers=H, json={"name": "tgt", "default_enabled": False})
patch("tgt", {"targeting": {"plan": "pro"}, "config": {"only": "pro"}})
c.post("/api/flags", headers=H, json={"name": "dep", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "child", "default_enabled": True})
patch("child", {"depends_on": "dep", "config": {"child": 1}})
c.post("/api/groups", headers=H, json={"name": "gg"})
c.post("/api/flags", headers=H, json={"name": "ga", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "gb", "default_enabled": True})
c.put("/api/groups/gg/flags", headers=H, json={"flag": "ga"})
c.put("/api/groups/gg/flags", headers=H, json={"flag": "gb"})

print("== 1. 基本形态：过去时刻每个开关的开/关与配置 ==")
t0 = time.time()
time.sleep(0.02)
h = history("u1", t0)
check("回包带 identity / at / flags",
      h["identity"] == "u1" and h["at"] == t0 and "flags" in h, h)
check("默认开的开关当时开、带当时配置",
      h["flags"]["fon"]["enabled"] is True
      and h["flags"]["fon"]["reason"] == "default"
      and h["flags"]["fon"]["config"] == {"v": "init"}, h["flags"]["fon"])
check("默认关的开关当时关、不带配置",
      h["flags"]["foff"]["enabled"] is False
      and h["flags"]["foff"]["reason"] == "default"
      and "config" not in h["flags"]["foff"], h["flags"]["foff"])
check("没带 attrs 时回包没有 attrs 键", "attrs" not in h)

print("== 2. 改配置后：问改前拿旧配置、问改后拿新配置 ==")
patch("fon", {"config": {"v": "second"}}, headers=H_BOB)
t1 = time.time()
time.sleep(0.02)
h_before = history("u1", t0)
h_after = history("u1", t1)
check("改前的时刻：拿旧配置", h_before["flags"]["fon"]["config"] == {"v": "init"})
check("改后的时刻：拿新配置", h_after["flags"]["fon"]["config"] == {"v": "second"})
# 清配置
patch("fon", {"config": None})
t2 = time.time()
check("清配置之前还带配置", history("u1", t1)["flags"]["fon"].get("config") == {"v": "second"})
check("清配置之后不带配置", "config" not in history("u1", t2)["flags"]["fon"])
patch("fon", {"config": {"v": "second"}})

print("== 3. 跟那一刻真来问（整包口径）一字不差 ==")
# 在 t3 时刻真的来拿一次整包（并让 ga 在组内落定），之后再改世界；
# 用历史接口问 t3，每个开关的 enabled/reason/config 必须与当时那包一致
b_t3 = bundle("u2")
t3 = next(b["updated_at"] for b in c.get("/api/bundles", headers=H).get_json()
          if b["identity"] == "u2" and b["attrs"] == {})
# 再用几次改动把世界改掉
patch("fon", {"default_enabled": False})
patch("foff", {"default_enabled": True})
patch("dep", {"kill_switch": True})
c.put("/api/flags/ga/overrides", headers=H, json={"identity": "u2", "enabled": False})
time.sleep(0.02)
h_t3 = history("u2", t3)
same = all(
    h_t3["flags"].get(n, {}).get("enabled") == v["enabled"]
    and h_t3["flags"].get(n, {}).get("reason") == v["reason"]
    and h_t3["flags"].get(n, {}).get("config") == v.get("config")
    for n, v in b_t3["flags"].items())
check("历史接口问 t3 与当时真拿的整包逐开关一致", same,
      (b_t3["flags"], h_t3["flags"]))
# 组：t3 整包按名字序让 ga 落定为开、gb 判关；历史也该是
check("历史里组裁决与当时一致（ga 赢、gb 关）",
      h_t3["flags"]["ga"]["enabled"] is True
      and h_t3["flags"]["ga"]["reason"] == "group"
      and h_t3["flags"]["gb"]["enabled"] is False
      and h_t3["flags"]["gb"]["reason"] == "group", h_t3["flags"])
# 还原世界
patch("fon", {"default_enabled": True})
patch("foff", {"default_enabled": False})
patch("dep", {"kill_switch": False})
c.delete("/api/flags/ga/overrides", headers=H, json={"identity": "u2"})

print("== 4. 没真查过的身份：组落定按整包口径（名字序）确定性还原 ==")
# u3 从未来问过；组内在 t0 起 ga/gb 都自然开 -> 名字序首个 ga 落定
h = history("u3", time.time())
check("没落定记录时按名字序：ga 开(group)、gb 关(group)",
      h["flags"]["ga"]["enabled"] is True and h["flags"]["ga"]["reason"] == "group"
      and h["flags"]["gb"]["enabled"] is False
      and h["flags"]["gb"]["reason"] == "group", h["flags"])
# 单查与整包的口径差异仍保留：真来查 gb（单查）会让 gb 落定为开
r = check_flag("gb", "u4")
check("真单查 gb：gb 自己落定为开", r["enabled"] is True and r["reason"] == "group", r)
t4 = time.time()
h = history("u4", t4)
check("历史问 u4：gb 已落定为开（查询写副作用也进流水）",
      h["flags"]["gb"]["enabled"] is True
      and h["flags"]["gb"]["reason"] == "group", h["flags"])

print("== 5. 冻住：历史按当时冻结状态与快照算 ==")
patch("fon", {"config": {"v": "now"}})
freeze("fon", "u5")  # 此刻默认开，冻在开，快照 {"v":"now"}
t_fz = time.time()
time.sleep(0.02)
# 冻住后改默认、改配置、加强制，都不动冻住的值
patch("fon", {"default_enabled": False, "config": {"v": "later"}})
c.put("/api/flags/fon/overrides", headers=H, json={"identity": "u5", "enabled": False})
t_later = time.time()
h = history("u5", t_later)
check("冻住之后：历史仍给冻住的开 + 冻住那一刻的配置快照",
      h["flags"]["fon"]["enabled"] is True
      and h["flags"]["fon"]["reason"] == "freeze"
      and h["flags"]["fon"]["config"] == {"v": "now"}, h["flags"]["fon"])
h = history("u5", t0)
check("冻住之前的时刻：还没有冻结、按当时规则（开 + 初始配置）",
      h["flags"]["fon"]["enabled"] is True
      and h["flags"]["fon"]["reason"] == "default"
      and h["flags"]["fon"]["config"] == {"v": "init"}, h["flags"]["fon"])
# 全关仍压过冻住
patch("fon", {"kill_switch": True})
t_kill = time.time()
h = history("u5", t_kill)
check("冻住 + 全关：关、reason=kill_switch、不带配置",
      h["flags"]["fon"]["enabled"] is False
      and h["flags"]["fon"]["reason"] == "kill_switch"
      and "config" not in h["flags"]["fon"], h["flags"]["fon"])
patch("fon", {"kill_switch": False})
# 解冻后：按解冻时刻的规则（默认关 + 强制关 -> override 关）
unfreeze("fon", "u5")
t_unfz = time.time()
h = history("u5", t_unfz)
check("解冻之后：历史按当时规则（强制关）、不带配置",
      h["flags"]["fon"]["enabled"] is False
      and h["flags"]["fon"]["reason"] == "override"
      and "config" not in h["flags"]["fon"], h["flags"]["fon"])
# 冻在关：不带配置
c.delete("/api/flags/fon/overrides", headers=H, json={"identity": "u5"})
freeze("foff", "u5")
t_fzoff = time.time()
h = history("u5", t_fzoff)
check("冻在关：关、freeze、不带配置",
      h["flags"]["foff"]["enabled"] is False
      and h["flags"]["foff"]["reason"] == "freeze"
      and "config" not in h["flags"]["foff"], h["flags"]["foff"])
unfreeze("foff", "u5")
# 还原 fon：默认开、配置 second、无全关
patch("fon", {"default_enabled": True, "kill_switch": False,
              "config": {"v": "second"}})

print("== 6. 定时变更：没到点不算，到点算（不用有人触发） ==")
# 已到点但仍 pending（没人触发过惰性应用）的预约：重放也必须认。直接往预约表
# 插一条 pending 记录，并临时关掉惰性应用器，保证它在测试期间不被任何请求应用
import sqlite3
patch("foff", {"default_enabled": False})
t_before_sched = time.time()
effective = t_before_sched + 0.01
_dbb = sqlite3.connect(os.environ["FLAG_DB"])
_fid = _dbb.execute("SELECT id FROM flags WHERE name='foff'").fetchone()[0]
_dbb.execute(
    "INSERT INTO scheduled_changes"
    " (flag_id, flag_name, changes, effective_at, status, created_by, created_at)"
    " VALUES (?,?,?,?, 'pending', 'alice', ?)",
    (_fid, "foff", json.dumps({"default_enabled": True}), effective,
     t_before_sched - 1))
_dbb.commit()
_dbb.close()
_orig_apply = flag_app.apply_due_scheduled_changes
flag_app.apply_due_scheduled_changes = lambda: None
h = history("u6", t_before_sched)
check("到点前（effective_at 之前）：历史仍按旧默认关",
      h["flags"]["foff"]["enabled"] is False, h["flags"]["foff"])
time.sleep(0.05)
h = history("u6", time.time())
check("已到点但没人触发：历史也按到点后算（默认开）",
      h["flags"]["foff"]["enabled"] is True
      and h["flags"]["foff"]["reason"] == "default", h["flags"]["foff"])
# 恢复惰性应用：真实请求触发后，线上结果与重放一致
flag_app.apply_due_scheduled_changes = _orig_apply
check_flag("fon", "u6")
check("惰性应用后真来问也是开", check_flag("foff", "u6")["enabled"] is True)
# 被取消的预约：到点也不算
t_before2 = time.time()
r = patch("foff", {"kill_switch": True, "effective_at": time.time() + 0.3})
cid = r.get_json()["change_id"]
c.delete(f"/api/scheduled-changes/{cid}", headers=H)
time.sleep(0.45)
h = history("u6", time.time())
check("取消的预约到点也不算（没有全关）",
      h["flags"]["foff"]["enabled"] is True, h["flags"]["foff"])
check("到点前的时刻：取消的预约更不算",
      history("u6", t_before2)["flags"]["foff"]["enabled"] is True)
patch("foff", {"default_enabled": False, "kill_switch": False})

print("== 7. 发布稿：没发布不算；发布按发布时刻一起算 ==")
# foff 此刻默认关（第 6 节末尾还原），稿里把全关打开：发布前/丢弃后都仍是
# 默认关（default），发布后才是 kill_switch
did = c.post("/api/drafts", headers=H, json={"note": "历史稿"}).get_json()["draft_id"]
c.put(f"/api/drafts/{did}/flags/foff", headers=H,
      json={"kill_switch": True, "config": {"draft": True}})
v_open = time.time()
time.sleep(0.05)
h = history("u7", time.time())
check("未发布的稿：历史里一字不生效（仍默认关）",
      h["flags"]["foff"]["enabled"] is False
      and h["flags"]["foff"]["reason"] == "default", h["flags"]["foff"])
c.post(f"/api/drafts/{did}/publish", headers=H)
t_pub = time.time()
h = history("u7", t_pub)
check("发布后：稿里的全关生效、不带配置",
      h["flags"]["foff"]["enabled"] is False
      and h["flags"]["foff"]["reason"] == "kill_switch"
      and "config" not in h["flags"]["foff"], h["flags"]["foff"])
check("发布前的时刻：稿不生效（仍默认关）",
      (lambda x: x["flags"]["foff"]["enabled"] is False
       and x["flags"]["foff"]["reason"] == "default")(history("u7", v_open)))
# 丢弃的稿也不算
did2 = c.post("/api/drafts", headers=H, json={}).get_json()["draft_id"]
c.put(f"/api/drafts/{did2}/flags/foff", headers=H,
      json={"kill_switch": False, "default_enabled": False})
c.delete(f"/api/drafts/{did2}", headers=H)
check("丢弃的稿不生效（foff 仍全关中）",
      history("u7", time.time())["flags"]["foff"]["reason"] == "kill_switch")
patch("foff", {"kill_switch": False, "default_enabled": False})

print("== 8. 带属性问：用当时的属性条件 + 来问时这身属性 ==")
h_pro = history("u8", time.time(), PRO)
h_free = history("u8", time.time(), FREE)
check("对得上当时条件：targeting 开、带当时配置",
      h_pro["flags"]["tgt"]["enabled"] is True
      and h_pro["flags"]["tgt"]["reason"] == "targeting"
      and h_pro["flags"]["tgt"]["config"] == {"only": "pro"}, h_pro["flags"]["tgt"])
check("对不上：按默认关、不带配置；回包带 attrs",
      h_free["flags"]["tgt"]["enabled"] is False
      and "config" not in h_free["flags"]["tgt"]
      and h_free["attrs"] == FREE, h_free["flags"]["tgt"])
# 改条件：问改前用旧条件
t_cond = time.time()
patch("tgt", {"targeting": {"plan": "free"}, "config": {"only": "free-now"}})
time.sleep(0.02)
h = history("u8", t_cond, PRO)
check("改条件之前：pro 那身仍按旧条件开、拿旧配置",
      h["flags"]["tgt"]["enabled"] is True
      and h["flags"]["tgt"].get("config") == {"only": "pro"}, h["flags"]["tgt"])
h = history("u8", time.time(), FREE)
check("改条件之后：free 那身按新条件开、拿新配置",
      h["flags"]["tgt"]["enabled"] is True
      and h["flags"]["tgt"].get("config") == {"only": "free-now"}, h["flags"]["tgt"])

print("== 9. 删除开关：删后不在历史结果里；删前在；删依赖自动解除 ==")
c.post("/api/flags", headers=H, json={"name": "tmp", "default_enabled": True})
c.post("/api/flags", headers=H, json={"name": "tmpchild", "default_enabled": True})
patch("tmpchild", {"depends_on": "tmp"})
t_tmp = time.time()
time.sleep(0.02)
check("删前：tmp 与 tmpchild 都在且开",
      history("u9", t_tmp)["flags"]["tmp"]["enabled"] is True
      and history("u9", t_tmp)["flags"]["tmpchild"]["enabled"] is True)
c.delete("/api/flags/tmp", headers=H)
t_del_after = time.time()
h = history("u9", t_del_after)
check("删后：tmp 不在结果里", "tmp" not in h["flags"], h["flags"].keys())
check("删后：依赖它的 tmpchild 自动解除依赖、按自己默认开",
      h["flags"]["tmpchild"]["enabled"] is True
      and h["flags"]["tmpchild"]["reason"] == "default", h["flags"].get("tmpchild"))
check("删前的时刻：tmp 仍在", "tmp" in history("u9", t_tmp)["flags"])
c.delete("/api/flags/tmpchild", headers=H)

print("== 10. 单人强制 / 依赖链：历史按当时状态算 ==")
t_ov_before = time.time()
c.put("/api/flags/foff/overrides", headers=H,
      json={"identity": "u10", "enabled": True})
t_ov = time.time()
check("加强制后：历史里按 override 开",
      history("u10", t_ov)["flags"]["foff"]["enabled"] is True
      and history("u10", t_ov)["flags"]["foff"]["reason"] == "override")
check("加强制前：历史里还是默认关",
      history("u10", t_ov_before)["flags"]["foff"]["enabled"] is False)
c.delete("/api/flags/foff/overrides", headers=H, json={"identity": "u10"})
t_rm = time.time()
check("移除强制后：历史回到默认关",
      history("u10", t_rm)["flags"]["foff"]["enabled"] is False)
# 依赖链：dep 关时 child 按 depends_on 关
t_dep_before = time.time()
patch("dep", {"kill_switch": True})
t_dep = time.time()
h = history("u10", t_dep)
check("dep 全关：child 判 depends_on 关、不带配置",
      h["flags"]["dep"]["enabled"] is False
      and h["flags"]["child"]["enabled"] is False
      and h["flags"]["child"]["reason"] == "depends_on"
      and "config" not in h["flags"]["child"], h["flags"]["child"])
check("dep 全关之前：child 开着带配置",
      history("u10", t_dep_before)["flags"]["child"]["enabled"] is True
      and history("u10", t_dep_before)["flags"]["child"]["config"] == {"child": 1})
patch("dep", {"kill_switch": False})

print("== 11. 只影响别人的改动不影响这个人（按身份过滤） ==")
t_base = time.time()
c.put("/api/flags/foff/overrides", headers=H,
      json={"identity": "someone-else", "enabled": True})
freeze("fon", "someone-else")
h = history("u11", time.time())
check("给别人的强制 / 冻结不出现在 u11 的结果口径里（foff 仍默认关）",
      h["flags"]["foff"]["enabled"] is False)
check("fon 对 u11 仍按默认开（没冻他）",
      h["flags"]["fon"]["reason"] == "default" and h["flags"]["fon"]["enabled"] is True)
check("基线时刻也一样", history("u11", t_base)["flags"]["foff"]["enabled"] is False)

print("== 12. 参数校验 ==")
r = c.get("/api/history?at=1.0")
check("缺 identity 400", r.status_code == 400)
r = c.get("/api/history?identity=u1")
check("缺 at 400", r.status_code == 400)
r = c.get("/api/history?identity=u1&at=not-a-time")
check("at 非数字 400", r.status_code == 400)
r = c.get(f"/api/history?identity=u1&at={time.time() + 3600}")
check("未来时刻 400", r.status_code == 400)
r = c.get("/api/history?identity=u1&at=100&attrs=%7B%22plan%22%3A%5B%5D%7D")
check("非标量属性 400", r.status_code == 400)
check("历史接口无需鉴权",
      c.get(f"/api/history?identity=u1&at={time.time()}").status_code == 200)
# 极早时刻：没有任何开关，flags 为空
h = history("u1", 1.0)
check("比所有数据都早的时刻：flags 为空", h["flags"] == {}, h["flags"])

print("== 13. 老路不坏：现在 check / 整包 / 带属性 ==")
r = c.get("/api/flags/fon/check")
check("check 缺 identity 仍 400", r.status_code == 400)
r = c.get("/api/bundle")
check("bundle 缺 identity 仍 400", r.status_code == 400)
r = check_flag("tgt", "u12", PRO)
check("现在带属性 check 照常（targeting）",
      r["enabled"] is False)  # 条件已被第 8 节改成 plan=free
r = check_flag("tgt", "u12", FREE)
check("现在带属性 check 按新条件开", r["enabled"] is True and r["reason"] == "targeting")
b = bundle("u12")
check("现在整包照常返回版本与结果", "version" in b and "flags" in b)
# 当前时刻的历史 == 当前整包（逐开关口径一致）
h = history("u12", time.time())
cur = bundle("u12")
ok = all(h["flags"][n]["enabled"] == v["enabled"]
         and h["flags"][n]["reason"] == v["reason"]
         and h["flags"][n].get("config") == v.get("config")
         for n, v in cur["flags"].items())
check("问「现在」的历史结果与当前整包逐开关一致", ok)

print("== 14. 预约改 targeting/config：按到点时刻用当时的条件与配置 ==")
t_create_before = time.time()
c.post("/api/flags", headers=H, json={"name": "hs", "default_enabled": False})
patch("hs", {"targeting": {"plan": "pro"}, "config": {"v": "old"}})
t_hs_before = time.time()
patch("hs", {"targeting": {"plan": "free"}, "config": {"v": "new"},
             "effective_at": time.time() + 0.3})
# 到点前：pro 开旧配置
time.sleep(0.1)
h = history("u20", time.time(), PRO)
check("预约到点前：按旧条件（pro 开、旧配置）",
      h["flags"]["hs"]["enabled"] is True
      and h["flags"]["hs"]["reason"] == "targeting"
      and h["flags"]["hs"]["config"] == {"v": "old"}, h["flags"]["hs"])
h = history("u20", time.time(), FREE)
check("预约到点前：free 对不上、默认关、不带配置",
      h["flags"]["hs"]["enabled"] is False and "config" not in h["flags"]["hs"])
time.sleep(0.4)
h = history("u20", time.time(), FREE)
check("预约到点后：按新条件（free 开、新配置）",
      h["flags"]["hs"]["enabled"] is True
      and h["flags"]["hs"]["reason"] == "targeting"
      and h["flags"]["hs"]["config"] == {"v": "new"}, h["flags"]["hs"])
h = history("u20", t_create_before, PRO)
check("问创建之前的时刻：开关不存在（默认关是它没建）",
      "hs" not in h["flags"], h["flags"].keys())
h = history("u20", t_hs_before, PRO)
check("预约之前、条件已在：pro 开旧配置",
      h["flags"]["hs"]["enabled"] is True
      and h["flags"]["hs"]["reason"] == "targeting"
      and h["flags"]["hs"]["config"] == {"v": "old"}, h["flags"]["hs"])
# 清条件 / 清配置的预约也认（None 值）
patch("hs", {"targeting": None, "config": None, "default_enabled": True,
             "effective_at": time.time() + 0.3})
time.sleep(0.5)
h = history("u20", time.time())
check("预约清条件/清配置 + 默认开到点：默认开、不带配置",
      h["flags"]["hs"]["enabled"] is True
      and h["flags"]["hs"]["reason"] == "default"
      and "config" not in h["flags"]["hs"], h["flags"]["hs"])

print("== 15. 多时刻逐点对账：每一刻的历史 == 当时真拿的整包 ==")
# 全新身份：每个管理端动作后立刻记下「时刻 + 当时真整包」；最后把世界改乱，
# 再逐时刻用历史接口问，必须与当时那包逐开关一致
uid = "u-timetravel"
c.post("/api/flags", headers=H, json={"name": "ht"})
timeline = []


def mark(label):
    time.sleep(0.02)
    timeline.append((time.time(), label, bundle(uid)))


mark("初始默认关")
patch("ht", {"default_enabled": True, "config": {"v": "a"}})
mark("默认开+配置a")
patch("ht", {"config": {"v": "b"}})
mark("配置b")
c.put("/api/flags/ht/overrides", headers=H, json={"identity": uid, "enabled": False})
mark("强制关")
c.delete("/api/flags/ht/overrides", headers=H, json={"identity": uid})
mark("解除强制（默认开）")
freeze("ht", uid)
mark("冻在开")
patch("ht", {"default_enabled": False, "config": {"v": "c"}, "kill_switch": True})
mark("冻开+全关")
patch("ht", {"kill_switch": False})
mark("冻开、全关解除")
unfreeze("ht", uid)
mark("解冻（默认关、配置c但不带）")
all_ok = True
detail = ""
for at, label, snap in timeline:
    h = history(uid, at)
    for n, v in snap["flags"].items():
        hv = h["flags"].get(n)
        if hv is None or hv["enabled"] != v["enabled"] or hv["reason"] != v["reason"] \
                or hv.get("config") != v.get("config"):
            all_ok = False
            detail = f"[{label}] {n}: 当时 {v} != 历史 {hv}"
            break
check("每个时点历史都与当时真整包逐开关（含 reason/config）一致", all_ok, detail)
# 抽查两个关键时点
at_fz = next(a for a, l, _ in timeline if l == "冻在开")
h = history(uid, at_fz)
check("冻在开时点：reason=freeze、带冻住时配置 {v:b}",
      h["flags"]["ht"]["reason"] == "freeze"
      and h["flags"]["ht"]["config"] == {"v": "b"}, h["flags"]["ht"])
at_kill = next(a for a, l, _ in timeline if l == "冻开+全关")
h = history(uid, at_kill)
check("全关时点：kill_switch、不带配置",
      h["flags"]["ht"]["reason"] == "kill_switch"
      and "config" not in h["flags"]["ht"], h["flags"]["ht"])
at_un = next(a for a, l, _ in timeline if l.startswith("解冻"))
h = history(uid, at_un)
check("解冻时点：默认关、不带配置",
      h["flags"]["ht"]["reason"] == "default"
      and h["flags"]["ht"]["enabled"] is False
      and "config" not in h["flags"]["ht"], h["flags"]["ht"])

print("== 16. 发布稿带多字段：发布时刻一起进历史 ==")
c.post("/api/flags", headers=H, json={"name": "hd"})
t_draft_before = time.time()
did = c.post("/api/drafts", headers=H, json={"note": "多字段稿"}).get_json()["draft_id"]
c.put(f"/api/drafts/{did}/flags/hd", headers=H,
      json={"default_enabled": True, "rollout_percent": 100,
            "config": {"draft": "yes"}, "description": "不影响求值"})
c.post(f"/api/drafts/{did}/publish", headers=H)
t_draft_after = time.time()
h = history("u21", t_draft_after)
check("发布后：放量 100 开、带稿里配置",
      h["flags"]["hd"]["enabled"] is True
      and h["flags"]["hd"]["reason"] == "rollout"
      and h["flags"]["hd"]["config"] == {"draft": "yes"}, h["flags"]["hd"])
h = history("u21", t_draft_before)
check("发布前：稿不生效（默认关、无配置）",
      h["flags"]["hd"]["enabled"] is False and "config" not in h["flags"]["hd"])

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)