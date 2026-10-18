"""FoGuard MCP server: lets an agent read the facility graph and get routes verified.

    python mcp_server.py                                   # stdio (Claude Desktop/Code)
    python mcp_server.py --http                            # http://127.0.0.1:8000/mcp
    python mcp_server.py --http --allowed-host abc.ngrok-free.app   # behind ngrok

Every route an agent proposes goes through RouteVerifier, so the agent can't
approve a path that skips a corridor or crosses a blocked one; it gets the
exact failure reason back and can retry.
"""
import argparse
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from graph.init_db import cloud_sync_folder, DB_PATH
from graph.tools import get_workspace_topology, propose_recovery_route

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
    args = parser.parse_args()

    sync = cloud_sync_folder()
    if sync:
        print(f"[mcp_server] Warning: {DB_PATH} is inside {sync}; sync clients can "
              f"corrupt the Kuzu database.", file=sys.stderr)

    if not args.http:
        server.run()
        return

    # DNS-rebinding protection stays on: only localhost and the hosts named
    # here (the ngrok domain, which ngrok forwards as the Host header) get in.
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *args.allowed_host],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         *(f"https://{host}" for host in args.allowed_host)],
    )
    print(f"[mcp_server] Serving http://{args.host}:{args.port}/mcp", file=sys.stderr)
    server.run("streamable-http", host=args.host, port=args.port, transport_security=security)


if __name__ == "__main__":
    main()
