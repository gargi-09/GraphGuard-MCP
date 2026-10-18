"""RouteVerifier: the deterministic guardrail every proposed route must pass."""
import heapq

import kuzu

from graph.init_db import _rows, get_connection

SEGMENT_QUERY = (
    "MATCH (a:Location)-[r:CONNECTED_TO]->(b:Location) "
    "WHERE a.id = $from_id AND b.id = $to_id "
    "RETURN a.is_blocked, b.is_blocked, r.is_blocked, r.distance"
)


class RouteVerifier:
    """Checks routes against the Kuzu graph so an LLM can't invent paths.

    Every answer comes from the database, never from the proposed route itself:
    a route is only approved if each hop is a real, currently passable edge.
    """

    def __init__(self, conn: kuzu.Connection | None = None):
        # Shares init_db's connection by default: Kuzu allows only one
        # read-write Database per file, so a second one would fail to lock.
        self.conn = conn or get_connection()

    def _location_states(self) -> dict:
        """Map every location ID to its is_blocked flag."""
        return {
            loc_id: is_blocked
            for loc_id, is_blocked in _rows(self.conn.execute(
                "MATCH (l:Location) RETURN l.id, l.is_blocked"
            ))
        }

    def verify_route(self, proposed_route: list[str]) -> dict:
        """Check a route hop by hop against the live graph.

        Returns {"valid": True, "path", "total_distance"}, or
        {"valid": False, "reason"} naming the first unknown location,
        missing corridor or blocked segment.
        """
        if (
            not isinstance(proposed_route, list)
            or not proposed_route
            or not all(isinstance(loc_id, str) for loc_id in proposed_route)
        ):
            return {"valid": False,
                    "reason": "Route must be a non-empty list of location IDs."}

        # Name invented IDs explicitly instead of letting them surface as a
        # generic missing edge.
        locations = self._location_states()
        for loc_id in proposed_route:
            if loc_id not in locations:
                return {"valid": False, "reason": f"Unknown location: {loc_id}."}

        # A one-stop route has no segments to check, so check the stop itself.
        if len(proposed_route) == 1 and locations[proposed_route[0]]:
            return {"valid": False,
                    "reason": f"Location {proposed_route[0]} is currently impassable."}

        total_distance = 0.0
        for from_id, to_id in zip(proposed_route, proposed_route[1:]):
            rows = _rows(self.conn.execute(
                SEGMENT_QUERY, {"from_id": from_id, "to_id": to_id}
            ))
            if not rows:
                return {"valid": False,
                        "reason": f"Disallowed jump: No pathway connects {from_id} -> {to_id}."}

            a_blocked, b_blocked, r_blocked, distance = rows[0]
            if a_blocked or b_blocked or r_blocked:
                return {"valid": False,
                        "reason": f"Path blocked: Segment {from_id} -> {to_id} is currently impassable."}
            total_distance += distance

        return {"valid": True, "path": list(proposed_route),
                "total_distance": round(total_distance, 3)}

    def find_shortest_valid_route(self, origin: str, destination: str) -> dict:
        """Dijkstra by distance over unblocked edges between unblocked nodes."""
        locations = self._location_states()
        for loc_id in (origin, destination):
            if loc_id not in locations:
                return {"valid": False, "reason": f"Unknown location: {loc_id}."}
            if locations[loc_id]:
                return {"valid": False,
                        "reason": f"Location {loc_id} is currently impassable."}

        graph = {}
        for from_id, to_id, distance in _rows(self.conn.execute(
            "MATCH (a:Location)-[r:CONNECTED_TO]->(b:Location) "
            "WHERE NOT a.is_blocked AND NOT b.is_blocked AND NOT r.is_blocked "
            "RETURN a.id, b.id, r.distance"
        )):
            graph.setdefault(from_id, []).append((to_id, distance))

        best = {origin: 0.0}
        previous = {}
        queue = [(0.0, origin)]
        while queue:
            dist, node = heapq.heappop(queue)
            if node == destination:
                break
            if dist > best[node]:
                continue
            for neighbor, edge_distance in graph.get(node, []):
                candidate = dist + edge_distance
                if candidate < best.get(neighbor, float("inf")):
                    best[neighbor] = candidate
                    previous[neighbor] = node
                    heapq.heappush(queue, (candidate, neighbor))

        if destination not in best:
            return {"valid": False,
                    "reason": f"No passable route from {origin} to {destination}."}

        path = [destination]
        while path[-1] != origin:
            path.append(previous[path[-1]])
        path.reverse()

        # Re-check the result through the same per-segment guardrail that
        # LLM-proposed routes go through.
        return self.verify_route(path)


if __name__ == "__main__":
    from graph.init_db import close_db, init_db, mark_edge_blocked

    init_db(reset=True)
    verifier = RouteVerifier()

    def check(label, result, expect_valid):
        status = "PASS" if result["valid"] == expect_valid else "FAIL"
        print(f"[{status}] {label}: {result}")
        assert result["valid"] == expect_valid, label

    check("Baseline A-B-C-F", verifier.verify_route(["A", "B", "C", "F"]), True)
    check("Hallucinated jump A-C-F", verifier.verify_route(["A", "C", "F"]), False)
    check("Unknown location A-Z-F", verifier.verify_route(["A", "Z", "F"]), False)
    check("Empty route", verifier.verify_route([]), False)
    check("Return trip F-C-B-A", verifier.verify_route(["F", "C", "B", "A"]), True)

    shortest = verifier.find_shortest_valid_route("A", "F")
    check("Shortest A->F before block", shortest, True)
    assert shortest["path"] == ["A", "B", "C", "F"] and shortest["total_distance"] == 30.0

    mark_edge_blocked("B", "C")
    print("\n-- Blocked B <-> C --")
    check("A-B-C-F after block", verifier.verify_route(["A", "B", "C", "F"]), False)
    check("Return trip F-C-B-A after block", verifier.verify_route(["F", "C", "B", "A"]), False)
    check("Bypass A-D-E-F after block", verifier.verify_route(["A", "D", "E", "F"]), True)

    shortest = verifier.find_shortest_valid_route("A", "F")
    check("Shortest A->F after block", shortest, True)
    assert shortest["path"] == ["A", "D", "E", "F"] and shortest["total_distance"] == 38.0

    shortest = verifier.find_shortest_valid_route("F", "A")
    check("Shortest F->A after block", shortest, True)
    assert shortest["path"] == ["F", "E", "D", "A"] and shortest["total_distance"] == 38.0

    mark_edge_blocked("B", "C", blocked=False)
    print("\n[verifier] All checks passed")
    close_db()
