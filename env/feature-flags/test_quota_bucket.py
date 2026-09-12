"""名额桶（quota bucket）的端到端测试。

覆盖需求（口语版）：
1. 管理端能在一个环境里开一个名额桶：开的时候写下这个桶有多少个名额，
   再写下这桶盯着哪些开关。少写了环境、名额或开关，或者说了个没有的环境、
   没有的开关，这次开不成，并说清楚。
2. 同一个开关不能同时被两个还开着的桶盯着（409）；关桶后可以。
3. 来问时，被这桶盯着的开关要对这个人开，得先在这个环境里占到一个名额；
   占满了，后来的人这些开关只能关（reason=quota）。
4. 同一个人已经占到的，只要人、身上属性和环境没变，多次来问还是开，
   不能因为后来的人来问就把他挤掉。
5. 没被这桶盯着的开关，不走这桶的名额。
6. 人退出名额、或者改了这个桶的人数或盯着哪些开关，再来问要按新的。
7. 名额改少了、已经占着的人比新人数多，多出来的按后占到的先让；
   被让出去的人再来问，这些开关是关。
8. 少写了要退的人，这次退不成，并说清楚（400）。
"""
import json
import os
import tempfile
import urllib.parse

os.environ["FLAG_DB"] = tempfile.mktemp(suffix=".db")
os.environ["ADMIN_TOKEN"] = "test-token"

import app as flag_app  # noqa: E402

flag_app.app.config["TESTING"] = True
flag_app.init_environment_db("test", "test")
flag_app.init_environment_db("other", "test")

# 不带默认环境头的裸客户端（测「少写环境 / 没有的环境」），要在换类之前建
plain = flag_app.app.test_client()


class _EnvClient(flag_app.FlaskClient):
    def open(self, *args, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault("X-Environment", "test")
        kwargs["headers"] = headers
        return super().open(*args, **kwargs)


flag_app.app.test_client_class = _EnvClient
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


def q(identity, attrs=None):
    s = f"identity={urllib.parse.quote(identity)}"
    if attrs is not None:
        s += "&attrs=" + urllib.parse.quote(json.dumps(attrs, ensure_ascii=False))
    return s


def check_flag(flag, identity, attrs=None):
    r = c.get(f"/api/flags/{flag}/check?{q(identity, attrs)}")
    return r.status_code, r.get_json()


def bundle(identity, attrs=None, version=None):
    s = q(identity, attrs)
    if version is not None:
        s += f"&version={version}"
    r = c.get(f"/api/bundle?{s}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def create(name, default=False, **kw):
    body = {"name": name, "default_enabled": default}
    body.update(kw)
    r = c.post("/api/flags", headers=H, json=body)
    assert r.status_code in (200, 201), r.get_json()


def bucket_create(body, headers=H):
    return c.post("/api/quota-buckets", headers=headers, json=body)


def bucket_get(bid):
    r = c.get(f"/api/quota-buckets/{bid}", headers=H)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def bucket_list():
    r = c.get("/api/quota-buckets", headers=H)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def occupants(bid):
    return [o["identity"] for o in bucket_get(bid)["occupants"]]


# ---------------------------------------------------------------- 造开关

create("f-on", default=True)          # 默认开：谁来问自然结果都是开
create("f-off", default=False)        # 默认关：自然结果都是关（永远占不到名额）
create("f-free", default=True)        # 不被任何桶盯：不走名额
create("f-second", default=True)      # 第二个被盯的开关
create("f-third", default=True)       # 换盯梢名单时用

# ---------------------------------------------------------------- 开桶：少写了 / 说错了开不成

print("== 开桶校验 ==")
r = bucket_create({"flags": ["f-on"]})
check("少写名额 400 并说清楚",
      r.status_code == 400 and "capacity" in r.get_json().get("error", ""),
      (r.status_code, r.get_json()))
r = bucket_create({"capacity": 2})
check("少写开关 400 并说清楚",
      r.status_code == 400 and "flags" in r.get_json().get("error", ""),
      (r.status_code, r.get_json()))
r = bucket_create({})
check("名额开关都少写 400", r.status_code == 400, r.get_json())
r = bucket_create({"capacity": 2, "flags": []})
check("空开关名单 400", r.status_code == 400, r.get_json())
for bad, label in ((True, "布尔名额"), (-1, "负数名额"), ("2", "字符串名额"), (1.5, "小数名额")):
    r = bucket_create({"capacity": bad, "flags": ["f-on"]})
    check(f"{label} 400", r.status_code == 400, (r.status_code, r.get_json()))
r = bucket_create({"capacity": 1, "flags": ["f-on", "nope"]})
check("说了个没有的开关 404 并说清楚",
      r.status_code == 404 and "nope" in r.get_json().get("error", ""),
      (r.status_code, r.get_json()))
r = bucket_create({"capacity": 1, "flags": ["f-on", "f-on"]})
check("开关名单重复 400", r.status_code == 400, r.get_json())
r = bucket_create({"capacity": 1, "flags": "f-on"})
check("开关不是名单 400", r.status_code == 400, r.get_json())
check("开不成时一个桶都没写库", bucket_list() == [], bucket_list())

# 少写环境 / 没有的环境（用不带默认环境头的裸客户端）
r = plain.post("/api/quota-buckets", headers=H,
               json={"capacity": 1, "flags": ["f-on"]})
check("少写环境 400 并说清楚",
      r.status_code == 400 and "environment is required" in r.get_json().get("error", ""),
      (r.status_code, r.get_json()))
r = plain.post("/api/quota-buckets?environment=nope", headers=H,
               json={"capacity": 1, "flags": ["f-on"]})
check("没有的环境 404 并说清楚",
      r.status_code == 404 and "environment not found" in r.get_json().get("error", ""),
      (r.status_code, r.get_json()))
r = bucket_create({"capacity": 1, "flags": ["f-on"]},
                  headers={"X-Admin-Token": "wrong"})
check("无管理令牌 401", r.status_code == 401, r.status_code)

# ---------------------------------------------------------------- 开桶 + 一个开关不能被两个开着的桶盯

print("== 开桶与盯梢冲突 ==")
r = bucket_create({"capacity": 2, "flags": ["f-on"], "name": "seats"})
check("开桶成功 201", r.status_code == 201, r.get_json())
B1 = r.get_json()["id"]
check("回显名额与盯梢", r.get_json()["capacity"] == 2 and
      r.get_json()["flags"] == ["f-on"] and r.get_json()["status"] == "open",
      r.get_json())
r = bucket_create({"capacity": 1, "flags": ["f-on"]})
check("同一个开关不能被两个还开着的桶盯 409 并说清楚",
      r.status_code == 409 and "f-on" in r.get_json().get("error", ""),
      (r.status_code, r.get_json()))
r = bucket_create({"capacity": 1, "flags": ["f-second"], "name": "seats"})
check("桶名撞了 409", r.status_code == 409, r.get_json())
r = bucket_create({"capacity": 1, "flags": ["f-second"]})
check("盯别的开关可以开", r.status_code == 201, r.get_json())
B2 = r.get_json()["id"]
check("列表能看到两个桶", len(bucket_list()) == 2, bucket_list())

# ---------------------------------------------------------------- 来问：先占名额，占满则关

print("== 占位与占满 ==")
st, j = check_flag("f-on", "u1")
check("u1 占到名额：开", j["enabled"] is True and st == 200, j)
check("u1 已记在占位名单里", occupants(B1) == ["u1"], occupants(B1))
st, j = check_flag("f-on", "u2")
check("u2 占到名额：开", j["enabled"] is True, j)
st, j = check_flag("f-on", "u3")
check("占满了，u3 只能关（reason=quota）",
      j["enabled"] is False and j["reason"] == "quota", j)
check("u3 没占到位", occupants(B1) == ["u1", "u2"], occupants(B1))
st, j = check_flag("f-on", "u4")
check("再来的人还是关", j["enabled"] is False and j["reason"] == "quota", j)

# 已占到的人多次来问恒定开，不被后来的人挤掉
for _ in range(3):
    st, j1 = check_flag("f-on", "u1")
    check_again = j1["enabled"] is True
    assert check_again, j1
check("u1 多次来问还是开（没被挤掉）", True)
st, j1 = check_flag("f-on", "u1")
st, j2 = check_flag("f-on", "u1")
check("u1 两次来问一字不差", j1 == j2, (j1, j2))
check("占位名单没被后来的人改变", occupants(B1) == ["u1", "u2"], occupants(B1))

# 没被盯的开关不走名额
st, j = check_flag("f-free", "u3")
check("没被盯的开关不走名额（u3 照常开）",
      j["enabled"] is True and j["reason"] == "default", j)
check("u3 占的仍是 0 个桶", occupants(B1) == ["u1", "u2"], occupants(B1))

# 自然结果是关的开关不占名额
st, j = check_flag("f-off", "u9")
check("默认关的开关来问是关（不占名额）",
      j["enabled"] is False and j["reason"] == "default", j)
r = bucket_create({"capacity": 1, "flags": ["f-off"]})
B_OFF = r.get_json()["id"]
st, j = check_flag("f-off", "u9")
check("被盯的默认关开关仍是关", j["enabled"] is False and j["reason"] == "default", j)
check("默认关不占名额", occupants(B_OFF) == [], occupants(B_OFF))

# 整包口径一致：u3 的整包里 f-on 是 quota 关，f-free 是开
b3 = bundle("u3")
check("整包：u3 的 f-on 是 quota 关",
      b3["flags"]["f-on"]["enabled"] is False and
      b3["flags"]["f-on"]["reason"] == "quota", b3["flags"]["f-on"])
check("整包：u3 的 f-free 照常开", b3["flags"]["f-free"]["enabled"] is True,
      b3["flags"]["f-free"])
b1 = bundle("u1")
check("整包：u1 的 f-on 是开", b1["flags"]["f-on"]["enabled"] is True,
      b1["flags"]["f-on"])
check("同一人多次拿整包版本一致", bundle("u1")["version"] == b1["version"])

# 带属性来问：人、属性、环境没变结果恒定
st, ja = check_flag("f-on", "u1", {"plan": "pro"})
st, jb = check_flag("f-on", "u1", {"plan": "pro"})
check("占到的人带同一身属性来问恒定开",
      ja["enabled"] is True and ja == jb, (ja, jb))

# 环境隔离：另一个环境里没有这个桶
r = plain.post("/api/flags?environment=other", headers=H,
               json={"name": "f-on", "default_enabled": True})
assert r.status_code in (200, 201), r.get_json()
r = plain.get("/api/flags/f-on/check?identity=u9&environment=other")
check("别的环境没开桶，同名开关照常开",
      r.status_code == 200 and r.get_json()["enabled"] is True, r.get_json())

# ---------------------------------------------------------------- 人退出名额

print("== 人退出名额 ==")
r = c.delete(f"/api/quota-buckets/{B1}/occupants", headers=H, json={})
check("少写了要退的人 400 并说清楚",
      r.status_code == 400 and "identity" in r.get_json().get("error", ""),
      (r.status_code, r.get_json()))
r = c.delete(f"/api/quota-buckets/{B1}/occupants", headers=H,
             json={"identity": ""})
check("要退的人是空串 400", r.status_code == 400, r.get_json())
r = c.delete(f"/api/quota-buckets/{B1}/occupants", headers=H,
             json={"identity": "u3"})
check("退一个没占着的人是 no-op",
      r.status_code == 200 and r.get_json()["changed"] is False, r.get_json())

r = c.delete(f"/api/quota-buckets/{B1}/occupants", headers=H,
             json={"identity": "u1"})
check("退出 u1 成功", r.status_code == 200 and r.get_json()["changed"] is True,
      r.get_json())
check("u1 已不在占位名单", occupants(B1) == ["u2"], occupants(B1))
# 空出一个名额：后来的人先来问先占到
st, j = check_flag("f-on", "u3")
check("空出名额后 u3 占到了：开", j["enabled"] is True, j)
check("u3 占到位", occupants(B1) == ["u2", "u3"], occupants(B1))
st, j = check_flag("f-on", "u1")
check("被退出的 u1 再来问：桶又满了只能关",
      j["enabled"] is False and j["reason"] == "quota", j)

# ---------------------------------------------------------------- 改人数：改少按后占到的先让

print("== 名额改少：后占到的先让 ==")
# 当前 B1 容量 2，占着 u2（先）、u3（后）
r = c.patch(f"/api/quota-buckets/{B1}", headers=H, json={"capacity": 1})
check("名额改少成功", r.status_code == 200, r.get_json())
check("多出来的按后占到的先让：让出的是 u3",
      r.get_json()["evicted"] == ["u3"], r.get_json())
check("占位名单只剩 u2", occupants(B1) == ["u2"], occupants(B1))
st, j = check_flag("f-on", "u3")
check("被让出去的 u3 再来问是关（quota）",
      j["enabled"] is False and j["reason"] == "quota", j)
st, j = check_flag("f-on", "u2")
check("先占到的 u2 留下，还是开", j["enabled"] is True, j)
st, j = check_flag("f-on", "u1")
check("桶满，u1 仍然关", j["enabled"] is False and j["reason"] == "quota", j)

# 名额改多：后来的人又能占到了
r = c.patch(f"/api/quota-buckets/{B1}", headers=H, json={"capacity": 3})
check("名额改多成功", r.status_code == 200 and r.get_json()["capacity"] == 3,
      r.get_json())
st, j = check_flag("f-on", "u3")
check("名额改多后 u3 又能占到：开", j["enabled"] is True, j)
st, j = check_flag("f-on", "u1")
check("名额改多后 u1 也能占到：开", j["enabled"] is True, j)
check("占位名单是 u2/u3/u1", occupants(B1) == ["u2", "u3", "u1"], occupants(B1))
st, j = check_flag("f-on", "u5")
check("又满了，u5 关", j["enabled"] is False and j["reason"] == "quota", j)

# 名额改到 0：全让出去
r = c.patch(f"/api/quota-buckets/{B1}", headers=H, json={"capacity": 0})
check("名额改到 0，全部让出",
      r.status_code == 200 and sorted(r.get_json()["evicted"]) == ["u1", "u2", "u3"],
      r.get_json())
st, j = check_flag("f-on", "u2")
check("名额为 0，原来占着的人再来问也是关",
      j["enabled"] is False and j["reason"] == "quota", j)

# 改人数的校验
r = c.patch(f"/api/quota-buckets/{B1}", headers=H, json={"capacity": "x"})
check("名额写成非数字 400", r.status_code == 400, r.get_json())
r = c.patch(f"/api/quota-buckets/{B1}", headers=H, json={})
check("什么都不改 400", r.status_code == 400, r.get_json())
r = c.patch("/api/quota-buckets/9999", headers=H, json={"capacity": 1})
check("改没有的桶 404", r.status_code == 404, r.get_json())

# ---------------------------------------------------------------- 改盯哪些开关

print("== 改盯哪些开关 ==")
r = c.patch(f"/api/quota-buckets/{B1}", headers=H, json={"capacity": 2})
assert r.status_code == 200, r.get_json()
st, j = check_flag("f-on", "u6")
check("恢复容量后 u6 占到：开", j["enabled"] is True, j)
check("占位是 u6 一个", occupants(B1) == ["u6"], occupants(B1))
r = c.patch(f"/api/quota-buckets/{B1}", headers=H,
            json={"flags": ["f-third"]})
check("改盯 f-third 成功", r.status_code == 200 and
      r.get_json()["flags"] == ["f-third"], r.get_json())
check("改盯梢不清占位：u6 仍占着", occupants(B1) == ["u6"], occupants(B1))
st, j = check_flag("f-on", "u5")
check("f-on 不再被盯：之前占不到的 u5 现在照自然规则开",
      j["enabled"] is True and j["reason"] == "default", j)
st, j = check_flag("f-third", "u6")
check("f-third 被盯上：已占着的 u6 还是开", j["enabled"] is True, j)
st, j = check_flag("f-third", "u7")
check("f-third 被盯上：u7 占到第二个名额开", j["enabled"] is True, j)
st, j = check_flag("f-third", "u8")
check("f-third 满员：u8 关（quota）",
      j["enabled"] is False and j["reason"] == "quota", j)
r = c.patch(f"/api/quota-buckets/{B1}", headers=H,
            json={"flags": ["f-second"]})
check("改盯已被别的开着的桶盯的开关 409",
      r.status_code == 409 and "f-second" in r.get_json().get("error", ""),
      (r.status_code, r.get_json()))
r = c.patch(f"/api/quota-buckets/{B1}", headers=H,
            json={"flags": ["nope"]})
check("改盯没有的开关 404", r.status_code == 404, r.get_json())
r = c.patch(f"/api/quota-buckets/{B1}", headers=H, json={"flags": []})
check("改盯空名单 400", r.status_code == 400, r.get_json())
check("改不成时盯梢不变", bucket_get(B1)["flags"] == ["f-third"],
      bucket_get(B1))

# ---------------------------------------------------------------- 整包版本：改桶后旧包过期

print("== 整包版本 ==")
# 当前 B1 容量 2，占着 u6（先）、u7（后），盯 f-third
v_u6 = bundle("u6")["version"]
v_u7 = bundle("u7")["version"]
v_u8 = bundle("u8")["version"]
r = c.patch(f"/api/quota-buckets/{B1}", headers=H, json={"capacity": 1})
check("名额改少，让出的是后占到的 u7",
      r.status_code == 200 and r.get_json()["evicted"] == ["u7"], r.get_json())
check("占位只剩 u6", occupants(B1) == ["u6"], occupants(B1))
st, j = check_flag("f-third", "u6")
check("先占到的 u6 留下仍开", j["enabled"] is True, j)
st, j = check_flag("f-third", "u7")
check("被让出的 u7 再来问是关（quota）",
      j["enabled"] is False and j["reason"] == "quota", j)
check("被让出的 u7 旧版本过期 valid=false",
      bundle("u7", version=v_u7)["valid"] is False)
check("留下的 u6 版本一字不变 valid=true",
      bundle("u6", version=v_u6)["valid"] is True)
check("无关的 u8（本来就没占到）版本不变",
      bundle("u8", version=v_u8)["valid"] is True)

# ---------------------------------------------------------------- 关桶

print("== 关桶 ==")
r = c.post(f"/api/quota-buckets/{B1}/close", headers=H)
check("关桶成功", r.status_code == 200 and r.get_json()["changed"] is True,
      r.get_json())
check("关桶后占位清空", occupants(B1) == [], occupants(B1))
st, j = check_flag("f-third", "u9")
check("关桶后 f-third 不再走名额：u9 照自然规则开",
      j["enabled"] is True and j["reason"] == "default", j)
r = c.post(f"/api/quota-buckets/{B1}/close", headers=H)
check("已关再关是幂等 no-op", r.status_code == 200 and
      r.get_json()["changed"] is False, r.get_json())
r = c.patch(f"/api/quota-buckets/{B1}", headers=H, json={"capacity": 5})
check("已关的桶不能再改 409", r.status_code == 409, r.get_json())
r = c.delete(f"/api/quota-buckets/{B1}/occupants", headers=H,
             json={"identity": "u7"})
check("已关的桶不能再退人 409", r.status_code == 409, r.get_json())
r = bucket_create({"capacity": 1, "flags": ["f-third"]})
check("关桶后它盯的开关可以被新桶盯", r.status_code == 201, r.get_json())
B3 = r.get_json()["id"]
st, j = check_flag("f-third", "u9")
check("新桶生效：u9 占到开", j["enabled"] is True, j)
st, j = check_flag("f-third", "u10")
check("新桶生效：u10 占不到关",
      j["enabled"] is False and j["reason"] == "quota", j)

# ---------------------------------------------------------------- 身份合并：名额按主身份算

print("== 身份合并 ==")
# 单独造一个开关和桶，避免前面整包求值（整包也会对每个开关求值、可能占位）
create("f-merge", default=True)
r = bucket_create({"capacity": 1, "flags": ["f-merge"]})
assert r.status_code == 201, r.get_json()
B4 = r.get_json()["id"]
r = c.post("/api/identities/merges", headers=H,
           json={"identities": ["qa-a", "qa-b"]})
assert r.status_code in (200, 201), r.get_json()
st, j = check_flag("f-merge", "qa-a")
check("qa-a 占到名额开", j["enabled"] is True, j)
st, j = check_flag("f-merge", "qa-b")
check("同一个人的别名来问也是开（名额按主身份算）",
      j["enabled"] is True, j)
check("占位记在主身份名下", occupants(B4) == ["qa-a"], occupants(B4))
r = c.delete(f"/api/quota-buckets/{B4}/occupants", headers=H,
             json={"identity": "qa-b"})
check("给别名退名额，退的是主身份", r.status_code == 200 and
      r.get_json()["changed"] is True, r.get_json())
check("主身份已退出", occupants(B4) == [], occupants(B4))
st, j = check_flag("f-merge", "qa-b")
check("退出后别名再来问：桶有空位又占到（按新的算）",
      j["enabled"] is True, j)

print()
print(f"{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
