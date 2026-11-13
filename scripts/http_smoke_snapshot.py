"""HTTP 冒烟：对运行中的 uvicorn 验证快照→重跑全链路（仅用标准库）。

运行：python3 -I scripts/http_smoke_snapshot.py
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
PASS = 0
FAIL = 0


def call(method: str, path: str, body: dict | None = None, expect_ok: bool = True) -> dict:
    global PASS, FAIL
    data = json.dumps({"values": body}).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = {"ok": False, "status": exc.code, "detail": exc.read().decode()}
    good = payload.get("ok", True) is expect_ok if "ok" in payload else expect_ok
    name = f"{method} {path}"
    if good:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} -> {payload}")
    return payload


def get(path: str) -> dict:
    global PASS
    with urllib.request.urlopen(BASE + path) as resp:
        payload = json.loads(resp.read().decode())
    PASS += 1
    print(f"  ✓ GET {path}")
    return payload


def main() -> int:
    global PASS
    print("HTTP 全链路冒烟")
    # 1. 创建（带存储校验失败注入）
    call("POST", "/api/snapshot", {
        "快照编号": "SNAP-HTTP-1", "表名称": "t_http", "快照时间": "2026-11-13 10:00:00",
        "保留天数": "33", "模拟存储校验结果": "失败"}, expect_ok=True)
    listed = get("/api/snapshot?keyword=SNAP-HTTP-1")
    row = listed["items"][0]
    assert row["保留天数"] == "33" and row["status"] == "草稿" and row["存储校验"] == "失败"
    # 2. 同编号幂等续跑（不带故障注入）
    call("POST", "/api/snapshot", {
        "快照编号": "SNAP-HTTP-1", "表名称": "t_http", "快照时间": "2026-11-13 10:00:00",
        "保留天数": "33"}, expect_ok=True)
    listed = get("/api/snapshot?keyword=SNAP-HTTP-1")
    assert len(listed["items"]) == 1 and listed["items"][0]["存储校验"] == "通过"
    sid = listed["items"][0]["id"]
    # 3. 状态机推进
    call("POST", f"/api/snapshot/{sid}/actions", {"action": "开始校验"})
    call("POST", f"/api/snapshot/{sid}/actions", {"action": "校验通过", "校验结论": "HTTP 冒烟通过"})
    detail = get(f"/api/snapshot/{sid}")
    assert detail["status"] == "已完成" and detail["可回滚"] is True
    # 4. 重跑引用
    call("POST", "/api/backfill", {"批次编号": "BF-HTTP-1", "目标表名": "t_http", "开始日期": "2026-11-13"})
    bf = get("/api/backfill?keyword=BF-HTTP-1")
    bid = bf["items"][0]["id"]
    call("POST", f"/api/backfill/{bid}/actions", {"action": "引用快照", "快照编号": "SNAP-HTTP-1"})
    bf_detail = get(f"/api/backfill/{bid}")
    assert bf_detail["引用快照状态"] == "已完成" and bf_detail["可回滚"] is True
    assert bf_detail["回滚依据校验编号"] == detail["校验编号"]
    # 5. 启动重跑后清理被拒
    call("POST", f"/api/backfill/{bid}/actions", {"action": "启动重跑"})
    call("POST", f"/api/snapshot/{sid}/actions", {"action": "清理快照"}, expect_ok=False)
    # 6. 暂停后可清理，重跑页实时变不可回滚
    call("POST", f"/api/backfill/{bid}/actions", {"action": "暂停重跑"})
    call("POST", f"/api/snapshot/{sid}/actions", {"action": "清理快照"})
    bf_detail = get(f"/api/backfill/{bid}")
    assert bf_detail["引用快照状态"] == "已清理" and bf_detail["可回滚"] is False
    # 7. 已清理不能再被引用
    call("POST", "/api/backfill", {"批次编号": "BF-HTTP-2", "目标表名": "t_http", "开始日期": "2026-11-13"})
    bf2 = get("/api/backfill?keyword=BF-HTTP-2")
    call("POST", f"/api/backfill/{bf2['items'][0]['id']}/actions",
         {"action": "引用快照", "快照编号": "SNAP-HTTP-1"}, expect_ok=False)
    # 8. 恢复校验结果页
    rec = get("/api/snapshot/recovery?keyword=SNAP-HTTP-1")
    assert rec["total"] == 1 and rec["items"][0]["校验结果"] == "通过"
    # 9. 404
    try:
        urllib.request.urlopen(BASE + "/api/snapshot/99999")
        raise AssertionError("应当 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
        PASS += 1
        print("  ✓ GET /api/snapshot/99999 -> 404")

    print(f"\nHTTP 冒烟结果：通过 {PASS} 条，失败 {FAIL} 条")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
