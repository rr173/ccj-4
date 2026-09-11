"""定时生效（scheduled change）的端到端测试。

覆盖：
1. 约了时间的改动，到点前不生效：求值结果、整包结果与整包版本都不变
2. 到点后按新规则求值、整包换新版本；拿着到点前的版本来问 => 明确过期
3. 不约时间的改动仍然改完即生效（原行为不变）
4. 定时变更可取消，取消后到点也不生效
5. 生效记入失效记录与审计（操作人 = 预约人）
6. 参数校验与鉴权
7. 同一开关多个预约按生效时间先后应用；删除开关取消未生效预约
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


def bundle(identity, version=None):
    url = f"/api/bundle?identity={identity}"
    if version is not None:
        url += f"&version={version}"
    r = c.get(url)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def check_flag(flag, identity):
    r = c.get(f"/api/flags/{flag}/check?identity={identity}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def flag_row(name):
    rows = c.get("/api/flags", headers=H).get_json()
    return next(f for f in rows if f["name"] == name)


def scheduled():
    return c.get("/api/scheduled-changes", headers=H).get_json()


# ---------------------------------------------------------------- 准备开关
c.post("/api/flags", headers=H, json={"name": "sched-a"})                       # 默认关
c.post("/api/flags", headers=H, json={"name": "sched-b", "default_enabled": True})

print("== 1. 约了时间：到点前一切照旧 ==")
v0 = bundle("u1")["version"]
flags0 = bundle("u1")["flags"]
future = time.time() + 0.4
r = c.patch("/api/flags/sched-a", headers=H,
            json={"default_enabled": True, "effective_at": future})
check("预约返回 202", r.status_code == 202, r.status_code)
body = r.get_json()
check("响应标记 scheduled", body.get("scheduled") is True)
change_id = body["change_id"]

r = check_flag("sched-a", "u1")
check("到点前 check 还按现在的结果走", r["enabled"] is False and r["reason"] == "default")
b = bundle("u1")
check("到点前整包版本不换", b["version"] == v0)
check("到点前整包结果不变", b["flags"] == flags0)
check("到点前带版本来问仍有效", bundle("u1", version=v0)["valid"] is True)

sch = scheduled()
check("待生效列表里有这条预约", any(s["id"] == change_id for s in sch))
s = next(s for s in sch if s["id"] == change_id)
check("预约内容正确", s["flag"] == "sched-a" and s["changes"] == {"default_enabled": True})
check("预约人记录正确", s["created_by"] == "alice")
check("开关列表带未生效预约", len(flag_row("sched-a")["scheduled_changes"]) == 1)
check("预约列表接口要鉴权", c.get("/api/scheduled-changes").status_code == 401)
inv = c.get("/api/bundles/invalidations", headers=H).get_json()
check("到点前没有失效记录", not any("sched-a" in r["change"] for r in inv))

print("== 2. 到点后：按新规则、整包换新版本、旧包过期 ==")
time.sleep(0.6)  # 越过生效时刻
r = check_flag("sched-a", "u1")
check("到点后 check 按新规则", r["enabled"] is True and r["reason"] == "default")
b1 = bundle("u1")
check("到点后整包换新版本", b1["version"] != v0)
check("到点后整包按新规则", b1["flags"]["sched-a"]["enabled"] is True)
r = bundle("u1", version=v0)
check("拿着到点前那包来问 => 过期", r["valid"] is False)
check("过期响应附带当前新版本", r["version"] == b1["version"])
check("当前版本仍有效", bundle("u1", version=b1["version"])["valid"] is True)
check("预约生效后不在待生效列表", not any(s["id"] == change_id for s in scheduled()))

inv = [r for r in c.get("/api/bundles/invalidations", headers=H).get_json()
       if r["change"].startswith("scheduled_change sched-a")]
check("失效记录里有这次定时生效", len(inv) == 1, inv)
check("失效记录的操作人是预约人", inv[0]["actor"] == "alice")
check("失效记录的旧版本是到点前那包", inv[0]["old_version"] == v0)
check("记下的新版本与本人拿到的一致", inv[0]["new_version"] == b1["version"])
aud = c.get("/api/audit?limit=100", headers=H).get_json()
check("审计里有预约记录",
      any(a["action"] == "schedule_change" and a["flag_name"] == "sched-a" for a in aud))
check("审计里有生效记录",
      any(a["action"] == "apply_scheduled" and a["flag_name"] == "sched-a" for a in aud))

print("== 3. 不约时间：改完立即生效（原行为不变）==")
v_before = bundle("u1")["version"]
r = c.patch("/api/flags/sched-a", headers=H, json={"default_enabled": False})
check("立即修改返回 200", r.status_code == 200 and r.get_json().get("changed") == 1)
check("改完立即生效", check_flag("sched-a", "u1")["enabled"] is False)
check("立即生效整包版本也换", bundle("u1")["version"] != v_before)

print("== 4. 取消预约：到点也不生效 ==")
v_before = bundle("u1")["version"]
r = c.patch("/api/flags/sched-b", headers=H,
            json={"kill_switch": True, "effective_at": time.time() + 0.4})
cid = r.get_json()["change_id"]
r = c.delete(f"/api/scheduled-changes/{cid}", headers=H)
check("取消返回 ok", r.status_code == 200)
check("取消后不在待生效列表", not any(s["id"] == cid for s in scheduled()))
r = c.delete(f"/api/scheduled-changes/{cid}", headers=H)
check("重复取消返回 409", r.status_code == 409)
time.sleep(0.6)  # 越过原定的生效时刻
check("取消后到点也不生效", check_flag("sched-b", "u1")["enabled"] is True)
check("取消后整包版本不换", bundle("u1")["version"] == v_before)

print("== 5. 参数校验 ==")
r = c.patch("/api/flags/sched-a", headers=H,
            json={"default_enabled": True, "effective_at": time.time() - 1})
check("过去的时间 400", r.status_code == 400)
r = c.patch("/api/flags/sched-a", headers=H,
            json={"default_enabled": True, "effective_at": "tomorrow"})
check("非数字时间 400", r.status_code == 400)
r = c.patch("/api/flags/sched-a", headers=H,
            json={"effective_at": time.time() + 60})
check("没有可预约的字段 400", r.status_code == 400)
r = c.patch("/api/flags/sched-a", headers=H,
            json={"rollout_percent": 200, "effective_at": time.time() + 60})
check("非法放量比例 400", r.status_code == 400)
r = c.patch("/api/flags/sched-a", headers={"X-Actor": "x"},
            json={"default_enabled": True, "effective_at": time.time() + 60})
check("预约接口要鉴权", r.status_code == 401)
r = c.delete("/api/scheduled-changes/99999", headers=H)
check("取消不存在的预约 404", r.status_code == 404)
check("校验失败没留下预约", scheduled() == [])
check("校验失败没改动配置", flag_row("sched-a")["default_enabled"] is False)

print("== 6. 同一开关多个预约：按生效时间先后应用 ==")
c.post("/api/flags", headers=H, json={"name": "sched-c", "default_enabled": True})
t1 = time.time() + 0.3
c.patch("/api/flags/sched-c", headers=H_BOB,
        json={"rollout_percent": 30, "effective_at": t1})
c.patch("/api/flags/sched-c", headers=H_BOB,
        json={"kill_switch": True, "effective_at": t1 + 0.5})
check("同一开关可约多个", len(flag_row("sched-c")["scheduled_changes"]) == 2)
time.sleep(0.5)  # 越过第一个，未到第二个
f = flag_row("sched-c")
check("第一个预约先生效", f["rollout_percent"] == 30 and not f["kill_switch"])
check("第二个还在等待", len(f["scheduled_changes"]) == 1)
time.sleep(0.5)  # 越过第二个
f = flag_row("sched-c")
check("第二个预约也生效", f["kill_switch"] is True)
r = check_flag("sched-c", "u1")
check("全关生效后 check 为关", r["enabled"] is False and r["reason"] == "kill_switch")

print("== 7. 删除开关：未生效的预约一并取消 ==")
c.post("/api/flags", headers=H, json={"name": "sched-d"})
c.patch("/api/flags/sched-d", headers=H,
        json={"default_enabled": True, "effective_at": time.time() + 3600})
check("删除前有待生效预约", len(scheduled()) == 1)
c.delete("/api/flags/sched-d", headers=H)
check("删除开关后预约取消", scheduled() == [])

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
