"""Claude API recovery agent: reroutes around an obstruction and drafts the notices.

    python recovery_agent.py        # runs against whatever the graph shows now

Claude gets the same two FoGuard tools the MCP server exposes, run locally.
It must get its route approved by propose_recovery_route (the RouteVerifier
guardrail) before stating it, then drafts the two notices. Sending is done by
Fo in Claude Web: build_fo_prompt() turns the result into a prompt to paste
there, with the contacts filled in from .env.

Configuration (.env in the project root):
    ANTHROPIC_API_KEY    required
    FACILITIES_CONTACT   optional, who Fo should notify about the blockage
    RECIPIENT_CONTACT    optional, the delivery recipient at the destination
"""
import json
import os

import anthropic
from anthropic import beta_tool
from dotenv import load_dotenv

from graph import tools

load_dotenv()

MODEL = "claude-opus-5-5"

# Schemas and descriptions come from the shared functions, so the agent and the
# MCP server describe the tools identically.
_TOPOLOGY_SPEC = beta_tool(tools.get_workspace_topology)
_ROUTE_SPEC = beta_tool(tools.propose_recovery_route)

SYSTEM_PROMPT = """You are FoGuard's incident-recovery agent for a facility's delivery robots. \
The facility is a set of waypoints joined by two-way corridors, and FoGuard's graph is the \
only source of truth about which corridors are passable.

When an obstruction is reported:
1. Call get_workspace_topology to see the current layout.
2. Work out the shortest route from the origin to the destination that avoids every blocked \
corridor and location, and submit it with propose_recovery_route. If it is rejected, read the \
reason, fix the path and propose again. Never state a route that propose_recovery_route has \
not approved.
3. Write two short notices: one asking the facilities team to clear the blocked corridor(s), \
and one telling the recipient at the destination that the delivery is being rerouted and may be \
delayed. Include the approved route and its distance.

If every route you can find is rejected and the graph shows no passable route at all, don't \
invent one: say so plainly, and write the notices instead as an urgent request to facilities \
naming the corridor(s) to clear to restore access, and a message to the recipient that the \
delivery is on hold."""

DRAFT_INSTRUCTIONS = """
4. No messaging tools are connected. Finish with the two notices as drafts under the headings \
"Facilities notice" and "Recipient notice"."""

# Claude's reasoning comes back as readable summaries (the raw chain of
# thought is never returned), so a run shows how Claude reached its answer.
THINKING = {"type": "adaptive", "display": "summarized"}

# The prompt pasted into Claude Web, where Fo sends the notices. The dashboard
# fills the same templates in the browser (exported through data.js).
FO_PROMPT_TEMPLATE = """FoGuard incident at {timestamp}: {corridor} obstructed. FoGuard's \
route verifier approved the reroute {route} ({distance} m) for the delivery from {origin} \
to {destination}.

Please use Fo to send the two notices drafted below:
1. The "Facilities notice" to the facilities team: {facilities}
2. The "Recipient notice" to the recipient at {destination}: {recipient}

Send each notice only to the contact listed for it, using the channel given (email, text or \
call). If a contact says "not provided", don't send that notice; ask me for the contact instead.

Drafted notices:

{notices}"""

# Used when no passable route exists: hold the delivery and escalate.
FO_ESCALATION_TEMPLATE = """FoGuard incident at {timestamp}: {corridor} obstructed, and FoGuard's \
route verifier found NO passable route from {origin} to {destination}. The delivery is on hold \
until a corridor is cleared.

Please use Fo to send the two notices drafted below, marking the facilities one urgent:
1. The "Facilities notice" to the facilities team: {facilities}
2. The "Recipient notice" to the recipient at {destination}: {recipient}

Send each notice only to the contact listed for it, using the channel given (email, text or \
call). If a contact says "not provided", don't send that notice; ask me for the contact instead.

Drafted notices:

{notices}"""

NOT_PROVIDED = "not provided"


def blocked_corridors(incident: dict) -> list[list[str]]:
    """An incident may name one blocked corridor or several."""
    return incident.get("blocked_corridors") or [incident["blocked_corridor"]]


def describe_corridors(incident: dict) -> str:
    pairs = [" <-> ".join(c) for c in blocked_corridors(incident)]
    return ("corridor " if len(pairs) == 1 else "corridors ") + " and ".join(pairs)


def browser_agent_config() -> dict:
    """Model, prompt and tool definitions for the dashboard's in-browser agent.

    Exported by scripts/build_dashboard.py so the browser runs the same agent
    and builds the same Fo prompt.
    """
    return {
        "model": MODEL,
        "system": SYSTEM_PROMPT + DRAFT_INSTRUCTIONS,
        "tools": [{"name": spec.name, "description": spec.description,
                   "input_schema": spec.input_schema, "strict": True}
                  for spec in (_TOPOLOGY_SPEC, _ROUTE_SPEC)],
        "thinking": THINKING,
        "fo_prompt_template": FO_PROMPT_TEMPLATE,
        "fo_escalation_template": FO_ESCALATION_TEMPLATE,
        "not_provided": NOT_PROVIDED,
    }


def build_fo_prompt(incident: dict, outcome: dict,
                    facilities: str | None = None, recipient: str | None = None) -> str:
    """The prompt to paste into Claude Web so Fo sends the drafted notices.

    Contacts default to FACILITIES_CONTACT / RECIPIENT_CONTACT from .env. With
    no approved route it's the hold-and-escalate version instead.
    """
    route = outcome["approved_route"]
    values = dict(
        timestamp=incident["timestamp"],
        corridor=describe_corridors(incident),
        origin=incident["origin"],
        destination=incident["destination"],
        facilities=facilities or os.getenv("FACILITIES_CONTACT") or NOT_PROVIDED,
        recipient=recipient or os.getenv("RECIPIENT_CONTACT") or NOT_PROVIDED,
        notices=outcome["final_text"],
    )
    if not route:
        return FO_ESCALATION_TEMPLATE.format(**values)
    return FO_PROMPT_TEMPLATE.format(route=" -> ".join(route["route"]),
                                     distance=f"{route['total_distance_m']:g}", **values)


def run_recovery(incident: dict, verbose: bool = True) -> dict:
    """Have Claude find and verify a reroute for the incident and draft the notices.

    Returns the route the verifier actually approved, taken from the tool calls
    rather than from Claude's text, Claude's final message (the drafts), and a
    transcript: Claude's summarized reasoning, each tool call and each result,
    in order.
    """
    log = print if verbose else (lambda *a, **k: None)
    approved = {}
    proposals = []
    transcript = []

    @beta_tool(name=_TOPOLOGY_SPEC.name, description=_TOPOLOGY_SPEC.description,
               input_schema=_TOPOLOGY_SPEC.input_schema, strict=True)
    def get_workspace_topology() -> str:
        result = tools.get_workspace_topology()
        summary = f"blocked corridors {result['blocked_corridors']}"
        log(f"  <- topology: {summary}")
        transcript.append({"type": "tool_result", "tool": "get_workspace_topology", "summary": summary})
        return json.dumps(result)

    @beta_tool(name=_ROUTE_SPEC.name, description=_ROUTE_SPEC.description,
               input_schema=_ROUTE_SPEC.input_schema, strict=True)
    def propose_recovery_route(origin: str, destination: str, proposed_path: list[str]) -> str:
        result = tools.propose_recovery_route(origin, destination, proposed_path)
        proposals.append({"path": proposed_path, **result})
        if result["approved"]:
            approved.update(result)
            summary = f"APPROVED {'-'.join(result['route'])} ({result['total_distance_m']:g} m)"
        else:
            summary = f"REJECTED {'-'.join(proposed_path)}: {result['reason']}"
        log(f"  <- {summary}")
        transcript.append({"type": "tool_result", "tool": "propose_recovery_route",
                           "approved": result["approved"], "summary": summary})
        return json.dumps(result)

    client = anthropic.Anthropic()
    log(f"[agent] {MODEL} handling the incident (notices will be drafts for Fo)")

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT + DRAFT_INSTRUCTIONS,
        tools=[get_workspace_topology, propose_recovery_route],
        messages=[{"role": "user", "content":
                   f"FoGuard incident report:\n{json.dumps(incident, indent=2)}"}],
        thinking=THINKING,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    last = None
    for message in runner:
        last = message
        for block in message.content:
            if block.type == "thinking" and block.thinking.strip():
                text = block.thinking.strip()
                log("  [Claude's reasoning] " + text.replace("\n", "\n      "))
                transcript.append({"type": "thinking", "text": text})
            elif block.type == "tool_use":
                call = f"{block.name}({json.dumps(block.input) if block.input else ''})"
                log(f"  -> {call}")
                transcript.append({"type": "tool_use", "tool": block.name, "input": block.input or {}})

    if last is None:
        raise RuntimeError("The recovery agent returned no response.")
    if last.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request: {last.stop_details}")

    final_text = "\n".join(b.text for b in last.content if b.type == "text").strip()
    return {
        "approved_route": approved or None,
        "proposals": proposals,
        "transcript": transcript,
        "final_text": final_text,
    }


if __name__ == "__main__":
    import sys
    from datetime import datetime, timezone

    sys.stdout.reconfigure(errors="replace")  # cp1252 consoles can't print every reply

    incident = {
        "event": "OBSTRUCTION_DETECTED",
        "blocked_corridor": ["B", "C"],
        "origin": "A",
        "destination": "F",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    outcome = run_recovery(incident)
    print("\n" + outcome["final_text"])
    if outcome["approved_route"]:
        print("\n--- Paste into Claude Web (Fo connector on) ---\n")
        print(build_fo_prompt(incident, outcome))
