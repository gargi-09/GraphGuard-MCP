"""The two agent-facing FoGuard tools, shared by the MCP server and the API agent.

Both open the Kuzu database only for the length of the call: Kuzu allows one
read-write process per file, so holding it would lock out the vision trigger
node, demo_run.py, or the other of these two callers.
"""
import contextlib
import sys
import threading

from graph.init_db import CORRIDORS, close_db, get_topology, init_db, mark_edge_blocked
from graph.verifier import RouteVerifier

# Serializes every database open in this process. Public so code that opens
# the DB another way (the trigger node's report_obstruction) can hold it too.
graph_lock = threading.Lock()


@contextlib.contextmanager
def _graph():
    with graph_lock, contextlib.redirect_stdout(sys.stderr):
        # init_db's status prints would corrupt an MCP stdio protocol stream.
        init_db()
        try:
            yield
        finally:
            close_db()


def get_workspace_topology() -> dict:
    """Return the facility layout: every location, every corridor with its length
    in metres, and which corridors and locations are currently blocked.

    Corridors are two-way; a blocked corridor is impassable in both directions.
    """
    with _graph():
        topology = get_topology()

    corridors = {}
    for edge in topology["edges"]:
        pair = tuple(sorted((edge["from"], edge["to"])))
        corridor = corridors.setdefault(
            pair, {"between": list(pair), "distance_m": edge["distance"], "is_blocked": False}
        )
        corridor["is_blocked"] = corridor["is_blocked"] or edge["is_blocked"]

    corridor_list = list(corridors.values())
    return {
        "locations": topology["locations"],
        "corridors": corridor_list,
        "blocked_corridors": [c["between"] for c in corridor_list if c["is_blocked"]],
        "blocked_locations": [l["id"] for l in topology["locations"] if l["is_blocked"]],
    }


def propose_recovery_route(origin: str, destination: str, proposed_path: list[str]) -> dict:
    """Check a proposed route against the live facility graph.

    Each consecutive pair in proposed_path must be joined by an unblocked
    corridor. Returns approved=true with the route and its total distance, or
    approved=false with the exact reason so the path can be fixed.

    Args:
        origin: Location ID the route starts from, e.g. "A".
        destination: Location ID the route ends at, e.g. "F".
        proposed_path: Full ordered list of location IDs including origin and destination, e.g. ["A", "D", "E", "F"].
    """
    if not proposed_path or proposed_path[0] != origin:
        return {"approved": False,
                "reason": f"Route must start at the origin {origin}."}
    if proposed_path[-1] != destination:
        return {"approved": False,
                "reason": f"Route must end at the destination {destination}."}

    with _graph():
        result = RouteVerifier().verify_route(proposed_path)

    if not result["valid"]:
        return {"approved": False, "reason": result["reason"]}
    return {"approved": True, "route": result["path"],
            "total_distance_m": result["total_distance"]}


def shortest_route(origin: str, destination: str) -> dict:
    """Shortest passable route by distance, re-checked by the verifier."""
    with _graph():
        return RouteVerifier().find_shortest_valid_route(origin, destination)


def set_corridor_blocked(a: str, b: str, blocked: bool) -> bool:
    """Block or clear the corridor between a and b (both directions)."""
    with _graph():
        return mark_edge_blocked(a, b, blocked)


def reset_graph():
    """Wipe the database and reseed it: every corridor open again."""
    with graph_lock, contextlib.redirect_stdout(sys.stderr):
        close_db()
        init_db(reset=True)
        close_db()


def candidate_paths(origin: str, destination: str) -> list[list[str]]:
    """Every simple path between two waypoints on the physical map, ignoring blocks."""
    neighbours = {}
    for a, b, _ in CORRIDORS:
        neighbours.setdefault(a, []).append(b)
        neighbours.setdefault(b, []).append(a)
    paths, stack = [], [[origin]]
    while stack:
        path = stack.pop()
        if path[-1] == destination:
            paths.append(path)
            continue
        for nxt in sorted(neighbours.get(path[-1], [])):
            if nxt not in path:
                stack.append(path + [nxt])
    return paths


def route_options(origin: str, destination: str) -> dict:
    """The verifier's reasoning made visible: every candidate route and its verdict.

    Each path that exists on the physical map is run through the same
    verify_route guardrail an agent's proposal goes through. Valid routes are
    ranked by distance; rejected ones keep the verifier's exact reason.
    """
    with _graph():
        verifier = RouteVerifier()
        options = []
        for path in candidate_paths(origin, destination):
            verdict = verifier.verify_route(path)
            options.append({"path": path, "valid": verdict["valid"],
                            "total_distance": verdict.get("total_distance"),
                            "reason": verdict.get("reason")})
        shortest = verifier.find_shortest_valid_route(origin, destination)
    options.sort(key=lambda o: (not o["valid"], o["total_distance"] or 0, len(o["path"])))
    return {"origin": origin, "destination": destination, "options": options, "chosen": shortest}
