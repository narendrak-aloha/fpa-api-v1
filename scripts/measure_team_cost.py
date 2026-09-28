"""Team against a single agent: model calls, tokens and latency on the same questions.

Both run through the same ``FPAOrchestrator`` (same guardrails, masking,
disclosure log, bounded repair, arithmetic check) with the same tools and
instructions; the only difference is ``build_agno_team(single=True)``. Each
question is asked of each mode in turn, so a provider slowdown hits both.

Tokens are counted per model call at the provider boundary. For the
``claude-code`` provider that is the Claude Agent SDK's own usage figure,
including prompt-cache reads and writes, which Agno's run metrics do not see.
For the API providers it is Agno's run metrics, leader plus members.

Needs the stack (ClickHouse for the query tool, Postgres for the disclosure log)
and a configured provider.

    uv run python scripts/measure_team_cost.py                       # 3 questions x 1 repeat
    uv run python scripts/measure_team_cost.py --repeats 3 --out docs/team_cost.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from fpa_project.identities import CFO

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

QUESTIONS = [
    "Why did Poland miss its services revenue plan in Q2 2026? By practice.",
    "What was services revenue by practice in Q2 2026?",
    "Delivery cost in Poland for Q2 2026 as of the July close (2026-07-05T18:00:00)",
]

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


class _Recorder:
    """Stands in for a team or agent and keeps every run output it returns."""

    def __init__(self, inner, outputs: list, members: list | None = None):
        self._inner = inner
        self._outputs = outputs
        self.members = members if members is not None else []

    def run(self, *args, **kwargs):
        output = self._inner.run(*args, **kwargs)
        self._outputs.append(output)
        return output

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _count_sdk_usage(calls: list[dict]) -> None:
    """Record the SDK's usage for every Claude Code call, cache tokens included."""
    from fpa_project.agent_team import claude_code_model

    original = claude_code_model.ClaudeCodeModel._acall

    async def counted(self, system, prompt):
        data = await original(self, system, prompt)
        calls.append(dict(data.get("_usage") or {}))
        return data

    claude_code_model.ClaudeCodeModel._acall = counted


def _metrics_usage(outputs: list) -> tuple[int, dict[str, int]]:
    """Model calls and tokens from Agno's run metrics (API providers)."""
    totals = dict.fromkeys(USAGE_KEYS, 0)
    calls = 0
    runs = []
    for output in outputs:
        runs.append(output)
        runs.extend(getattr(output, "member_responses", None) or [])
    for run in runs:
        metrics = getattr(run, "metrics", None)
        if metrics is None:
            continue
        totals["input_tokens"] += int(getattr(metrics, "input_tokens", 0) or 0)
        totals["output_tokens"] += int(getattr(metrics, "output_tokens", 0) or 0)
        totals["cache_read_input_tokens"] += int(getattr(metrics, "cache_read_tokens", 0) or 0)
        totals["cache_creation_input_tokens"] += int(getattr(metrics, "cache_write_tokens", 0) or 0)
        calls += len([m for m in getattr(run, "messages", None) or [] if getattr(m, "role", "") == "assistant"])
    return calls, totals


def run_once(mode: str, question: str, provider: str, scope, sdk_calls: list[dict]) -> dict:
    from app import build_model, scoped_tools
    from fpa_project.agent_team import FPAOrchestrator, PlanningRequest, build_agno_team

    tools = scoped_tools(scope)
    built = build_agno_team(model=build_model(provider), toolset=tools, single=(mode == "single"))
    outputs: list = []
    # The orchestrator sends its one repair to members[0]; a lone agent repairs itself.
    first = built.members[0] if mode == "team" else built
    members = [_Recorder(first, outputs)] + (list(built.members[1:]) if mode == "team" else [])
    runner = _Recorder(built, outputs, members)

    before = len(sdk_calls)
    started = time.monotonic()
    result = FPAOrchestrator(tools).run_with_team(runner, PlanningRequest(request=question))
    seconds = time.monotonic() - started

    if provider == "claude-code":
        mine = sdk_calls[before:]
        calls = len(mine)
        tokens = {key: sum(int(call.get(key) or 0) for call in mine) for key in USAGE_KEYS}
    else:
        calls, tokens = _metrics_usage(outputs)
    return {
        "mode": mode,
        "question": question,
        "status": result.execution_status,
        "dsl": result.generated_dsl,
        "produced_by": result.produced_by,
        "attempts": len(outputs),
        "seconds": round(seconds, 2),
        "model_calls": calls,
        **tokens,
        "total_tokens": sum(tokens.values()),
    }


def summarise(runs: list[dict]) -> list[dict]:
    rows = []
    for mode in ("single", "team"):
        mine = [r for r in runs if r["mode"] == mode]
        if not mine:
            continue
        rows.append({
            "mode": mode,
            "runs": len(mine),
            "success": sum(r["status"] == "SUCCESS" for r in mine),
            "median_seconds": round(statistics.median(r["seconds"] for r in mine), 1),
            "median_model_calls": statistics.median(r["model_calls"] for r in mine),
            **{f"median_{k}": statistics.median(r[k] for r in mine) for k in (*USAGE_KEYS, "total_tokens")},
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--provider", default="claude-code", choices=["claude-code", "claude-api", "gemini"])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--question", action="append", help="ask this instead of the defaults (repeatable)")
    parser.add_argument("--user", default=CFO, help="whose scope the tools carry")
    parser.add_argument("--out", default=str(ROOT / "docs" / "team_cost.json"))
    args = parser.parse_args()

    from app import ALL_COMPANIES
    from fpa_project.agent_team import UserScope

    scope = UserScope(user_id=args.user, allowed_companies=ALL_COMPANIES, max_estimated_rows=1_000_000)
    sdk_calls: list[dict] = []
    if args.provider == "claude-code":
        _count_sdk_usage(sdk_calls)

    runs = []
    for repeat in range(args.repeats):
        for question in args.question or QUESTIONS:
            for mode in ("single", "team"):
                print(f"[{repeat + 1}/{args.repeats}] {mode:6} {question[:60]}", flush=True)
                try:
                    run = run_once(mode, question, args.provider, scope, sdk_calls)
                except Exception as exc:  # noqa: BLE001 - one failed run is a data point, not the end
                    run = {"mode": mode, "question": question, "status": f"ERROR: {type(exc).__name__}: {exc}",
                           "seconds": 0, "model_calls": 0, "total_tokens": 0, **dict.fromkeys(USAGE_KEYS, 0)}
                print(f"    -> {run['status']} in {run['seconds']}s, {run['model_calls']} calls, "
                      f"{run['total_tokens']} tokens", flush=True)
                runs.append(run)

    summary = summarise(runs)
    report = {
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": args.provider,
        "repeats": args.repeats,
        "summary": summary,
        "runs": runs,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\nwrote {args.out}\n")
    print("| mode | runs | success | median s | median calls | median input | median output | median cache read | median cache write | median total |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for row in summary:
        print(f"| {row['mode']} | {row['runs']} | {row['success']} | {row['median_seconds']} | {row['median_model_calls']} "
              f"| {row['median_input_tokens']} | {row['median_output_tokens']} | {row['median_cache_read_input_tokens']} "
              f"| {row['median_cache_creation_input_tokens']} | {row['median_total_tokens']} |")


if __name__ == "__main__":
    main()
