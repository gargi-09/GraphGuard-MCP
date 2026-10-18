"""Check the dashboard's JavaScript simulation against the real Python verifier.

    python scripts/check_sim_parity.py

For every combination of blocked corridors (2^8 = 256), compares the shortest
route and a set of proposed routes between graph/verifier.py and the engine
embedded in docs/index.html. Needs Node.js; uses a throwaway database so the
real data/kuzu_db is untouched. Exits 1 on any mismatch.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import graph.init_db as db  # noqa: E402
from graph.verifier import RouteVerifier  # noqa: E402

PAIRS = [("A", "F"), ("F", "A"), ("B", "E"), ("D", "C")]
PROPOSALS = [["A", "B", "C", "F"], ["A", "D", "E", "F"], ["A", "C", "F"],
             ["A", "Z", "F"], ["A", "B", "D", "E", "F"], ["A", "D", "E", "C", "F"]]

JS_RUNNER = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const start = html.indexOf("const sim = (() =>");
const end = html.indexOf("// ---------------------------------------------------------------- backend");
const DATA = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const sim = new Function("DATA", html.slice(start, end) + "; return sim;")(DATA);
const { masks, pairs, proposals } = JSON.parse(fs.readFileSync(0, "utf8"));
const C = DATA.facility.corridors;
const out = masks.map((mask) => {
  sim.reset();
  C.forEach((c, i) => { if (mask & (1 << i)) sim.setBlocked(c.a, c.b, true); });
  return {
    shortest: pairs.map(([o, d]) => sim.shortest(o, d)),
    verify: proposals.map((p) => sim.propose("A", "F", p)),
  };
});
process.stdout.write(JSON.stringify(out));
"""


def normalize(value):
    """JSON numbers compare by value: Python writes 30.0 where JavaScript writes 30."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return {k: normalize(v) for k, v in value.items()}


def python_results(masks):
    out = []
    conn = db.get_connection()
    verifier = RouteVerifier(conn)
    for mask in masks:
        for i, (a, b, _) in enumerate(db.CORRIDORS):
            db.mark_edge_blocked(a, b, bool(mask & (1 << i)))
        shortest = [verifier.find_shortest_valid_route(o, d) for o, d in PAIRS]
        verify = []
        for path in PROPOSALS:
            # Mirror graph.tools.propose_recovery_route without reopening the DB.
            if not path or path[0] != "A":
                verify.append({"approved": False, "reason": "Route must start at the origin A."})
                continue
            if path[-1] != "F":
                verify.append({"approved": False, "reason": "Route must end at the destination F."})
                continue
            r = verifier.verify_route(path)
            verify.append({"approved": False, "reason": r["reason"]} if not r["valid"] else
                          {"approved": True, "route": r["path"], "total_distance_m": r["total_distance"]})
        out.append({"shortest": shortest, "verify": verify})
    return out


def main():
    node = shutil.which("node")
    if not node:
        print("Node.js not found; skipping the simulation parity check.")
        return
    masks = list(range(2 ** len(db.CORRIDORS)))

    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "kuzu_db"  # throwaway database
    try:
        db.init_db(reset=True)
        expected = python_results(masks)
    finally:
        db.close_db()
        shutil.rmtree(tmp, ignore_errors=True)

    data_js = (ROOT / "docs" / "data.js").read_text(encoding="utf-8")
    data_json = data_js[data_js.index("{"):data_js.rindex("}") + 1]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write(data_json)
        data_path = f.name
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(JS_RUNNER)
        runner_path = f.name
    run = subprocess.run([node, runner_path, str(ROOT / "docs" / "index.html"), data_path],
                         input=json.dumps({"masks": masks, "pairs": PAIRS, "proposals": PROPOSALS}),
                         capture_output=True, text=True, encoding="utf-8")
    Path(data_path).unlink()
    Path(runner_path).unlink()
    if run.returncode != 0:
        print(run.stderr)
        sys.exit(1)
    actual = json.loads(run.stdout)

    mismatches = [(m, e, a) for m, e, a in zip(masks, expected, actual)
                  if normalize(e) != normalize(a)]
    checks = len(masks) * (len(PAIRS) + len(PROPOSALS))
    if mismatches:
        mask, e, a = mismatches[0]
        print(f"{len(mismatches)} of {len(masks)} block combinations differ. First, mask {mask:08b}:")
        print(" python:", json.dumps(e)[:400])
        print(" js:    ", json.dumps(a)[:400])
        sys.exit(1)
    print(f"Simulation matches the Python verifier: {checks} checks over {len(masks)} block combinations.")


if __name__ == "__main__":
    main()
