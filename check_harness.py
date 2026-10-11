#!/usr/bin/env python3
"""check_harness.py - verifies that the multi-agent relay harness works.
Run:  python check_harness.py --dry-run   (no harness needed)
      python check_harness.py             (real harness commands)
Env:  HARNESS_BIN, HARNESS_MSG_CMD, HARNESS_LIMIT_CMD, HARNESS_DRY_RUN=1
Exit code: 0 = all checks passed, 1 = a check failed.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HARNESS_BIN = os.environ.get("HARNESS_BIN", "harness")
MSG_CMD = os.environ.get("HARNESS_MSG_CMD", "ai message").split()
LIMIT_CMD = os.environ.get("HARNESS_LIMIT_CMD", "limit reach").split()
DRY_RUN = os.environ.get("HARNESS_DRY_RUN") == "1"

REQUIRED_FIELDS = [
    "TASK_GOAL", "STATUS", "CURRENT_AGENT", "COMPLETED_STEPS",
    "NEXT_STEP", "OPEN_ISSUES", "FILES_TOUCHED", "LAST_UPDATED",
]
VALID_STATUS = {"not_started", "in_progress", "blocked", "done"}
CONTEXT_LIMIT_RATIO = 0.85


class SkipCheck(Exception):
    """Raised when a check cannot run in this environment."""


def _skip(reason):
    if "PYTEST_CURRENT_TEST" in os.environ:
        import pytest
        pytest.skip(reason)
    raise SkipCheck(reason)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def harness_available():
    return DRY_RUN or shutil.which(HARNESS_BIN) is not None


def call_harness(sub_cmd, message):
    cmd = [HARNESS_BIN, *sub_cmd, message]
    if DRY_RUN:
        print(f"    [dry-run] {' '.join(cmd)}")
        return 0, "dry-run"
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return 124, "timeout after 30s"
    return res.returncode, (res.stdout + res.stderr).strip()


def notify_message(text):
    return call_harness(MSG_CMD, text)


def notify_limit(text):
    return call_harness(LIMIT_CMD, text)


def write_handoff(path, goal, status, agent, completed, next_step,
                  issues="none", files="none"):
    steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(completed, 1)) or "  (none)"
    path.write_text(
        f"- TASK_GOAL: {goal}\n"
        f"- STATUS: {status}\n"
        f"- CURRENT_AGENT: {agent}\n"
        f"- COMPLETED_STEPS:\n{steps}\n"
        f"- NEXT_STEP: {next_step}\n"
        f"- OPEN_ISSUES: {issues}\n"
        f"- FILES_TOUCHED: {files}\n"
        f"- LAST_UPDATED: {now()} by {agent}\n",
        encoding="utf-8",
    )


def parse_handoff(path):
    if not path.exists() or path.stat().st_size == 0:
        raise ValueError("HANDOFF.md missing or empty")
    text = path.read_text(encoding="utf-8")
    fields = {}
    for name in REQUIRED_FIELDS:
        m = re.search(rf"^- {name}:(.*?)(?=^- [A-Z_]+:|\Z)", text, re.M | re.S)
        if not m:
            raise ValueError(f"missing field: {name}")
        fields[name] = m.group(1).strip()
    if fields["STATUS"] not in VALID_STATUS:
        raise ValueError(f"invalid STATUS: {fields['STATUS']}")
    fields["STEP_COUNT"] = len(re.findall(r"^\s*\d+\.", fields["COMPLETED_STEPS"], re.M))
    return fields


LIMIT_PATTERNS = [
    r"\b429\b", r"rate[\s_-]?limit", r"quota", r"usage[\s_-]?limit",
    r"too many requests", r"limit (reached|exceeded)",
]


def is_limit_error(text="", context_used=0, context_total=0):
    low = text.lower()
    if any(re.search(p, low) for p in LIMIT_PATTERNS):
        return True
    if context_total and context_used / context_total >= CONTEXT_LIMIT_RATIO:
        return True
    return False


class FakeAgent:
    def __init__(self, name, budget):
        self.name = name
        self.budget = budget

    def do_step(self, step):
        if self.budget <= 0:
            raise RuntimeError("429 usage limit reached")
        self.budget -= 1
        return f"{step} done by {self.name}"


def run_relay(workdir, steps, agents, notifier_msg, notifier_limit):
    handoff = workdir / "HANDOFF.md"
    goal = "relay self-test"
    completed = []
    limit_events = []

    write_handoff(handoff, goal, "not_started", agents[0].name, [], steps[0])

    for agent in agents:
        state = parse_handoff(handoff)
        done_count = state["STEP_COUNT"]
        if done_count >= len(steps):
            break

        label = "STARTED" if not completed else "RESUMED"
        code, out = notifier_msg(f"{label} | Agent: {agent.name} | From step: {done_count + 1}")
        assert code == 0, f"{label} notification failed: {out}"

        for idx in range(done_count, len(steps)):
            try:
                completed.append(agent.do_step(steps[idx]))
            except RuntimeError as err:
                assert is_limit_error(str(err)), "limit error not detected"
                nxt = steps[idx]
                write_handoff(handoff, goal, "in_progress", agent.name,
                              [s.split(" done")[0] for s in completed], nxt)
                code, out = notifier_limit(
                    f"LIMIT_REACHED | Agent: {agent.name} | Next: {nxt}")
                assert code == 0, f"limit notification failed: {out}"
                limit_events.append(agent.name)
                break
            write_handoff(
                handoff, goal, "in_progress", agent.name,
                [s.split(" done")[0] for s in completed],
                steps[idx + 1] if idx + 1 < len(steps) else "none",
            )
        else:
            write_handoff(handoff, goal, "done", agent.name,
                          [s.split(" done")[0] for s in completed], "none")
            notifier_msg(f"DONE | Agent: {agent.name}")
            break

    return completed, limit_events


def test_01_harness_installed():
    if not harness_available():
        _skip(f"'{HARNESS_BIN}' not found on PATH (use --dry-run to skip)")


def test_02_ai_message_notification():
    if not harness_available():
        _skip("harness not installed")
    code, out = notify_message(f"TEST | harness ai message check | {now()}")
    assert code == 0, f"`harness {' '.join(MSG_CMD)}` failed (exit {code}): {out}"


def test_03_limit_reach_notification():
    if not harness_available():
        _skip("harness not installed")
    code, out = notify_limit(f"TEST | harness limit reach check | {now()}")
    assert code == 0, f"`harness {' '.join(LIMIT_CMD)}` failed (exit {code}): {out}"


def test_04_handoff_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "HANDOFF.md"
        write_handoff(p, "demo", "in_progress", "Agent A", ["step one", "step two"], "step three")
        f = parse_handoff(p)
        assert f["CURRENT_AGENT"] == "Agent A"
        assert f["STEP_COUNT"] == 2
        assert f["NEXT_STEP"] == "step three"


def test_05_handoff_rejects_bad_file():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "HANDOFF.md"
        p.write_text("- TASK_GOAL: x\n", encoding="utf-8")
        try:
            parse_handoff(p)
        except ValueError:
            return
        raise AssertionError("incomplete HANDOFF.md was accepted")


def test_06_limit_detection():
    assert is_limit_error("HTTP 429 Too Many Requests")
    assert is_limit_error("You have exceeded your usage limit")
    assert is_limit_error("quota exceeded")
    assert is_limit_error("", context_used=86, context_total=100)
    assert not is_limit_error("syntax error on line 4")
    assert not is_limit_error("", context_used=50, context_total=100)


def test_07_relay_agent_a_limit_agent_b_finishes():
    if not harness_available():
        _skip("harness not installed")
    steps = [f"step {i}" for i in range(1, 7)]
    with tempfile.TemporaryDirectory() as d:
        workdir = Path(d)
        agents = [FakeAgent("Agent A", 3), FakeAgent("Agent B", 10)]
        completed, limits = run_relay(workdir, steps, agents, notify_message, notify_limit)
        assert len(completed) == len(steps), f"expected {len(steps)} steps, got {len(completed)}"
        names = [c.split(" done")[0] for c in completed]
        assert names == steps, "steps skipped, repeated or out of order"
        assert completed[0].endswith("Agent A") and completed[-1].endswith("Agent B")
        assert limits == ["Agent A"], f"unexpected limit events: {limits}"
        assert parse_handoff(workdir / "HANDOFF.md")["STATUS"] == "done"


def test_08_relay_three_agents():
    if not harness_available():
        _skip("harness not installed")
    steps = [f"step {i}" for i in range(1, 8)]
    with tempfile.TemporaryDirectory() as d:
        agents = [FakeAgent("A", 2), FakeAgent("B", 2), FakeAgent("C", 10)]
        completed, limits = run_relay(Path(d), steps, agents, notify_message, notify_limit)
        assert len(completed) == 7
        assert limits == ["A", "B"]


def test_09_handoff_committed_to_git():
    if shutil.which("git") is None:
        _skip("git not installed")
    with tempfile.TemporaryDirectory() as d:
        wd = Path(d)
        run = lambda *a: subprocess.run(a, cwd=wd, capture_output=True, text=True)
        run("git", "init", "-q")
        run("git", "config", "user.email", "harness@test.local")
        run("git", "config", "user.name", "harness-test")
        write_handoff(wd / "HANDOFF.md", "demo", "in_progress", "Agent A", ["s1"], "s2")
        run("git", "add", "HANDOFF.md")
        res = run("git", "commit", "-q", "-m", "step 1: handoff")
        assert res.returncode == 0, res.stderr
        assert "step 1" in run("git", "log", "--oneline").stdout


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser(description="Check that the multi-agent harness works.")
    ap.add_argument("--dry-run", action="store_true", help="do not call the real harness CLI")
    args = ap.parse_args()
    if args.dry_run:
        DRY_RUN = True

    checks = [(n, f) for n, f in sorted(globals().items())
              if n.startswith("test_") and callable(f)]
    print(f"Harness check | {now()} | dry-run={DRY_RUN}\n")

    passed = failed = skipped = 0
    for name, fn in checks:
        label = name[5:].replace("_", " ")
        try:
            fn()
            print(f"  PASS  {label}")
            passed += 1
        except SkipCheck as e:
            print(f"  SKIP  {label}  ({e})")
            skipped += 1
        except Exception as e:
            print(f"  FAIL  {label}\n        {type(e).__name__}: {e}")
            failed += 1

    print(f"\nResult: {passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())