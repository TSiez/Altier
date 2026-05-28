# Altier

Two self-contained web apps from the Bystander Studio toolkit:

| Folder | App | What it does |
|---|---|---|
| [`frontend/`](frontend) | **Concept Archive** | An AI design-concept generator. One brief → six homepage concepts × two plates each, rendered live by [kie.ai](https://kie.ai) (`google/nano-banana`). Sessions persist in `localStorage`; any session can be exported as a standalone HTML file. |
| [`trend-finder/`](trend-finder) | **Trend Finder** | A live news/Reddit/Hacker News scraper with a Gemini-powered "extract ideas" step. Pulls top-5 trending stories for any topic, lets you tick the ones you want, then pulls the ranked list of ideas out of each article. |

Both are dependency-light: hand-written HTML/CSS/JS, with small Python/Flask + Node servers where needed.

---

## frontend/ — Concept Archive

**Open** [`frontend/Frontend.html`](frontend/Frontend.html) directly in a browser.

Before it can generate, paste a kie.ai API key into the `DEFAULT_KEY` constant near the top of the `<script>` block (or set it via the in-page key field if you add one). The page never sends the key anywhere except `api.kie.ai`.

```js
const DEFAULT_KEY = "your-kie-ai-key-here";
```

Files of interest:
- [`Frontend.html`](frontend/Frontend.html) — the generator UI.
- [`Demo.html`](frontend/Demo.html) — a finished session ("Hours") shown as a polished gallery.
- [`demo-prompts.json`](frontend/demo-prompts.json) — the brief used to seed the Hours demo.
- [`demo-gen.ps1`](frontend/demo-gen.ps1) — PowerShell helper that re-generates the Hours demo. Reads `KIE_API_KEY` from the environment:
  ```powershell
  $env:KIE_API_KEY = 'your-kie-ai-key-here'
  ./demo-gen.ps1
  ```

---

## trend-finder/ — Trend Finder

A two-process app:
- **Flask** on `:5001` — `/api/trending`, `/api/extract`, `/api/health`, and an `/img` thumbnail proxy.
- **Node** on `:3002` (optional) — same-origin static server that proxies the Gemini-backed extract endpoint, so the Gemini key stays server-side.

### Setup

1. Install Python deps:
   ```bash
   cd trend-finder
   pip install -r requirements.txt
   ```
2. Create a `.env` next to `server.py` with your Gemini key:
   ```
   GEMINI_API_KEY=your-gemini-key-here
   # GEMINI_MODEL=gemini-flash-latest   # optional override
   ```
3. Start Flask:
   ```bash
   python server.py
   ```
4. (Optional) Start the Node static/proxy server:
   ```bash
   node serve.mjs
   ```
5. Open `Trend Finder.html` directly, or browse to `http://localhost:3002/Trend Finder.html` if you started the Node server.

### How it works

- Type a topic → `GET /api/trending?q=…` aggregates Google News RSS, Reddit and Hacker News into a top-5 list.
- Each story gets a thumbnail (Google News RSS, Reddit preview, or `og:image` fetched from the destination), all proxied through `/img` so hotlink-blocked sources still render.
- Hit **Extract Ideas** on any story (or tick several and press **Extract from selected**) to send the article to `/api/extract` → Gemini reads it and pulls out the ranked list of ideas, which appear in the right-hand "Selected Ideas" panel.
- Tick the ideas you want to keep, then **Preview** or **Export CSV**.

---

## Deploy to Render (one-click via Blueprint)

A [`render.yaml`](render.yaml) at the repo root makes the Trend Finder one click away:

1. Push this repo to GitHub.
2. In Render, click **New + → Blueprint** and pick this repo. Render reads `render.yaml` and stages a single Web Service: **`altier-trend-finder`**, running Flask under gunicorn, serving both the static page and the API from the same origin.
3. When prompted, paste your `GEMINI_API_KEY` (it's marked `sync: false` so it's set per-environment, never committed). `GEMINI_MODEL` defaults to `gemini-flash-latest`.
4. Deploy. The first request after 15 min of idle on the **free** plan wakes the dyno (~30–60 s cold start) — change `plan: free` → `plan: starter` in `render.yaml` (or in the dashboard) for $7/mo and it stays warm 24/7.

The blueprint sets:
- `rootDir: trend-finder` so the build/run happens inside that folder
- `buildCommand: pip install -r requirements.txt` (includes `gunicorn`)
- `startCommand: gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --access-logfile - server:app`
- `healthCheckPath: /api/health` so Render knows the service is up

The frontend (Concept Archive) is pure static and is happiest on Vercel (free, edge-CDN, no server). Drop `frontend/` into a Vercel project as static output and you're done.

---

## Notes

- `.env` files are gitignored — set the keys yourself per the steps above.
- The PowerShell helpers in `frontend/` are Windows-friendly; the rest is OS-agnostic.
- Both apps are designed to be self-contained: a single HTML file + a tiny server where one's needed.
