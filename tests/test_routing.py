"""FoGuard deterministic routing suite: the single source of truth for diagnostics.

    python tests/test_routing.py        # prints the 5/5 run trace
    python -m tests.test_routing        # same

run_all_tests() is also what the MCP server's GET /run_diagnostics returns and
what the dashboard renders, so the terminal and the dashboard show the same
results from the same code.

Every run builds a fresh graph in a throwaway database, seeded by the same
code as data/kuzu_db (graph.init_db.seed_graph), so results are deterministic
and the live graph is never touched, even when the endpoint is called publicly.
"""
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import kuzu  # noqa: E402

from graph.init_db import CORRIDORS, mark_edge_blocked, seed_graph  # noqa: E402
from graph.verifier import RouteVerifier  # noqa: E402

SUITE_NAME = "FoGuard deterministic routing"

# Each scenario sets exactly these corridors blocked (everything else open),
# then asks the verifier's shortest-route search for origin -> destination.
SCENARIOS = [
    {
        "id": 1, "name": "Classic Reroute",
        "description": "Block B <-> C. The normal route A-B-C-F is cut, so A -> F must reroute through the bypass.",
        "blocked": [["B", "C"]], "origin": "A", "destination": "F",
        "expect": {"valid": True, "path": ["A", "D", "E", "F"], "total_distance": 38.0},
    },
    {
        "id": 2, "name": "Cross-Link Test",
        "description": "Block B <-> C and A <-> D. Both the normal route and the bypass entrance are cut, "
                       "so A -> F must use the B <-> D cross-link.",
        "blocked": [["B", "C"], ["A", "D"]], "origin": "A", "destination": "F",
        "expect": {"valid": True, "path": ["A", "B", "D", "E", "F"], "total_distance": 39.0},
    },
    {
        "id": 3, "name": "Short Trip",
        "description": "Block C <-> E. B -> E can't go via C, so it takes the B <-> D cross-link.",
        "blocked": [["C", "E"]], "origin": "B", "destination": "E",
        "expect": {"valid": True, "path": ["B", "D", "E"], "total_distance": 20.0},
    },
    {
        "id": 4, "name": "Impossible Route",
        "description": "Block C <-> F and E <-> F, F's only two corridors. No route to F exists, "
                       "and the verifier must say so rather than invent one.",
        "blocked": [["C", "F"], ["E", "F"]], "origin": "A", "destination": "F",
        "expect": {"valid": False},
    },
    {
        "id": 5, "name": "Clear Path",
        "description": "Unblock everything. D -> C takes the shortest immediate path over the cross-link.",
        "blocked": [], "origin": "D", "destination": "C",
        "expect": {"valid": True, "path": ["D", "B", "C"], "total_distance": 18.0},
    },
]

_suite_lock = threading.Lock()  # the MCP server may be asked to run it concurrently


@contextmanager
def _isolated_graph():
    """A freshly seeded graph in a temporary database, deleted afterwards."""
    tmp = Path(tempfile.mkdtemp(prefix="foguard-tests-"))
    db = kuzu.Database(str(tmp / "kuzu_db"))
    conn = kuzu.Connection(db)
    try:
        seed_graph(conn)
        yield conn
    finally:
        conn.close()
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _check_scenario(scenario: dict, conn: kuzu.Connection) -> dict:
    blocked = {tuple(sorted(pair)) for pair in scenario["blocked"]}
    for a, b, _ in CORRIDORS:
        mark_edge_blocked(a, b, tuple(sorted((a, b))) in blocked, conn=conn)

    verifier = RouteVerifier(conn)
    started = time.perf_counter()
    result = verifier.find_shortest_valid_route(scenario["origin"], scenario["destination"])
    duration_ms = round((time.perf_counter() - started) * 1000, 2)

    expect = scenario["expect"]
    failures = []
    if result["valid"] != expect["valid"]:
        failures.append(f"expected valid={expect['valid']}, got valid={result['valid']}")
    elif expect["valid"]:
        if result["path"] != expect["path"]:
            failures.append(f"expected path {'-'.join(expect['path'])}, got {'-'.join(result['path'])}")
        if result["total_distance"] != expect["total_distance"]:
            failures.append(f"expected {expect['total_distance']:g} m, got {result['total_distance']:g} m")
    # Prove each block took effect: a hop across it must be rejected.
    for a, b in scenario["blocked"]:
        hop = verifier.verify_route([a, b])
        if hop["valid"]:
            failures.append(f"blocked corridor {a} <-> {b} was still accepted")

    return {
        "id": scenario["id"],
        "name": scenario["name"],
        "description": scenario["description"],
        "blocked": scenario["blocked"],
        "origin": scenario["origin"],
        "destination": scenario["destination"],
        "expected": expect,
        "result": result,
        "passed": not failures,
        "failures": failures,
        "duration_ms": duration_ms,
    }


def run_all_tests() -> dict:
    """Run every scenario on a freshly reset graph and return the results as JSON-ready data."""
    with _suite_lock, _isolated_graph() as conn:
        started = time.perf_counter()
        results = [_check_scenario(s, conn) for s in SCENARIOS]
        total_ms = round((time.perf_counter() - started) * 1000, 2)
    passed = sum(r["passed"] for r in results)
    return {
        "suite": SUITE_NAME,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": "Python RouteVerifier on a freshly seeded Kuzu graph",
        "passed": passed,
        "total": len(results),
        "all_passed": passed == len(results),
        "duration_ms": total_ms,
        "results": results,
    }


def format_trace(report: dict) -> str:
    """Plain-text trace of a run, for the terminal and written submissions."""
    lines = [f"{report['suite']}  |  {report['ran_at']}", f"Engine: {report['engine']}", ""]
    for r in report["results"]:
        status = "PASS" if r["passed"] else "FAIL"
        blocked = ", ".join(f"{a}<->{b}" for a, b in r["blocked"]) or "none"
        got = r["result"]
        outcome = (f"{' -> '.join(got['path'])} ({got['total_distance']:g} m)" if got["valid"]
                   else f"no route: {got['reason']}")
        lines.append(f"[{status}] {r['id']}. {r['name']}  ({r['duration_ms']} ms)")
        lines.append(f"       blocked: {blocked}   trip: {r['origin']} -> {r['destination']}")
        lines.append(f"       result:  {outcome}")
        for failure in r["failures"]:
            lines.append(f"       FAILURE: {failure}")
    lines += ["", f"{report['passed']}/{report['total']} passed in {report['duration_ms']} ms"]
    return "\n".join(lines)


def test_all_scenarios_pass():
    """pytest entry point."""
    report = run_all_tests()
    assert report["all_passed"], format_trace(report)


if __name__ == "__main__":
    report = run_all_tests()
    print(format_trace(report))
    sys.exit(0 if report["all_passed"] else 1)
