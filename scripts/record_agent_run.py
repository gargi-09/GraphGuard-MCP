"""Record a real recovery-agent run for the dashboard's replay button.

    python scripts/record_agent_run.py

Resets the graph, blocks the monitored corridor, runs the Claude recovery
agent (it only drafts the notices; nothing is sent), and saves the
transcript to docs/recorded_run.json. Visitors without an API key can then
replay a genuine run on the GitHub Pages dashboard. Spends a little API
credit; rerun scripts/build_dashboard.py afterwards.
"""
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from graph import tools  # noqa: E402
from recovery_agent import MODEL, run_recovery  # noqa: E402
from vision.trigger_node import DESTINATION, MONITORED_CORRIDOR, ORIGIN  # noqa: E402

OUTPUT = ROOT / "docs" / "recorded_run.json"


def main():
    tools.reset_graph()
    tools.set_corridor_blocked(*MONITORED_CORRIDOR, True)
    incident = {
        "event": "OBSTRUCTION_DETECTED",
        "blocked_corridor": list(MONITORED_CORRIDOR),
        "origin": ORIGIN,
        "destination": DESTINATION,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    log = io.StringIO()
    try:
        with redirect_stdout(log):
            outcome = run_recovery(incident)
    finally:
        tools.reset_graph()

    record = {
        "model": MODEL,
        "recorded_at": incident["timestamp"],
        "incident": incident,
        "log": [line for line in log.getvalue().splitlines() if line.strip()],
        "approved_route": outcome["approved_route"],
        "proposals": outcome["proposals"],
        "final_text": outcome["final_text"],
    }
    OUTPUT.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    route = outcome["approved_route"]
    print(f"Recorded run to {OUTPUT.relative_to(ROOT)}: "
          f"{'-'.join(route['route']) if route else 'no approved route'}")


if __name__ == "__main__":
    main()
