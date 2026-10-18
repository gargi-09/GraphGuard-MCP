"""Local HTTP API that backs the dashboard's Live mode.

    python api_server.py          # http://127.0.0.1:8001  (dashboard at /)

Every button in the dashboard's Live mode calls one of these endpoints, which
run the real Python code against the real Kuzu database. The server binds to
127.0.0.1 only. Browsers may call it from localhost pages, and from extra
origins listed in DASHBOARD_ORIGIN (comma-separated), e.g. your GitHub Pages
site: DASHBOARD_ORIGIN=https://yourname.github.io

Some endpoints change state (block corridors, reset the graph) or spend API
credit (the recovery agent), so only list origins you control.
"""
import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path

import anthropic
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from mcp import Client
from pydantic import BaseModel

from graph import tools
from mcp_server import LOCAL_MCP_URL, PUBLIC_MCP_URL
from graph.init_db import DB_PATH, LOCATIONS, cloud_sync_folder
from recovery_agent import MODEL, build_fo_prompt, run_recovery
from tests.test_routing import run_all_tests
from vision.trigger_node import MONITORED_CORRIDOR, build_recovery_prompt, report_obstruction

load_dotenv()

ROOT = Path(__file__).resolve().parent
PORT = 8001
LOCATION_IDS = {loc_id for loc_id, _ in LOCATIONS}
MCP_TARGETS = {"local": LOCAL_MCP_URL, "public": PUBLIC_MCP_URL}

app = FastAPI(title="FoGuard API")

allowed_origins = [
    f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}",
    *[o.strip().rstrip("/") for o in os.getenv("DASHBOARD_ORIGIN", "").split(",") if o.strip()],
]
# allow_private_network: Chrome asks before a public site (github.io) may call
# a server on this machine; this is the server's half of that opt-in.
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
                   allow_private_network=True)


def check_location(loc_id: str):
    if loc_id not in LOCATION_IDS:
        raise HTTPException(400, f"Unknown location: {loc_id}.")


class Corridor(BaseModel):
    a: str
    b: str
    blocked: bool


class RouteRequest(BaseModel):
    origin: str
    destination: str
    path: list[str] = []


class AgentRequest(BaseModel):
    facilities_contact: str = ""
    recipient_contact: str = ""


@app.get("/api/status")
def status():
    sync = cloud_sync_folder()
    return {
        "db_path": str(DB_PATH),
        "cloud_sync_folder": sync,
        "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        "contacts_set": bool(os.getenv("FACILITIES_CONTACT") and os.getenv("RECIPIENT_CONTACT")),
        "model": MODEL,
        "monitored_corridor": list(MONITORED_CORRIDOR),
        "mcp_targets": MCP_TARGETS,
    }


@app.get("/api/topology")
def topology():
    return tools.get_workspace_topology()


@app.post("/api/corridor")
def set_corridor(body: Corridor):
    check_location(body.a)
    check_location(body.b)
    if not tools.set_corridor_blocked(body.a, body.b, body.blocked):
        raise HTTPException(404, f"No corridor between {body.a} and {body.b}.")
    return tools.get_workspace_topology()


@app.post("/api/reset")
def reset():
    tools.reset_graph()
    return tools.get_workspace_topology()


@app.post("/api/verify")
def verify(body: RouteRequest):
    # Unknown IDs are left to the verifier, which names them in its reason.
    return tools.propose_recovery_route(body.origin, body.destination, body.path)


@app.post("/api/shortest")
def shortest(body: RouteRequest):
    check_location(body.origin)
    check_location(body.destination)
    return tools.shortest_route(body.origin, body.destination)


@app.post("/api/trigger/mock")
def trigger_mock():
    with tools.graph_lock, contextlib.redirect_stdout(io.StringIO()):
        incident = report_obstruction(print_card=False)
    reroute = tools.shortest_route(incident["origin"], incident["destination"])
    return {"incident": incident, "reroute": reroute,
            "prompt": build_recovery_prompt(incident, reroute)}


@app.post("/api/demo")
def demo():
    # Steps 1-5 only: a blank key makes demo_run skip the paid agent step
    # (load_dotenv never overrides a variable that is already set).
    env = {**os.environ, "ANTHROPIC_API_KEY": "", "PYTHONIOENCODING": "utf-8"}
    with tools.graph_lock:
        run = subprocess.run([sys.executable, "demo_run.py", "--no-visual"], cwd=ROOT, env=env,
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
    return {"exit_code": run.returncode, "output": run.stdout + run.stderr}


@app.post("/api/agent")
def agent(body: AgentRequest):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(400, "ANTHROPIC_API_KEY is not set in .env.")
    blocked = tools.get_workspace_topology()["blocked_corridors"]
    if not blocked:
        raise HTTPException(400, "No corridor is blocked. Run the mock trigger first.")
    from datetime import datetime, timezone
    incident = {
        "event": "OBSTRUCTION_DETECTED",
        "blocked_corridor": blocked[0],
        "origin": "A",
        "destination": "F",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log):
            outcome = run_recovery(incident)
    except anthropic.AuthenticationError:
        raise HTTPException(401, "The Anthropic API rejected ANTHROPIC_API_KEY.")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Anthropic API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Could not reach the Anthropic API.")
    # Contacts typed in the page win; otherwise build_fo_prompt uses .env.
    fo_prompt = (build_fo_prompt(incident, outcome, body.facilities_contact or None,
                                 body.recipient_contact or None)
                 if outcome["approved_route"] else None)
    return {"incident": incident, "log": log.getvalue(), "fo_prompt": fo_prompt, **outcome}


@app.get("/api/run_diagnostics")
def diagnostics():
    """The same routing suite as the MCP server's /run_diagnostics (tests/test_routing.py)."""
    return run_all_tests()


@app.get("/api/mcp")
async def mcp_check(target: str = "local"):
    url = MCP_TARGETS.get(target)
    if url is None:
        raise HTTPException(400, f"target must be one of {sorted(MCP_TARGETS)}.")
    try:
        async with Client(url) as client:
            listed = await client.list_tools()
    except Exception as e:  # any transport failure means "not reachable"
        return {"url": url, "reachable": False, "error": f"{type(e).__name__}: {e}"}
    return {"url": url, "reachable": True, "tools": [t.name for t in listed.tools]}


# The dashboard itself, so http://127.0.0.1:8001/ works without GitHub Pages.
app.mount("/", StaticFiles(directory=ROOT / "docs", html=True), name="dashboard")


if __name__ == "__main__":
    print(f"[api] FoGuard API on http://127.0.0.1:{PORT}  (allowed origins: {allowed_origins})")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
