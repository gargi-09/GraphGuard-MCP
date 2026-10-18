"""Run the routing scenarios through the whole FoGuard pipeline, with the reasoning shown.

    python scenario_pipeline.py                 # all five scenarios
    python scenario_pipeline.py --scenario 2    # just one (repeatable)
    python scenario_pipeline.py --no-agent      # skip the Claude step (free)
    python scenario_pipeline.py --pause 6       # wait between scenarios (screen recordings)

The scenarios come from tests/test_routing.py. For each one:
  1. Incident: its corridors are blocked on the LIVE graph (a dashboard in
     Live edge mode follows along).
  2. Verifier reasoning: every route that physically exists, each run through
     the verify_route guardrail, with its distance or exact rejection reason.
  3. Claude: the recovery agent reads the graph, proposes routes until one is
     approved (its summarized reasoning is shown), and drafts the notices.
  4. Fo: the paste-ready Claude Web prompt (reroute, or hold-and-escalate when
     no route exists), with contacts from .env.
A scenario with no blocked corridor is a routine check: no incident fires.

Transcripts are saved to docs/scenario_runs.json for the dashboard to replay
(without contacts). The live graph is reset at the end.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from graph import tools
from recovery_agent import MODEL, build_fo_prompt, run_recovery
from tests.test_routing import SCENARIOS

load_dotenv()

ROOT = Path(__file__).resolve().parent
RUNS_FILE = ROOT / "docs" / "scenario_runs.json"
RULE = "=" * 76


def section(title: str):
    print(f"\n  -- {title} " + "-" * max(0, 68 - len(title)))


def route_text(path, distance) -> str:
    return f"{' -> '.join(path)} ({distance:g} m)"


def run_scenario(s: dict, use_agent: bool) -> dict:
    print(f"\n{RULE}\n  SCENARIO {s['id']}: {s['name'].upper()}\n  {s['description']}\n{RULE}")
    blocked = s["blocked"]

    # 1. Incident on the live graph
    section("1. Incident on the live graph")
    tools.reset_graph()
    for a, b in blocked:
        tools.set_corridor_blocked(a, b, True)
    incident = None
    if blocked:
        incident = {
            "event": "OBSTRUCTION_DETECTED",
            "scenario": s["name"],
            "blocked_corridors": blocked,
            "blocked_corridor": blocked[0],
            "origin": s["origin"],
            "destination": s["destination"],
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        print(f"  Blocked: {', '.join(a + ' <-> ' + b for a, b in blocked)}  |  delivery {s['origin']} -> {s['destination']}")
        print(f"  {json.dumps(incident)}")
    else:
        print(f"  No corridor blocked: routine check of {s['origin']} -> {s['destination']}, no incident raised.")

    # 2. The verifier's reasoning
    section("2. Verifier reasoning: every route on the map, checked")
    options = tools.route_options(s["origin"], s["destination"])
    for o in options["options"]:
        verdict = f"OK    {o['total_distance']:>5g} m" if o["valid"] else f"NO    {o['reason']}"
        print(f"  {'-'.join(o['path']):<14} {verdict}")
    chosen = options["chosen"]
    expect = s["expect"]
    if chosen["valid"]:
        print(f"  Chosen: {route_text(chosen['path'], chosen['total_distance'])}, the shortest route that passes")
        matches = expect["valid"] and chosen["path"] == expect["path"]
    else:
        print(f"  Chosen: none. {chosen['reason']}")
        matches = not expect["valid"]
    print(f"  Matches the expected result in tests/test_routing.py: {'YES' if matches else 'NO'}")

    run = {"scenario": s, "incident": incident, "route_options": options, "matches_expected": matches,
           "agent": None, "agent_skipped": None}

    if not incident:
        print("\n  Nothing to recover: no Claude run and no notices for a routine check.")
        run["agent_skipped"] = "no incident"
        return run
    if not use_agent:
        print("\n  Claude step skipped (--no-agent or no ANTHROPIC_API_KEY).")
        run["agent_skipped"] = "not requested"
        return run

    # 3. Claude
    section(f"3. Claude recovery agent ({MODEL})")
    outcome = run_recovery(incident)
    route = outcome["approved_route"]
    rejections = sum(not p["approved"] for p in outcome["proposals"])
    if route:
        agrees = route["route"] == chosen.get("path")
        print(f"\n  Verifier-approved route from Claude: {route_text(route['route'], route['total_distance_m'])} "
              f"after {len(outcome['proposals'])} proposal(s), {rejections} rejected. "
              f"Same as the verifier's own answer: {'YES' if agrees else 'NO'}")
    else:
        agrees = not chosen["valid"]
        print(f"\n  No route was approved ({len(outcome['proposals'])} proposal(s), all rejected). "
              f"Agrees there is no route: {'YES' if agrees else 'NO'}")
    print(f"\n{outcome['final_text']}")
    run["agent"] = {"model": MODEL, **outcome, "agrees_with_verifier": agrees}

    # 4. Fo
    section("4. Fo: paste into Claude Web (Fo connector on)")
    print(build_fo_prompt(incident, outcome))
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", type=int, action="append", choices=[s["id"] for s in SCENARIOS],
                        help="run only this scenario (repeatable)")
    parser.add_argument("--no-agent", action="store_true", help="skip the Claude step")
    parser.add_argument("--pause", type=float, default=0, help="seconds to wait between scenarios")
    parser.add_argument("--no-save", action="store_true", help="don't write docs/scenario_runs.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(errors="replace")

    import os
    use_agent = not args.no_agent and bool(os.getenv("ANTHROPIC_API_KEY"))
    chosen = [s for s in SCENARIOS if not args.scenario or s["id"] in args.scenario]

    runs = []
    try:
        for i, s in enumerate(chosen):
            if i and args.pause:
                time.sleep(args.pause)
            runs.append(run_scenario(s, use_agent))
    except anthropic.AuthenticationError:
        sys.exit("The Anthropic API rejected ANTHROPIC_API_KEY; check the key in .env.")
    finally:
        tools.reset_graph()

    print(f"\n{RULE}\n  SUMMARY\n{RULE}")
    for r in runs:
        s, agent = r["scenario"], r["agent"]
        claude = ("skipped: " + r["agent_skipped"]) if agent is None else (
            f"{len(agent['proposals'])} proposal(s), agrees with verifier: {'yes' if agent['agrees_with_verifier'] else 'NO'}")
        print(f"  {s['id']}. {s['name']:<17} verifier matches expected: {'yes' if r['matches_expected'] else 'NO':<4} | Claude: {claude}")
    print("\n  The live graph has been reset (all corridors open).")

    if not args.no_save and use_agent and not args.scenario:
        # Contacts never go in this file: it's published with the dashboard.
        record = {"recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "model": MODEL, "runs": runs}
        RUNS_FILE.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  Saved all runs to {RUNS_FILE.relative_to(ROOT)}; run python scripts/build_dashboard.py to publish them.")


if __name__ == "__main__":
    main()
