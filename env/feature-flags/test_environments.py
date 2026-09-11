"""多环境隔离的端到端测试。"""
import os
import tempfile

os.environ["FLAG_DB"] = tempfile.mktemp(suffix=".db")
os.environ["ADMIN_TOKEN"] = "test-token"

import app as flag_app  # noqa: E402

flag_app.app.config["TESTING"] = True
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


def j(r):
    return r.get_json()


def create_env(name):
    r = c.post("/api/environments", headers=H, json={"name": name})
    assert r.status_code in (201, 409), j(r)


def get(path, env=None):
    headers = dict(H)  # 管理端接口需要令牌；公开接口多带也无妨
    if env is not None:
        headers["X-Environment"] = env
    return c.get(path, headers=headers)


def post(path, env, body):
    return c.post(path, headers={**H, "X-Environment": env}, json=body)


def patch(path, env, body):
    return c.patch(path, headers={**H, "X-Environment": env}, json=body)


create_env("prod")
create_env("staging")

print("== 1. 环境必须显式且存在 ==")
r = get("/api/flags")
check("漏带环境返回 400", r.status_code == 400 and "environment is required" in j(r)["error"], j(r))
r = get("/api/flags", "nope")
check("未知环境返回 404", r.status_code == 404 and "environment not found" in j(r)["error"], j(r))
r = get("/api/flags", "prod")
check("存在环境可访问", r.status_code == 200, j(r))
r = c.get("/api/flags?environment=prod", headers=H)
check("查询参数也可指定环境", r.status_code == 200, j(r))
r = c.get("/api/flags?env=prod", headers={**H, "X-Env": "prod"})
check("短参数与短头兼容", r.status_code == 200, j(r))
r = c.get("/api/flags?env=prod", headers={"X-Env": "staging"})
check("短参数与短头冲突明确报错", r.status_code == 400, j(r))
r = c.get("/api/flags?environment=prod", headers={"X-Environment": "staging"})
check("查询参数与头冲突明确报错", r.status_code == 400, j(r))
bad = c.post("/api/environments", headers=H, json={"name": "../bad"})
check("非法环境名拒绝", bad.status_code == 400, j(bad))

print("== 2. 配置与开关互不影响 ==")
r = post("/api/flags", "prod", {"name": "same", "default_enabled": True})
check("prod 创建开关", r.status_code == 201, j(r))
r = post("/api/flags", "staging", {"name": "same", "default_enabled": False})
check("staging 创建同名开关", r.status_code == 201, j(r))
r = get("/api/flags/same/check?identity=u1", "prod")
check("prod 默认开", j(r)["enabled"] is True, j(r))
r = get("/api/flags/same/check?identity=u1", "staging")
check("staging 默认关", j(r)["enabled"] is False, j(r))

prod_b = j(get("/api/bundle?identity=u1", "prod"))
stage_b = j(get("/api/bundle?identity=u1", "staging"))
check("两环境各有整包版本", prod_b["version"] != stage_b["version"],
      (prod_b, stage_b))

print("== 3. 一个环境改动只让自己的旧包过期 ==")
r = patch("/api/flags/same", "prod", {"kill_switch": True})
check("prod 全关成功", r.status_code == 200, j(r))
new_prod = j(get(f"/api/bundle?identity=u1&version={prod_b['version']}", "prod"))
same_stage = j(get(f"/api/bundle?identity=u1&version={stage_b['version']}", "staging"))
check("prod 旧包过期", new_prod["valid"] is False and
      new_prod["flags"]["same"]["enabled"] is False, new_prod)
check("staging 的包仍然有效", same_stage["valid"] is True and
      same_stage["version"] == stage_b["version"] and
      same_stage["flags"]["same"]["enabled"] is False, same_stage)

r = get("/api/flags/same/check?identity=u1", "staging")
check("staging 求值不受 prod 全关影响", j(r)["enabled"] is False, j(r))

print("== 4. 同人同环境同属性结果恒定 ==")
first = j(get("/api/flags/same/check?identity=u1&attrs=%7B%22plan%22%3A%22pro%22%7D",
              "staging"))
for _ in range(3):
    again = j(get("/api/flags/same/check?identity=u1&attrs=%7B%22plan%22%3A%22pro%22%7D",
                  "staging"))
    check("重复单查不变", again == first, (again, first))
b1 = j(get("/api/bundle?identity=u1&attrs=%7B%22plan%22%3A%22pro%22%7D", "staging"))
b2 = j(get("/api/bundle?identity=u1&attrs=%7B%22plan%22%3A%22pro%22%7D", "staging"))
check("重复拿包版本结果不变", b1 == b2, (b1, b2))

print(f"\n{'=' * 40}\nPASS {passed}  FAIL {failed}")
if failed:
    raise SystemExit(1)
