# FoGuard

FoGuard watches a facility corridor for obstructions, keeps a verified graph of which corridors are passable, and has an AI agent reroute deliveries around problems without ever approving a path that doesn't exist.

**Dashboard:** `https://YOUR-USERNAME.github.io/GraphGuard-MCP/` (after the GitHub Pages setup below). It lists every feature with a plain-English explanation and lets you try each one in the browser.

## What's in here

| Feature | What it does | Try it |
|---|---|---|
| Facility graph | Six waypoints (A–F) and eight two-way corridors in a Kùzu graph database | `python -m graph.init_db` |
| Route verifier | Rejects any route that jumps between unconnected waypoints or crosses a blocked corridor | `python -m graph.verifier` |
| Vision trigger | Detects an obstruction on corridor B ↔ C (webcam, mock, or a visual demo for presentations) and blocks it in the graph | `python -m vision.trigger_node --visual-demo` |
| MCP server | Gives AI agents (e.g. Claude Web) read-only tools for the graph and the verifier | `python mcp_server.py` |
| Recovery agent | Claude, through the Claude API, finds a verified reroute and drafts the notices | `python recovery_agent.py` |
| End-to-end demo | All of the above in one run, with every result checked | `python demo_run.py` |
| Dashboard | The page above; runs standalone on GitHub Pages, including the Claude agent | open `docs/index.html` |

## Setup

Python 3.12 is required: Kùzu has no Windows build for Python 3.14.

```bash
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m graph.verifier
```

The last command builds the database at `data/kuzu_db` and runs the verifier checks. Keep the project **outside** OneDrive, Dropbox or other synced folders: sync clients corrupt the database, and `demo_run.py` refuses to run there.

### Secrets (`.env`, never committed)

Create `.env` in the project root:

```
ANTHROPIC_API_KEY=        # needed for the recovery agent
FACILITIES_CONTACT=       # optional: e.g. "email you@example.com", filled into the Fo prompt
RECIPIENT_CONTACT=        # optional: e.g. "text +15550000000", filled into the Fo prompt
DASHBOARD_ORIGIN=         # optional: your GitHub Pages origin, see below
```

`.gitignore` already excludes `.env`, `.venv/` and `data/`.

## The dashboard

The dashboard is a static page in `docs/`. **It works on its own on GitHub Pages; nothing needs to run on your computer.**

- **Browser mode (default).** The facility graph, route verifier, shortest-route search, mock trigger and demo steps run as JavaScript in the page, generated from the Python seed data. `scripts/check_sim_parity.py` checks them against the real Python verifier for all 256 combinations of blocked corridors.
- **Claude recovery agent, in the page.** Paste an Anthropic API key and the page calls the Claude API directly (official SDK, `dangerouslyAllowBrowser`), with Claude's tools running against the in-page graph. The key goes only to `api.anthropic.com` and is kept only if you tick *Remember*, in that browser's local storage. Visitors without a key can click **Replay a recorded run** to watch a real run captured by `scripts/record_agent_run.py`.
- **Local server mode (optional).** Drives the real Python code and Kùzu database through `api_server.py`. It's only needed to check the MCP server or to attach Fo.

### Deploy to GitHub Pages

1. Push this repository to GitHub.
2. Go to **Settings → Pages**, set **Source** to *Deploy from a branch*, choose branch `main` and folder `/docs`, and save.
3. After a minute the dashboard is live at `https://YOUR-USERNAME.github.io/YOUR-REPO/`.

### Run it locally (optional)

```bash
python api_server.py
```

Then open http://127.0.0.1:8001. Served from there, the page starts in Local server mode.

To drive your local server from the GitHub Pages site, add `DASHBOARD_ORIGIN=https://YOUR-USERNAME.github.io` to `.env`, restart `api_server.py`, and choose **Local server** on the site (Chrome may ask to allow access to your local network). The API server only listens on `127.0.0.1` and only accepts browser requests from localhost and the origins in `DASHBOARD_ORIGIN`; it can change the graph and spend API credit, so only list sites you control.

## Keeping the dashboard current

Feature descriptions live in `docs/features.json`. After changing it, or anything in `graph/init_db.py`:

```bash
python scripts/build_dashboard.py
python scripts/check_sim_parity.py
```

The first regenerates `docs/data.js` (features, graph seed data, the browser agent's prompt and tools, and the recorded run); `--check` reports whether it's stale. The second confirms the in-browser simulation still matches Python (needs Node.js). To refresh the recorded agent run, run `python scripts/record_agent_run.py` (spends a little API credit), then rebuild.

## Presentation mode

For judges and audiences, `python -m vision.trigger_node --visual-demo` shows what the edge node sees: a window titled *Edge Vision Node - Live Feed* shows the open corridor for 3 seconds, then the blocked corridor for 2 seconds, then the corridor is blocked in the graph and the incident and Claude Web prompt are printed. `python demo_run.py` uses it for step 3 automatically.

Put the two images in `demo_assets/` first (filenames in `demo_assets/README.md`). The feed is simulated from those stills; they aren't analyzed. Without them, or with `python demo_run.py --no-visual`, step 3 uses the plain mock trigger.

## Sending the notices with Fo

The recovery agent only drafts the notices. Sending happens in Claude Web, where Fo is connected and signed in:

1. Run `python demo_run.py` (or the agent on the dashboard). It ends with a **"Paste into Claude Web"** prompt containing the approved route, both drafted notices, and who gets which one.
2. Paste it into a Claude Web chat with the Fo connector enabled. Fo sends each notice to the contact listed for it.

Contacts come from `FACILITIES_CONTACT` / `RECIPIENT_CONTACT` in `.env` when run from the terminal, or from the two boxes on the dashboard (never saved or published). Write the channel first, e.g. `email you@example.com` or `text +15550000000`. A missing contact shows as "not provided", and Fo is told to ask you rather than guess.

## Exposing the MCP server to Claude Web

```bash
python mcp_server.py --http --allowed-host YOUR-DOMAIN.ngrok-free.dev
ngrok http 8000 --url=https://YOUR-DOMAIN.ngrok-free.dev
```

Then in Claude Web: **Settings → Connectors → Add custom connector** with `https://YOUR-DOMAIN.ngrok-free.dev/mcp`. The server is read-only and unauthenticated; stop ngrok when you're not using it.

## Known issues

- `core/depth_estimator.py` documents depth as 0 = closest, but Depth Anything V2 returns 1 = closest, so `OcclusionReasoner`'s threshold selects the background. The trigger node works around it; `core/` itself is unchanged.
- `scripts/test_depth.py` imports modules that don't exist (`core.temporal_smoother`, `ar.*`).
- Fo can't be called from the Claude API (its MCP server rejects API connections without a token), so Fo is used from Claude Web through the paste-ready prompt instead.
