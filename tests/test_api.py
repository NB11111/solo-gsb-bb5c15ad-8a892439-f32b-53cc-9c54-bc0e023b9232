"""端到端 API 测试：上报去重、迟到重放、锚点校准、预览不写库。"""
from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


def _points(start_idx, count, temp, hum, step_minutes=5):
    return [
        {
            "ts": (BASE + timedelta(minutes=step_minutes * (start_idx + i)))
            .isoformat()
            .replace("+00:00", "Z"),
            "temperature": temp,
            "humidity": hum,
        }
        for i in range(count)
    ]


def _ingest(client, serial, points):
    body = {"readings": [{**p, "device_serial": serial} for p in points]}
    r = client.post("/api/v1/readings", json=body)
    assert r.status_code == 200, r.text
    return r.json()["devices"][0]


def _segments(client, serial):
    r = client.get(f"/api/v1/devices/{serial}/segments")
    assert r.status_code == 200, r.text
    return r.json()["segments"]


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_openapi_served(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    for p in (
        "/api/v1/readings",
        "/api/v1/devices/{serial}/segments",
        "/api/v1/devices/{serial}/anchors",
        "/api/v1/replay-preview",
    ):
        assert p in paths


def test_ingest_and_segment_two_regimes(client):
    pts = _points(0, 10, 20.0, 50.0) + _points(10, 10, 26.0, 62.0)
    summary = _ingest(client, "CASE-01", pts)
    assert summary["inserted"] == 20
    assert summary["late"] is False
    assert summary["replay"]["segments_produced"] == 2

    segs = _segments(client, "CASE-01")
    assert len(segs) == 2
    assert segs[0]["n"] == 10 and segs[1]["n"] == 10
    assert segs[0]["mean_temperature"] == 20.0
    assert segs[1]["mean_temperature"] == 26.0
    assert segs[0]["delta_temperature"] is None
    assert segs[1]["delta_temperature"] == 6.0
    assert segs[1]["delta_humidity"] == 12.0
    assert all(s["confidence"] == "high" for s in segs)


def test_duplicate_reports_merged_idempotent(client):
    pts = _points(0, 8, 21.0, 51.0)
    first = _ingest(client, "CASE-02", pts)
    assert first["inserted"] == 8

    # 原样重报：全部 unchanged，不触发重放
    again = _ingest(client, "CASE-02", pts)
    assert again["unchanged"] == 8
    assert again["inserted"] == 0 and again["merged"] == 0
    assert again["replay"] is None

    # 同时间戳不同数值：合并覆盖
    changed = [dict(p, temperature=p["temperature"] + 1.0) for p in pts]
    third = _ingest(client, "CASE-02", changed)
    assert third["merged"] == 8 and third["inserted"] == 0

    r = client.get("/api/v1/devices/CASE-02/readings")
    body = r.json()
    assert body["count"] == 8  # 仍是 8 行
    assert all(row["merged_count"] == 2 for row in body["readings"])
    assert all(row["temperature"] == 22.0 for row in body["readings"])


def test_duplicates_within_single_batch(client):
    pts = _points(0, 5, 20.0, 50.0)
    dup = pts + [dict(pts[-1], temperature=99.0)]  # 批次内同刻重复，后者生效
    summary = _ingest(client, "CASE-03", dup)
    assert summary["received"] == 6
    assert summary["duplicates_in_batch"] == 1
    assert summary["inserted"] == 5
    r = client.get("/api/v1/devices/CASE-03/readings").json()
    assert r["readings"][-1]["temperature"] == 99.0


def test_late_reading_triggers_replay(client):
    # 先上报两段稳定环境
    pts = _points(0, 10, 20.0, 50.0) + _points(10, 10, 24.0, 58.0)
    _ingest(client, "CASE-04", pts)
    assert len(_segments(client, "CASE-04")) == 2

    # 迟到读数：在中间补入一段持续高湿（形成新段）
    late_pts = _points(4, 6, 30.0, 80.0)
    summary = _ingest(client, "CASE-04", late_pts)
    assert summary["late"] is True
    assert summary["merged"] == 6
    assert summary["replay"]["trigger"] == "ingest"

    segs = _segments(client, "CASE-04")
    assert len(segs) == 3
    assert segs[1]["mean_temperature"] == 30.0
    assert segs[1]["mean_humidity"] == 80.0

    # 重放审计可查
    runs = client.get("/api/v1/devices/CASE-04/replays").json()
    assert len(runs) == 2
    assert runs[0]["late"] is True


def test_anchor_forces_boundary(client):
    # 完全恒定的环境：切分零收益，默认参数下保持单段
    pts = _points(0, 12, 20.0, 50.0)
    _ingest(client, "CASE-05", pts)
    assert len(_segments(client, "CASE-05")) == 1

    # 在中间加校准锚点 → 强制切成两段
    mid_ts = pts[6]["ts"]
    r = client.post(
        "/api/v1/devices/CASE-05/anchors", json={"ts": mid_ts, "note": "开柜更换展品"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["replay"]["trigger"] == "anchor_add"

    segs = _segments(client, "CASE-05")
    assert len(segs) == 2
    assert segs[1]["start_ts"].startswith(mid_ts[:16])

    # 重复提交同刻锚点：幂等，不再重放
    r2 = client.post("/api/v1/devices/CASE-05/anchors", json={"ts": mid_ts, "note": "更新备注"})
    assert r2.status_code == 201
    assert r2.json()["replay"] is None
    assert r2.json()["anchor"]["note"] == "更新备注"

    # 删除锚点 → 恢复单段
    anchor_id = r2.json()["anchor"]["id"]
    r3 = client.delete(f"/api/v1/devices/CASE-05/anchors/{anchor_id}")
    assert r3.status_code == 200
    assert r3.json()["replay"]["trigger"] == "anchor_delete"
    assert len(_segments(client, "CASE-05")) == 1


def test_replay_preview_never_writes(client):
    pts = _points(0, 10, 20.0, 50.0) + _points(10, 10, 24.0, 58.0)
    _ingest(client, "CASE-06", pts)
    before_segs = _segments(client, "CASE-06")
    before_readings = client.get("/api/v1/devices/CASE-06/readings").json()["count"]
    before_runs = len(client.get("/api/v1/devices/CASE-06/replays").json())

    # 预览：假设中间补入一段异常
    hypo = _points(4, 6, 35.0, 90.0)
    r = client.post(
        "/api/v1/replay-preview",
        json={"device_serial": "CASE-06", "readings": hypo},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["persisted"] is False
    assert body["would_change"] is True
    assert body["current_count"] == 2
    assert body["preview_count"] == 3

    # 预览后库内一切不变
    assert _segments(client, "CASE-06") == before_segs
    assert client.get("/api/v1/devices/CASE-06/readings").json()["count"] == before_readings
    assert len(client.get("/api/v1/devices/CASE-06/replays").json()) == before_runs


def test_preview_with_parameter_override_and_unknown_device(client):
    pts = _points(0, 10, 20.0, 50.0) + _points(10, 10, 20.4, 50.3)
    _ingest(client, "CASE-07", pts)
    # 极小惩罚项 + 极小段长：试算应切出更多段
    r = client.post(
        "/api/v1/replay-preview",
        json={"device_serial": "CASE-07", "readings": [], "penalty": 0.5, "min_size": 2},
    )
    assert r.status_code == 200
    assert r.json()["params"] == {"penalty": 0.5, "min_size": 2}

    # 未注册设备也可预览（基于空时间线）
    r2 = client.post(
        "/api/v1/replay-preview",
        json={"device_serial": "CASE-NEW", "readings": _points(0, 6, 19.0, 45.0)},
    )
    assert r2.status_code == 200
    assert r2.json()["current_count"] == 0
    assert r2.json()["preview_count"] == 1
    # 且未创建任何设备
    assert client.get("/api/v1/devices/CASE-NEW/segments").status_code == 404


def test_determinism_across_ingest_orders(client):
    pts = _points(0, 10, 20.0, 50.0) + _points(10, 10, 26.0, 62.0)
    _ingest(client, "CASE-08A", pts)
    # 另一台设备：同一批数据分两次、乱序上报
    first_half, second_half = pts[:10], pts[10:]
    _ingest(client, "CASE-08B", list(reversed(second_half)))
    _ingest(client, "CASE-08B", list(reversed(first_half)))
    assert _segments(client, "CASE-08A") == _segments(client, "CASE-08B")


def test_unknown_device_404(client):
    assert client.get("/api/v1/devices/NOPE/segments").status_code == 404
    assert client.get("/api/v1/devices/NOPE/readings").status_code == 404
    assert client.get("/api/v1/devices/NOPE/anchors").status_code == 404


def test_timezone_offsets_normalized(client):
    pts = _points(0, 6, 20.0, 50.0)
    # 同一时刻，+08:00 写法与 Z 写法应合并为一条
    p0 = dict(pts[0])
    p0["ts"] = p0["ts"].replace("Z", "+00:00")
    shifted = datetime.fromisoformat(pts[0]["ts"].replace("Z", "+00:00"))
    p_dup = dict(pts[0])
    p_dup["ts"] = shifted.astimezone(timezone(timedelta(hours=8))).isoformat()
    summary = _ingest(client, "CASE-09", [p0, p_dup] + pts[1:])
    assert summary["inserted"] == 6
    assert summary["duplicates_in_batch"] == 1
