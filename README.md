# FoGuard

FoGuard watches a facility corridor for obstructions, keeps a verified graph of which corridors are passable, and has an AI agent reroute deliveries around problems without ever approving a path that doesn't exist.

**Dashboard:** [Dashboard Link](https://gargi-09.github.io/GraphGuard-MCP/) (after the GitHub Pages setup below). It lists every feature with a plain-English explanation and lets you try each one in the browser.

## What's in here

| Feature | What it does | Try it |
|---|---|---|
| Facility graph | Six waypoints (A–F) and eight two-way corridors in a Kùzu graph database | `python -m graph.init_db` |
| Route verifier | Rejects any route that jumps between unconnected waypoints or crosses a blocked corridor | `python -m graph.verifier` |
| Vision trigger | Detects an obstruction on corridor B ↔ C (webcam, mock, or a visual demo for presentations) and blocks it in the graph | `python -m vision.trigger_node --visual-demo` |
| MCP server | Gives AI agents (e.g. Claude Web) and the dashboard read-only tools for the graph and the verifier | `python mcp_server.py --http` |
| Recovery agent | Claude, through the Claude API, finds a verified reroute and drafts the notices | `python recovery_agent.py` |
| End-to-end demo | All of the above in one run, with every result checked, ending in a prompt for Fo | `python demo_run.py` |
| Dashboard | The page above; runs standalone on GitHub Pages, including the Claude agent and the camera feed | open `docs/index.html` |
| Local API server | Optional backend that lets the dashboard drive the real Python code | `python api_server.py` |

## Setup

Python 3.12 is required: Kùzu has no Windows build for Python 3.14.

```bash
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m graph.verifier
```

The last command builds the database at `data/kuzu_db` and runs the verifier checks. Keep the project **outside** OneDrive, Dropbox or other synced folders: sync clients corrupt the database, and `demo_run.py` refuses to run there.

Node.js is optional; it's only needed for `scripts/check_sim_parity.py`. ngrok is needed only to publish the MCP server (see below).

### Secrets and settings (`.env`, never committed)

Create `.env` in the project root:

```
ANTHROPIC_API_KEY=        # needed for the recovery agent
FACILITIES_CONTACT=       # optional: e.g. "email you@example.com", filled into the Fo prompt
RECIPIENT_CONTACT=        # optional: e.g. "text +15550000000", filled into the Fo prompt
DASHBOARD_ORIGIN=         # optional: extra browser origins allowed to call the MCP and API servers
MCP_PUBLIC_URL=           # optional: your public MCP URL, if not the default ngrok one
HF_TOKEN=                 # optional: Hugging Face token for the webcam models
```

`.gitignore` excludes `.env`, `.venv/`, `data/` and `CLAUDE.md`.

## End-to-end demo

```bash
python demo_run.py               # step 3 shows the simulated camera feed
python demo_run.py --no-visual   # no window: plain mock trigger
```

1. Reset the facility graph.
2. Find the normal route: A → B → C → F (30 m).
3. The vision trigger reports an obstruction on B ↔ C (the visual demo window, or the plain mock).
4. The verifier rejects the old route.
5. The shortest valid fallback: A → D → E → F (38 m).
6. With `ANTHROPIC_API_KEY` set, Claude finds the reroute itself, gets it approved by the verifier, and drafts the two notices.
7. A **"Paste into Claude Web"** prompt is printed for Fo to send the notices.

Every step's result is checked, and the run fails loudly if anything differs. Corridor B ↔ C stays blocked afterwards so the MCP server shows the incident; `python -m graph.init_db` clears it.

## The vision trigger

```bash
python -m vision.trigger_node --mock          # instant simulated obstruction
python -m vision.trigger_node --visual-demo   # presentation mode (below)
python -m vision.trigger_node --webcam        # live detection on a camera
```

`--webcam` runs real detection: it counts the corridor as obstructed when people cover more than 35% of the corridor region (DepthAR segmentor), or when more than 8% of the region differs from a captured picture of the empty corridor, for 3 readings in a row. It arms only after capturing the empty corridor, and waits for nobody to be in view before capturing. Tuning flags: `--camera`, `--person-threshold`, `--change-threshold`, `--verbose`.

### Presentation mode

`--visual-demo` shows what the edge node sees, for judges and audiences. A window titled *Edge Vision Node - Live Feed* shows the open corridor for 3 seconds, then the blocked corridor for 2 seconds; then the corridor is blocked in the graph and the incident and Claude Web prompt are printed. `demo_run.py` uses it for step 3.

Put the two images in `demo_assets/` first: `img_2` (open corridor) and `img_1` (blocked corridor), as `.jpg`, `.jpeg`, `.png` or `.webp`. **The feed is scripted: the images are shown in sequence, not analyzed**, and the obstruction always fires after the second one. Everything after that (graph, verifier, reroute, Claude agent) is computed for real. Without the images, or with `demo_run.py --no-visual`, step 3 uses the plain mock trigger.

## The dashboard

The dashboard is a static page in `docs/`. **It works on its own on GitHub Pages; nothing needs to run on your computer.** It has three modes, chosen at the top of the page:

- **Browser (default).** The facility graph, route verifier, shortest-route search, mock trigger and demo steps run as JavaScript in the page, generated from the Python seed data. `scripts/check_sim_parity.py` checks them against the real Python verifier for all 256 combinations of blocked corridors.
- **Live edge (MCP).** The page reads the real graph on the edge laptop straight from the MCP server's public ngrok URL, every 3 seconds, read-only. Run the trigger on the laptop (e.g. `python -m vision.trigger_node --visual-demo`) and the map, reroute and camera feed on the public site update by themselves. Route checks and the Claude agent then use the real graph. Needs `mcp_server.py --http` and ngrok running.
- **Local server (optional).** Drives the real Python code and Kùzu database through `api_server.py`: blocking corridors, resetting, and running the demo against the real database.

Panels worth knowing:

- **Edge Vision Node Feed.** Shows the corridor camera view. **Trigger Anomaly** swaps to the blocked corridor, blocks it in the graph, recalculates the route and generates the Claude Web prompt; the image always follows the graph's state for that corridor. The build script copies web-sized versions of the `demo_assets/` images into `docs/demo_assets/`, because GitHub Pages only serves `docs/`.
- **Claude recovery agent.** Paste an Anthropic API key and the page calls the Claude API directly (official SDK, `dangerouslyAllowBrowser`). The key goes only to `api.anthropic.com` and is kept only if you tick *Remember*, in that browser's local storage. Visitors without a key can click **Replay a recorded run** to watch a real run captured by `scripts/record_agent_run.py`. After a run, **Send with Fo in Claude Web** builds the paste-ready prompt from the contacts you type (never saved or published).
- **MCP server.** **Check public** and **Check local** talk to the MCP server straight from the page: they list its tools and read the live graph.

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

Feature descriptions live in `docs/features.json`. After changing it, `graph/init_db.py`, the agent's prompt, or the demo images:

```bash
python scripts/build_dashboard.py
python scripts/check_sim_parity.py
```

The first regenerates `docs/data.js` (features, graph seed data, the browser agent's prompt and tools, the recovery and Fo prompt templates, the MCP URLs, and the recorded run) and the web copies of the feed images in `docs/demo_assets/`; `--check` reports whether either is stale. The second confirms the in-browser simulation still matches Python. To refresh the recorded agent run, run `python scripts/record_agent_run.py` (spends a little API credit), then rebuild.

## Exposing the MCP server

```bash
python mcp_server.py --http --allowed-host YOUR-DOMAIN.ngrok-free.dev
ngrok http 8000 --url=https://YOUR-DOMAIN.ngrok-free.dev
```

Free ngrok accounts need ngrok 3.20 or newer; run `ngrok update` if it refuses to connect.

- **Claude Web:** Settings → Connectors → Add custom connector, with `https://YOUR-DOMAIN.ngrok-free.dev/mcp`.
- **The dashboard:** the server answers browser requests (CORS) from `https://gargi-09.github.io` and any localhost port. Add other origins with `DASHBOARD_ORIGIN` in `.env` or `--cors-origin ORIGIN`. The dashboard sends `ngrok-skip-browser-warning` with every request, so ngrok's free tier doesn't answer with its HTML warning page.

Both tools only read the graph, and the server is unauthenticated, so anyone with the URL can see the graph but not change it. Stop ngrok when you're not using it.

## Sending the notices with Fo

The recovery agent only drafts the notices. Sending happens in Claude Web, where Fo is connected and signed in:

1. Run `python demo_run.py` (or the agent on the dashboard). It ends with a **"Paste into Claude Web"** prompt containing the approved route, both drafted notices, and who gets which one.
2. Paste it into a Claude Web chat with the Fo connector enabled. Fo sends each notice to the contact listed for it.

Contacts come from `FACILITIES_CONTACT` / `RECIPIENT_CONTACT` in `.env` when run from the terminal, or from the two boxes on the dashboard (never saved or published). Write the channel first, e.g. `email you@example.com` or `text +15550000000`. A missing contact shows as "not provided", and Fo is told to ask you rather than guess.

## Project layout

| Path | Contents |
|---|---|
| `graph/` | Kùzu database (`init_db.py`), route verifier (`verifier.py`), and the shared agent tools (`tools.py`) |
| `vision/trigger_node.py` | Mock, visual-demo and webcam triggers |
| `core/` | DepthAR vision models (depth, segmentation, occlusion) used by the webcam trigger |
| `mcp_server.py` | MCP server for Claude Web and the dashboard |
| `recovery_agent.py` | Claude API recovery agent and the Fo prompt |
| `demo_run.py` | End-to-end demo |
| `api_server.py` | Optional local backend for the dashboard |
| `docs/` | The dashboard (GitHub Pages): `index.html`, `features.json`, generated `data.js`, feed images, recorded run |
| `demo_assets/` | Original images for the visual demo |
| `scripts/` | Dashboard build, simulation parity check, agent-run recorder |

## Known issues

- The visual demo and the dashboard's Trigger Anomaly button simulate the detection; only `--webcam` analyzes frames.
- `core/depth_estimator.py` documents depth as 0 = closest, but Depth Anything V2 returns 1 = closest, so `OcclusionReasoner`'s threshold selects the background. The trigger node doesn't use depth for its decision; `core/` itself is unchanged.
- `scripts/test_depth.py` imports modules that don't exist (`core.temporal_smoother`, `ar.*`).
- Fo can't be called from the Claude API (its MCP server rejects API connections without a token), so Fo is used from Claude Web through the paste-ready prompt instead.
