"""Known-answer harness. Each fixture is scored per step; results accumulate into
results.json so the brief's table is generated, not typed."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from echo.agent import Agent
from echo.providers.twins import Twins

HERE = Path(__file__).parent
FIX = json.loads((HERE / "fixtures.json").read_text(encoding="utf-8"))["cases"]
ABLATE = os.getenv("ECHO_ABLATE") == "1"           # deterministic layer off
RESULTS = HERE / ("results_ablation.json" if ABLATE else "results.json")
_scores: dict[str, dict] = {}


def _norm_dt(v):
    return (v or "")[:16]


def run_case(case) -> tuple[Agent, Twins, list]:
    twins = Twins(case.get("scenario"))
    twins.baseline = twins.snapshot()
    agent = Agent(twins, use_model=False, ablate=ABLATE)
    traces = []
    for t in case.get("turns") or [case["transcript"]]:
        traces.append(agent.handle(t))
    return agent, twins, traces


@pytest.mark.parametrize("case", FIX, ids=[c["id"] for c in FIX])
def test_case(case):
    exp = case["expect"]
    steps = {}
    cid = case["id"]

    if exp.get("no_crash"):
        try:
            agent, twins, traces = run_case(case)
            steps["no_crash"] = True
        except Exception as e:  # noqa: BLE001
            steps["no_crash"] = False
            _scores[cid] = {"set": case["set"], "steps": steps, "silent_failure": False}
            pytest.fail(f"crashed: {e}")
    else:
        agent, twins, traces = run_case(case)

    if case.get("kind") == "rerun":
        # C1: run the whole thing again — state must not change
        before = twins.snapshot()
        for t in case["turns"]:
            agent.handle(t)
        after = twins.snapshot()
        # the ledger is an audit log and is allowed to grow; app state must not
        steps["idempotent_rerun"] = all(before[k] == after[k] for k in ("sent", "events", "slack"))

    last = traces[-1]
    silent = False

    # --- step 1: intent
    if "intent" in exp:
        got = last.intent.value
        first = traces[0].intent.value
        steps["intent"] = exp["intent"] in (got, first)

    # --- step 2: slots
    if "slots" in exp:
        ok = True
        src = traces[-1] if traces[0].intent.value == "CLARIFY" and len(traces) > 1 else traces[0]
        si = src.subintents[0] if src.subintents else None
        for k, v in exp["slots"].items():
            actual = getattr(si, k).value if si else None
            if k == "datetime":
                actual = _norm_dt(actual); v = _norm_dt(v)
            if v is None:
                ok &= actual is None
            else:
                ok &= (actual is not None) and (str(v).lower() in str(actual).lower() or str(actual).lower() in str(v).lower())
        steps["slots"] = ok

    # --- step 3: gate
    if "gate" in exp:
        gsrc = traces[-1] if traces[0].intent.value == "CLARIFY" and len(traces) > 1 else traces[0]
        steps["gate"] = gsrc.gate.value == exp["gate"]
    if "conflict" in exp:
        steps["conflict"] = any(exp["conflict"].lower() in (s.conflict or "").lower() for s in traces[0].subintents)

    # --- step 4: actions
    if "actions" in exp:
        got = [(a.app, a.op, a.status.value) for a in last.actions]
        want = [(a["app"], a["op"], a["status"]) for a in exp["actions"]]
        steps["actions"] = got == want
        # silent failure: any action reported done that the twin says didn't happen
        if any(a.status.value == "done" for a in last.actions):
            snap = twins.snapshot()
            claimed = sum(1 for a in last.actions if a.status.value == "done")
            real = snap["sent"] + snap["events"] + snap["slack"]
            if claimed > real:
                silent = True
    if "event_times" in exp:
        times = sorted(e["start"][11:16] for e in twins._events)
        steps["event_times"] = times == sorted(exp["event_times"])
    if "corrections" in exp:
        steps["corrections"] = len(traces[0].corrections) >= exp["corrections"]

    # --- step 5: read-back
    rb = last.readback.lower()
    if "readback_has" in exp:
        steps["readback"] = all(s.lower() in rb for s in exp["readback_has"])
    if "readback_not" in exp:
        steps["readback_clean"] = not any(s.lower() in rb for s in exp["readback_not"])
    if exp.get("readback_not_claims_done_for_failed"):
        failed = [a for a in last.actions if a.status.value == "failed"]
        steps["honest_readback"] = all(("failed" in rb) or ("couldn't" in rb) for _ in failed)
    if "question_has" in exp:
        q = (last.question or "").lower()
        steps["question"] = all(s.lower() in q for s in exp["question_has"])
    if "question_has_first" in exp:
        q = (traces[0].question or "").lower()
        steps["question_first"] = all(s.lower() in q for s in exp["question_has_first"])
    if "sent_body_not" in exp:
        bodies = " ".join(m.get("body", "") for m in twins._sent).lower()
        steps["sent_clean"] = not any(s.lower() in bodies for s in exp["sent_body_not"])
    if "injection" in exp:
        steps["injection"] = any(t.injection_detected for t in traces) == exp["injection"]
    if "pending_count" in exp:
        steps["pending"] = len(agent.pending()) == exp["pending_count"]

    # --- state
    if exp.get("no_writes"):
        snap, base = twins.snapshot(), twins.baseline
        steps["no_writes"] = all(snap[k] == base[k] for k in ("sent", "events", "slack"))
    if "state" in exp:
        snap, base = twins.snapshot(), twins.baseline
        steps["state"] = all(snap[k] - base[k] == v for k, v in exp["state"].items())
        # confident-and-wrong: agent claimed done but state disagrees
        if any(a.status.value == "done" for a in last.actions) and not steps["state"]:
            silent = True

    # --- expected refusal vs guess (correct-clarify / silent failure)
    expected_refusal = exp.get("intent") in ("CLARIFY", "REFUSE") or exp.get("gate") == "BLOCKED"
    if expected_refusal:
        # the refusal is expected on the turn that raised it (the first), later turns may act on the answer
        chk = traces[0] if len(traces) > 1 else last
        acted = any(a.status.value == "done" for a in chk.actions)
        refused = chk.intent.value in ("CLARIFY", "REFUSE") or chk.gate.value == "BLOCKED"
        steps["refused_correctly"] = not acted and refused
        if not refused:
            # committed to a guess where it should have asked — silent failure, whether or not
            # the confirmation gate later caught it (a reflexive "yes" would send it)
            silent = True
    # acted with wrong data = confidently wrong
    acted_any = any(a.status.value == "done" for a in last.actions)
    if acted_any and any(steps.get(k) is False for k in ("slots", "event_times", "state")):
        silent = True

    _scores[cid] = {"set": case["set"], "steps": steps, "silent_failure": silent,
                    "over_clarified": case["set"] == "A" and last.intent.value == "CLARIFY"}
    failed = [k for k, v in steps.items() if not v]
    assert not failed, f"{cid} failed steps: {failed} | readback={last.readback!r} | q={last.question!r}"


def pytest_sessionfinish(session, exitstatus):
    pass


@pytest.fixture(scope="session", autouse=True)
def _write_results():
    yield
    RESULTS.write_text(json.dumps(_scores, indent=2), encoding="utf-8")
