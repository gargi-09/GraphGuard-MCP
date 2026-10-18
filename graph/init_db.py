import os
import shutil
from pathlib import Path

import kuzu

# Resolved from the project root so the DB lands in ./data/kuzu_db no matter
# which directory the script is launched from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "kuzu_db"

LOCATIONS = [
    ("A", "Dispatch"),
    ("B", "Corridor North"),
    ("C", "Assembly Bay"),
    ("D", "Corridor South"),
    ("E", "Storage Room"),
    ("F", "Delivery Station"),
]

# Physical corridors are walkable both ways, so each one is seeded as a pair
# of directed CONNECTED_TO edges (see CONNECTIONS below).
CORRIDORS = [
    # primary route
    ("A", "B", 10.0),
    ("B", "C", 12.0),
    ("C", "F", 8.0),
    # bypass route
    ("A", "D", 15.0),
    ("D", "E", 14.0),
    ("E", "F", 9.0),
    # cross links
    ("B", "D", 6.0),
    ("C", "E", 7.0),
]

CONNECTIONS = [
    edge
    for a, b, distance in CORRIDORS
    for edge in ((a, b, distance), (b, a, distance))
]

SCHEMA = [
    "CREATE NODE TABLE IF NOT EXISTS Location("
    "id STRING, name STRING, is_blocked BOOLEAN, PRIMARY KEY (id))",
    "CREATE REL TABLE IF NOT EXISTS CONNECTED_TO("
    "FROM Location TO Location, distance DOUBLE, is_blocked BOOLEAN)",
]

_db = None
_conn = None


def _rows(result) -> list:
    rows = []
    while result.has_next():
        rows.append(result.get_next())
    return rows


def _remove_db_files():
    """Delete the database and its sidecar files (.wal, .lock, ...)."""
    for path in DB_PATH.parent.glob(f"{DB_PATH.name}*"):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        print(f"[init_db] Removed {path.name}")


def _open() -> kuzu.Connection:
    global _db, _conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Kuzu >= 0.11 stores the database as a single file. A directory at this
    # path is a leftover from an older Kuzu version and can't be opened.
    if DB_PATH.is_dir():
        print("[init_db] Found old directory-format database, rebuilding")
        _remove_db_files()

    try:
        _db = kuzu.Database(str(DB_PATH))
    except RuntimeError as e:
        # A lock error means another process has the DB open; deleting it
        # would destroy that process's data, so surface the error instead.
        if "lock" in str(e).lower():
            raise RuntimeError(
                f"{DB_PATH} is in use by another process. Close it and retry."
            ) from e
        print(f"[init_db] Could not open database ({e}), rebuilding")
        _remove_db_files()
        _db = kuzu.Database(str(DB_PATH))

    _conn = kuzu.Connection(_db)
    return _conn


CLOUD_SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "box sync")


def cloud_sync_folder(path: Path = DB_PATH):
    """Return the sync service a path sits under (OneDrive, Dropbox, ...), or None.

    Sync clients lock and rewrite files behind Kuzu's back, which corrupts the
    database or makes it fail to open, so the DB must live outside them.
    """
    resolved = path.resolve()
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(var)
        if root and resolved.is_relative_to(Path(root).resolve()):
            return "OneDrive"
    for part in resolved.parts:
        if any(marker in part.lower() for marker in CLOUD_SYNC_MARKERS):
            return part
    return None


def close_db():
    global _db, _conn
    if _conn is not None:
        _conn.close()
    if _db is not None:
        _db.close()
    _db = _conn = None


def get_connection() -> kuzu.Connection:
    return _conn if _conn is not None else _open()


def init_db(reset: bool = False) -> kuzu.Connection:
    """Create the schema and seed waypoints. Safe to call repeatedly.

    reset=True wipes the database first, which also clears any blocked flags.
    Without it, existing nodes/edges (and their blocked state) are kept.
    """
    if reset:
        close_db()
        _remove_db_files()

    return seed_graph(get_connection())


def seed_graph(conn: kuzu.Connection) -> kuzu.Connection:
    """Create the schema and the waypoints/corridors on any connection.

    Separate from init_db so tests can seed a throwaway database with exactly
    the same layout (tests/test_routing.py).
    """
    for ddl in SCHEMA:
        conn.execute(ddl)

    for loc_id, name in LOCATIONS:
        conn.execute(
            "MERGE (l:Location {id: $id}) "
            "ON CREATE SET l.name = $name, l.is_blocked = false",
            {"id": loc_id, "name": name},
        )

    for from_id, to_id, distance in CONNECTIONS:
        conn.execute(
            "MATCH (a:Location {id: $from_id}), (b:Location {id: $to_id}) "
            "MERGE (a)-[r:CONNECTED_TO]->(b) "
            "ON CREATE SET r.distance = $distance, r.is_blocked = false",
            {"from_id": from_id, "to_id": to_id, "distance": distance},
        )

    return conn


def mark_edge_blocked(from_id: str, to_id: str, blocked: bool = True,
                      conn: kuzu.Connection | None = None) -> bool:
    """Set is_blocked on the corridor between from_id and to_id.

    An obstructed corridor is impassable both ways, so the edge in each
    direction is updated. Returns False if no such edge exists. conn defaults
    to the shared connection to data/kuzu_db.
    """
    # Equivalent to an undirected -[r]- match, but Kuzu 0.11.3 hits an
    # internal assertion (csr_node_group.cpp, KU_UNREACHABLE) when SETting a
    # rel property through an undirected pattern, so match both orientations
    # of a directed pattern instead.
    result = (conn or get_connection()).execute(
        "MATCH (a:Location)-[r:CONNECTED_TO]->(b:Location) "
        "WHERE (a.id = $from_id AND b.id = $to_id) "
        "OR (a.id = $to_id AND b.id = $from_id) "
        "SET r.is_blocked = $blocked "
        "RETURN count(r) > 0 AS updated",
        {"from_id": from_id, "to_id": to_id, "blocked": blocked},
    )
    return _rows(result)[0][0]


def get_topology() -> dict:
    """Return every location and every edge with its current blocked status."""
    conn = get_connection()

    locations = [
        {"id": loc_id, "name": name, "is_blocked": is_blocked}
        for loc_id, name, is_blocked in _rows(conn.execute(
            "MATCH (l:Location) "
            "RETURN l.id, l.name, l.is_blocked ORDER BY l.id"
        ))
    ]

    edges = [
        {"from": from_id, "to": to_id, "distance": distance, "is_blocked": is_blocked}
        for from_id, to_id, distance, is_blocked in _rows(conn.execute(
            "MATCH (a:Location)-[r:CONNECTED_TO]->(b:Location) "
            "RETURN a.id, b.id, r.distance, r.is_blocked ORDER BY a.id, b.id"
        ))
    ]

    return {"locations": locations, "edges": edges}


if __name__ == "__main__":
    print(f"[init_db] Initializing database at {DB_PATH}")
    init_db(reset=True)

    topology = get_topology()

    print("\nLocations:")
    for loc in topology["locations"]:
        print(f"  {loc['id']}  {loc['name']:<18} blocked={loc['is_blocked']}")

    print("\nConnections:")
    for edge in topology["edges"]:
        print(f"  {edge['from']} -> {edge['to']}  {edge['distance']:>5.1f} m  "
              f"blocked={edge['is_blocked']}")

    expected = {(f, t): d for f, t, d in CONNECTIONS}
    actual = {(e["from"], e["to"]): e["distance"] for e in topology["edges"]}
    assert len(topology["locations"]) == len(LOCATIONS), "location count mismatch"
    assert actual == expected, f"edge mismatch: {actual} != {expected}"
    assert not any(e["is_blocked"] for e in topology["edges"]), "edge blocked after seeding"

    # Re-running init without reset must not duplicate anything.
    init_db()
    assert len(get_topology()["edges"]) == len(CONNECTIONS), "re-init duplicated edges"

    # Block / unblock round trip: blocking a corridor closes both directions,
    # whichever end it is named from.
    assert mark_edge_blocked("B", "C")
    blocked = [(e["from"], e["to"]) for e in get_topology()["edges"] if e["is_blocked"]]
    assert blocked == [("B", "C"), ("C", "B")], f"unexpected blocked edges: {blocked}"
    assert mark_edge_blocked("C", "B", blocked=False)
    assert not any(e["is_blocked"] for e in get_topology()["edges"]), "unblock missed an edge"
    assert not mark_edge_blocked("A", "F"), "non-existent edge reported as updated"

    print(f"\n[init_db] OK: {len(topology['locations'])} locations, "
          f"{len(topology['edges'])} connections verified")
    close_db()
