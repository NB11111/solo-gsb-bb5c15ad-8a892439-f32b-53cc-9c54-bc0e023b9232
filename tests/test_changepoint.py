"""PELT 分段核心算法的单元测试。"""
from app.changepoint import pelt_segment


def _regime(n, t, h):
    return [(t, h)] * n


def _unpack(points):
    return [p[0] for p in points], [p[1] for p in points]


def test_empty_input():
    assert pelt_segment([], [], penalty=12.0, min_size=3) == []


def test_short_input_single_segment():
    t, h = _unpack(_regime(4, 20.0, 50.0))
    assert pelt_segment(t, h, penalty=12.0, min_size=3) == [(0, 4)]


def test_detects_single_step():
    points = _regime(10, 20.0, 50.0) + _regime(10, 26.0, 62.0)
    t, h = _unpack(points)
    assert pelt_segment(t, h, penalty=12.0, min_size=3) == [(0, 10), (10, 20)]


def test_detects_multiple_steps():
    points = _regime(8, 20.0, 50.0) + _regime(8, 24.0, 55.0) + _regime(8, 19.0, 48.0)
    t, h = _unpack(points)
    assert pelt_segment(t, h, penalty=12.0, min_size=3) == [(0, 8), (8, 16), (16, 24)]


def test_penalty_suppresses_split():
    # 微小漂移：大惩罚项下不应切分
    points = _regime(10, 20.0, 50.0) + _regime(10, 20.3, 50.2)
    t, h = _unpack(points)
    assert pelt_segment(t, h, penalty=200.0, min_size=3) == [(0, 20)]


def test_min_size_respected():
    points = _regime(6, 20.0, 50.0) + _regime(6, 28.0, 65.0)
    t, h = _unpack(points)
    segs = pelt_segment(t, h, penalty=1.0, min_size=4)
    assert all(b - a >= 4 for a, b in segs)


def test_deterministic_same_input_same_output():
    # 伪随机但固定的扰动
    points = []
    for i in range(60):
        base_t = 20.0 if i < 30 else 25.0
        base_h = 50.0 if i < 30 else 60.0
        jitter = ((i * 7919) % 13) / 10.0
        points.append((base_t + jitter, base_h - jitter))
    t, h = _unpack(points)
    first = pelt_segment(t, h, penalty=12.0, min_size=3)
    for _ in range(5):
        assert pelt_segment(t, h, penalty=12.0, min_size=3) == first
    assert first == [(0, 30), (30, 60)]
