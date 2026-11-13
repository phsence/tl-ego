"""族评判器：跑一次真实验、按预先写死的判据 keep/kill。

判据是这个模块存在的全部意义，所以测的就是判据本身：
基线就活着的类型不算被救活、窗口没到不下结论、窗口内活够 = keep（族行留着）、
窗口内还死 = kill（参数回滚 + 删族行 + 写判词）。
全在临时目录里跑，POST 用假的，不碰生产 ego。
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

MOD = Path(__file__).resolve().parent / "l10_family_judge.py"


def load():
    spec = importlib.util.spec_from_file_location("judge_test", str(MOD))
    m = importlib.util.module_from_spec(spec)
    sys.modules["judge_test"] = m
    spec.loader.exec_module(m)
    return m


def seed(j, lae: Path, ticks: int, dead: list[str], alive_after: int | None = None,
         target: str = "social", start_tick: int = 1000) -> None:
    """写 observer 行：alive_after 之后 target 不再算死。"""
    (lae).mkdir(parents=True, exist_ok=True)
    with open(lae / "l10_observer.jsonl", "w", encoding="utf-8") as f:
        for i in range(ticks):
            d = list(dead)
            if alive_after is not None and i >= alive_after and target in d:
                d.remove(target)
            f.write(json.dumps({"tick": start_tick + i, "profile": {"dead_types": d}}) + "\n")


def seed_family(j, lae: Path, name="social_reentry", target="social",
                key="kwta_pool_mult", value=2.2) -> None:
    reg = lae / "l10_family_registry"
    reg.mkdir(parents=True, exist_ok=True)
    (reg / f"{name}.json").write_text(json.dumps(
        {"type": name, "when": {"dead_types_any": [target]}, "target": key,
         "value": value, "hypothesis": "h"}), encoding="utf-8")
    with open(lae / "l10_l5_proposals.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"tick": 1000, "status": "shadow_only",
                            "candidate": {"strategy": {"type": name,
                                                       "provenance": {"source": "family_registry",
                                                                      "registry_index": f"{name}.json"}}},
                            "shadow_param": {"key": key, "value": value}}) + "\n")


class FakePost:
    def __init__(self, status="ok"):
        self.calls, self.status = [], status

    def __call__(self, key, value, reason="", ttl=0.0):
        self.calls.append({"key": key, "value": value, "reason": reason, "ttl": ttl})
        return {"status": self.status, "old_value": 1.0}


def main() -> None:
    j = load()

    # 1. 没 observer → idle，不发任何写
    lae = Path(tempfile.mkdtemp())
    fp = FakePost()
    assert j.run(lae, fp)["action"] == "idle", "no observer should be idle"
    assert fp.calls == []

    # 2. 有族行 + 类型死着 → 开实验：真 POST 一次、带 TTL、写 state
    seed(j, lae, 60, ["social", "action"])
    seed_family(j, lae)
    r = j.run(lae, fp)
    assert r["action"] == "started", r
    assert fp.calls and fp.calls[0]["key"] == "kwta_pool_mult" and fp.calls[0]["ttl"] == j.TTL_S
    assert r["dead_type"] == "social" and r["old_value"] == 1.0
    st = json.loads((lae / "l10_family_judge_state.json").read_text(encoding="utf-8"))
    assert st["open"]["family"] == "social_reentry"

    # 3. 窗口没到 → running，不判、不重复写
    fp2 = FakePost()
    assert j.run(lae, fp2)["action"] == "running" and fp2.calls == []

    # 4. 窗口内它一直死 → kill：回滚到旧值 + 删族行 + 判词落盘
    seed(j, lae, 30, ["social"], alive_after=None)
    seed(j, lae, 60, ["social"], alive_after=None)          # 重新铺：全程社会都是死的
    st = json.loads((lae / "l10_family_judge_state.json").read_text(encoding="utf-8"))
    st["open"]["t0_tick"] = 1000                            # 让窗口直接过期
    (lae / "l10_family_judge_state.json").write_text(json.dumps(st), encoding="utf-8")
    r = j.run(lae, fp2)
    assert r["action"] == "kill" and r["alive_rows"] == 0, r
    assert fp2.calls and fp2.calls[-1]["value"] == 1.0, fp2.calls     # 回滚
    assert not (lae / "l10_family_registry" / "social_reentry.json").exists(), "dead family must be retired"
    verdicts = (lae / "l10_family_verdicts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(verdicts[-1])["verdict"] == "kill"

    # 5. 被淘汰的族不再重试
    assert j.run(lae, FakePost())["action"] == "idle"

    # 6. 救活 → keep，族行留着，不回滚
    lae2 = Path(tempfile.mkdtemp())
    seed(j, lae2, 60, ["social"], alive_after=None)
    seed_family(j, lae2, name="revive_me")
    fpa = FakePost()
    r = j.run(lae2, fpa)
    assert r["action"] == "started", r
    t0 = r["t0_tick"]
    # 窗口 (t0+6, t0+40]：把 1080 起 social 不再算死，落在窗口里
    seed(j, lae2, 60, ["social"], alive_after=1080 - t0, start_tick=t0)
    fpb = FakePost()
    r = j.run(lae2, fpb)
    assert r["action"] == "keep" and r["alive_rows"] >= j.MIN_ALIVE_ROWS, r
    assert fpb.calls == [], "keep must not revert the param"
    assert (lae2 / "l10_family_registry" / "revive_me.json").exists(), "kept family stays"

    # 7. 基线就活着的类型 → 不开实验（白送的赢不算赢）
    lae3 = Path(tempfile.mkdtemp())
    seed(j, lae3, 60, ["action"], alive_after=None)          # social 全程不算是死的
    seed_family(j, lae3)
    fpc = FakePost()
    r = j.run(lae3, fpc)
    assert r["action"] == "idle" and fpc.calls == [], r

    # 8. 参数不在白名单 → 不开实验（写了也是死写）
    lae4 = Path(tempfile.mkdtemp())
    seed(j, lae4, 60, ["social"], alive_after=None)
    seed_family(j, lae4, key="not_a_real_key")
    fpd = FakePost()
    assert j.run(lae4, fpd)["action"] == "idle" and fpd.calls == []

    print("OK: judge starts one TTL experiment, keeps only real revivals, kills (revert+retire) the rest")


if __name__ == "__main__":
    main()
