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
Both tools only read the graph, so allowing a browser origin exposes nothing
that Claude Web can't already see.
"""
import argparse
import os
import sys

import uvicorn
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from graph.init_db import cloud_sync_folder, DB_PATH
from graph.tools import get_workspace_topology, propose_recovery_route

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


def main():
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
