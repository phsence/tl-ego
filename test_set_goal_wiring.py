"""Task 3 tests: set_goal real wiring — HTTP loopback to 8766 /directive + degrade."""
import json
import pathlib
import sys
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ego_internal_bridge as bridge  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(m, resp):
    captured = {}

    def fake_open(req, timeout=10):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return resp

    m.side_effect = fake_open
    return captured


def test_set_goal_queued_path():
    with mock.patch("urllib.request.urlopen") as m, \
         mock.patch.object(bridge, "_real_remember") as rm:
        cap = _capture(m, _FakeResp({
            "status": "queued", "goal_id": "l5g-20260829-120000-Cabc",
            "permission": "sandbox"}))
        r = bridge._real_set_goal("测试目标", priority="high", horizon="short")
        assert r["status"] == "ok"
        assert r["goal_id"] == "l5g-20260829-120000-Cabc"
        assert cap["url"] == "http://127.0.0.1:8766/directive"
        assert cap["body"]["kind"] == "directive"
        assert cap["body"]["priority"] == "high"
        assert rm.called  # commander goal also anchored in L4 memory


def test_set_goal_degraded_when_server_down():
    import urllib.error
    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError("conn refused")), \
         mock.patch.object(bridge, "_real_remember") as rm:
        r = bridge._real_set_goal("断网目标")
        assert r["status"] == "degraded"
        assert rm.called  # memory fallback must fire


def test_set_goal_rejects_empty():
    r = bridge._real_set_goal("   ")
    assert r["status"] == "error"


def test_set_goal_feedback_kind_passthrough():
    with mock.patch("urllib.request.urlopen") as m, \
         mock.patch.object(bridge, "_real_remember") as rm:
        cap = _capture(m, _FakeResp({
            "status": "queued", "goal_id": "l5g-20260829-120001-Cdef",
            "goal_type": "commander_feedback", "permission": "sandbox"}))
        r = bridge._real_set_goal("MC 房子建歪了", kind="feedback")
        assert r["status"] == "ok"
        assert cap["body"]["kind"] == "feedback"
        assert rm.called


if __name__ == "__main__":
    for fn in (test_set_goal_queued_path, test_set_goal_degraded_when_server_down,
               test_set_goal_rejects_empty, test_set_goal_feedback_kind_passthrough):
        fn()
        print(f"PASS {fn.__name__}")
    print("ALL PASS")
