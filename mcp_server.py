"""FoGuard MCP server: lets an agent read the facility graph and get routes verified.

    python mcp_server.py                                   # stdio (Claude Desktop/Code)
    python mcp_server.py --http                            # http://127.0.0.1:8000/mcp
    python mcp_server.py --http --allowed-host abc.ngrok-free.app   # behind ngrok

Every route an agent proposes goes through RouteVerifier, so the agent can't
approve a path that skips a corridor or crosses a blocked one; it gets the
exact failure reason back and can retry.

Over HTTP the server also answers browser requests (CORS) from the GitHub
Pages dashboard and localhost, so the dashboard can call the tools directly.
Extra origins: DASHBOARD_ORIGIN in .env (comma-separated) or --cors-origin.
Both MCP tools only read the graph. Three plain HTTP routes serve the dashboard:
    GET  /run_diagnostics   the routing suite (tests/test_routing.py), on a throwaway graph
    POST /trigger_anomaly   block the monitored corridor, as the edge node would
    POST /clear_anomaly     clear it again
Those two writes are the only changes possible through the public URL.
"""
import argparse
import contextlib
import io
import os
import sys

import anyio
import uvicorn
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from graph import tools
from graph.init_db import cloud_sync_folder, DB_PATH
from graph.tools import get_workspace_topology, propose_recovery_route
from tests.test_routing import run_all_tests
from vision.trigger_node import MONITORED_CORRIDOR, build_recovery_prompt, report_obstruction

load_dotenv()

# The dashboard's GitHub Pages site (github.com/gargi-09/GraphGuard-MCP).
DEFAULT_DASHBOARD_ORIGINS = ["https://gargi-09.github.io"]
# Where the server is published through ngrok; the dashboard calls it here.
PUBLIC_MCP_URL = os.getenv("MCP_PUBLIC_URL", "https://catalog-jimmy-canyon.ngrok-free.dev/mcp")
LOCAL_MCP_URL = "http://127.0.0.1:8000/mcp"
# Any localhost port, for the dashboard served locally while testing.
LOCAL_ORIGIN_REGEX = r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?"

server = MCPServer(
    name="foguard",
    instructions=(
        "FoGuard tracks a facility's waypoints (A-F) and the corridors between them. "
        "Call get_workspace_topology to see the layout and which corridors are blocked, "
        "then call propose_recovery_route with a full path. Only report a route to the "
        "user once propose_recovery_route has approved it; if it is rejected, read the "
        "reason, fix the path and propose again."
    ),
)

# The tool logic lives in graph/tools.py so the Claude API agent
# (vision/recovery_agent.py) runs exactly the same checks.
_read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
server.tool(annotations=_read_only)(get_workspace_topology)
server.tool(annotations=_read_only)(propose_recovery_route)


# Plain HTTP routes next to /mcp, for the dashboard (not MCP tools, so Claude
# Web never sees them). The blocking work runs in a thread so MCP calls on the
# event loop aren't held up.

async def _json_from_thread(work) -> JSONResponse:
    """Run blocking work in a thread and always answer with JSON.

    An uncaught exception would become Starlette's plain-text 500, sent without
    CORS headers, so the browser would report a network failure instead of the
    real cause (e.g. the database locked by another process). The dashboard
    shows the "detail" field of an error response.
    """
    try:
        return JSONResponse(await anyio.to_thread.run_sync(work))
    except Exception as e:
        print(f"[mcp_server] {work.__name__} failed: {e!r}", file=sys.stderr)
        return JSONResponse({"detail": str(e) or type(e).__name__}, status_code=500)


@server.custom_route("/run_diagnostics", methods=["GET"])
async def run_diagnostics(request: Request) -> JSONResponse:
    """The 5-scenario routing suite from tests/test_routing.py, on a throwaway graph."""
    return await _json_from_thread(run_all_tests)


def _trigger_anomaly() -> dict:
    """Block the monitored corridor and return the incident, reroute and agent prompt."""
    with tools.graph_lock, contextlib.redirect_stdout(io.StringIO()):
        incident = report_obstruction(print_card=False)
    reroute = tools.shortest_route(incident["origin"], incident["destination"])
    return {"incident": incident, "reroute": reroute,
            "prompt": build_recovery_prompt(incident, reroute),
            "topology": get_workspace_topology()}


# The only two writes reachable over the public URL, deliberately narrow:
# block or clear the monitored corridor, exactly what the edge node itself does.
@server.custom_route("/trigger_anomaly", methods=["POST"])
async def trigger_anomaly(request: Request) -> JSONResponse:
    """Simulate the edge node's obstruction report on the monitored corridor."""
    return await _json_from_thread(_trigger_anomaly)


@server.custom_route("/clear_anomaly", methods=["POST"])
async def clear_anomaly(request: Request) -> JSONResponse:
    """Clear the monitored corridor again."""
    def clear():
        tools.set_corridor_blocked(*MONITORED_CORRIDOR, False)
        return {"topology": get_workspace_topology()}
    return await _json_from_thread(clear)


def main():
    """Parse the CLI flags and serve over stdio, or HTTP with CORS when --http is given."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--http", action="store_true",
                        help="serve streamable HTTP at /mcp instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    parser.add_argument("--allowed-host", action="append", default=[], metavar="HOST",
                        help="extra Host header to accept, e.g. your ngrok domain "
                             "(repeatable). Localhost is always accepted.")
    parser.add_argument("--cors-origin", action="append", default=[], metavar="ORIGIN",
                        help="extra browser origin allowed to call the server, e.g. "
                             "https://you.github.io (repeatable)")
    args = parser.parse_args()

    sync = cloud_sync_folder()
    if sync:
        print(f"[mcp_server] Warning: {DB_PATH} is inside {sync}; sync clients can "
              f"corrupt the Kuzu database.", file=sys.stderr)

    if not args.http:
        server.run()
        return

    dashboard_origins = sorted({
        *DEFAULT_DASHBOARD_ORIGINS, *args.cors_origin,
        *(o.strip().rstrip("/") for o in os.getenv("DASHBOARD_ORIGIN", "").split(",") if o.strip()),
    })

    # DNS-rebinding protection stays on: only localhost and the hosts named
    # here (the ngrok domain, which ngrok forwards as the Host header) get in.
    # It checks the Origin header too, so the dashboard origins must be listed
    # here as well as in CORS below, or browser calls pass CORS and then 403.
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *args.allowed_host],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         *(f"https://{host}" for host in args.allowed_host),
                         *dashboard_origins],
    )
    app = server.streamable_http_app(transport_security=security, host=args.host)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=dashboard_origins,
        allow_origin_regex=LOCAL_ORIGIN_REGEX,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        # ngrok-skip-browser-warning: the dashboard sends it so ngrok's free
        # tier doesn't answer with its HTML interstitial instead of JSON.
        allow_headers=["Content-Type", "Accept", "Mcp-Session-Id", "Mcp-Protocol-Version",
                       "Last-Event-ID", "ngrok-skip-browser-warning"],
        expose_headers=["Mcp-Session-Id"],
        # Chrome asks before a public site (github.io) may call a server on this
        # machine (the dashboard's "Check local" button); this is the opt-in.
        allow_private_network=True,
    )
    print(f"[mcp_server] Serving http://{args.host}:{args.port}/mcp", file=sys.stderr)
    print(f"[mcp_server] Browser access allowed from localhost and: {', '.join(dashboard_origins)}",
          file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
