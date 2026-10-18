"""End-to-end FoGuard demo: baseline route, obstruction, rejection, verified fallback,
then the Claude API recovery agent.

    python demo_run.py               # step 3 shows the simulated camera feed
    python demo_run.py --no-visual   # no window: plain mock trigger

Step 3 plays the trigger node's presentation mode (open corridor, then
blocked) when the demo_assets/ images are present, and falls back to the
plain mock trigger otherwise. Step 6 runs only when ANTHROPIC_API_KEY is set
(in .env); without it the trigger prints the copy-paste prompt for Claude Web.

Leaves corridor B <-> C blocked so the MCP server (and Claude through it)
sees the incident afterwards. Run `python -m graph.init_db` to clear it.
"""
import argparse
import os
import sys

import anthropic
import cv2
from dotenv import load_dotenv

from graph.init_db import DB_PATH, close_db, cloud_sync_folder, init_db
from graph.verifier import RouteVerifier
from recovery_agent import build_fo_prompt, run_recovery
from vision.trigger_node import missing_demo_images, run_mock, run_visual_demo

load_dotenv()

BASELINE = ["A", "B", "C", "F"]
FALLBACK = ["A", "D", "E", "F"]


def step(number: int, title: str):
    print(f"\n--- Step {number}: {title} " + "-" * max(0, 56 - len(title)))


def expect(condition: bool, message: str):
    if not condition:
        print(f"[demo] FAILED: {message}")
        close_db()
        sys.exit(1)


def trigger_obstruction(visual: bool, print_card: bool) -> dict:
    """Step 3: the presentation-mode feed when possible, else the plain mock."""
    if visual:
        missing = missing_demo_images()
        if missing:
            print("[demo] Demo images not found in demo_assets/ "
                  f"({', '.join(p.name for p in missing)}); using the plain mock trigger.")
        else:
            try:
                return run_visual_demo(print_card=print_card)
            except cv2.error as e:  # e.g. no display to open a window on
                print(f"[demo] Couldn't open the feed window ({e.err}); using the plain mock trigger.")
    return run_mock(print_card=print_card)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-visual", action="store_true",
                        help="skip the simulated camera window in step 3")
    args = parser.parse_args()

    # Claude's replies can contain characters like arrows that the Windows
    # cp1252 console can't encode; replace them rather than crash.
    sys.stdout.reconfigure(errors="replace")

    step(1, "Reset the facility graph")
    sync = cloud_sync_folder()
    expect(sync is None,
           f"{DB_PATH} is inside {sync}. Sync clients corrupt Kuzu databases; "
           f"move the project out of {sync} and rerun.")
    init_db(reset=True)
    print(f"[demo] Kuzu graph seeded at {DB_PATH} (not in a cloud-sync folder)")

    step(2, "Baseline optimal route")
    baseline = RouteVerifier().find_shortest_valid_route("A", "F")
    expect(baseline["valid"] and baseline["path"] == BASELINE,
           f"expected baseline {'-'.join(BASELINE)}, got {baseline}")
    print(f"[demo] Optimal A -> F: {' -> '.join(baseline['path'])} "
          f"({baseline['total_distance']:g} m)")
    # The trigger node opens the database itself; Kuzu allows one open handle.
    close_db()

    # With an API key the agent takes over, so the copy-paste card isn't needed.
    use_agent = bool(os.getenv("ANTHROPIC_API_KEY"))

    step(3, "Vision trigger reports an obstruction")
    incident = trigger_obstruction(visual=not args.no_visual, print_card=not use_agent)

    step(4, "Verifier rejects the old baseline route")
    verifier = RouteVerifier()
    rejected = verifier.verify_route(BASELINE)
    expect(not rejected["valid"], f"baseline route was still approved: {rejected}")
    print(f"[demo] {' -> '.join(BASELINE)} REJECTED: {rejected['reason']}")

    step(5, "Shortest valid fallback route")
    fallback = verifier.find_shortest_valid_route("A", "F")
    expect(fallback["valid"] and fallback["path"] == FALLBACK,
           f"expected fallback {'-'.join(FALLBACK)}, got {fallback}")
    print(f"[demo] Fallback A -> F: {' -> '.join(fallback['path'])} "
          f"({fallback['total_distance']:g} m), verified segment by segment")

    # The agent's tools open the database themselves.
    close_db()

    if use_agent:
        step(6, "Claude recovery agent reroutes and drafts the notices")
        try:
            outcome = run_recovery(incident)
        except anthropic.AuthenticationError:
            expect(False, "the Anthropic API rejected ANTHROPIC_API_KEY; check the key in .env.")
        except anthropic.APIConnectionError:
            expect(False, "could not reach the Anthropic API; check the network connection.")
        except anthropic.APIStatusError as e:
            expect(False, f"Anthropic API error {e.status_code}: {e.message}")
        route = outcome["approved_route"]
        expect(route is not None, "Claude finished without getting a route approved.")
        expect(route["route"] == FALLBACK,
               f"Claude's approved route {'-'.join(route['route'])} is not the shortest "
               f"({'-'.join(FALLBACK)})")
        print(f"\n[demo] Verifier-approved agent route: {' -> '.join(route['route'])} "
              f"({route['total_distance_m']:g} m) after {len(outcome['proposals'])} proposal(s)")

        step(7, "Hand the notices to Fo in Claude Web")
        rule = "=" * 72
        print(rule)
        print("  Paste everything between the dashed lines into Claude Web with the Fo")
        print("  connector on. Contacts come from FACILITIES_CONTACT / RECIPIENT_CONTACT in .env.")
        print("-" * 72)
        print(build_fo_prompt(incident, outcome))
        print("-" * 72)
        print(rule)
    else:
        print("\n[demo] Skipping step 6: set ANTHROPIC_API_KEY in .env to run the Claude "
              "recovery agent.")

    print("\n[demo] Complete. Corridor B <-> C stays blocked for the MCP server; "
          "run `python -m graph.init_db` to reset.")


if __name__ == "__main__":
    main()
