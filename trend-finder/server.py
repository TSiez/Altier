"""
Trend Finder — Python Flask backend (Server 2).

Runs on http://localhost:5001 and handles ALL scraping + extraction.

Endpoints
  GET /api/health                 -> { ok, service, gemini }
  GET /api/trending?q=<topic>     -> top 5 results from Google News RSS,
                                     Reddit and Hacker News combined.
                                     Each: { rank, title, link, source, thumbnail }
  GET /api/extract?url=<article>  -> { title, url, items:[{rank, idea}], method }
                                     Gemini Flash is the PRIMARY extractor;
                                     HTML pattern matching is a fallback only.
                                     Google News redirect links return
                                     { needs_redirect: true, open_url, ... }

The Gemini API key is read from a .env file (GEMINI_API_KEY=...) that sits
next to this file. It is never hardcoded and never sent to the browser — only
this server talks to Gemini.

Run:
    pip install -r requirements.txt
    python server.py            # serves on :5001

The Node server (serve.mjs, :3002) serves the HTML, proxies thumbnails and
proxies /api/* calls to this server.
"""

import os
import re
import json
import time
import html
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
import concurrent.futures

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_from_directory, abort, Response
from flask_cors import CORS


ROOT = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# Config / secrets
# --------------------------------------------------------------------------

def _load_env(path: str) -> None:
    """Tiny .env loader (no python-dotenv dependency)."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            # don't clobber a real environment variable, but DO fill in empties
            # (so a leftover `set GEMINI_API_KEY=` in the shell doesn't disable Gemini)
            if not os.environ.get(key):
                os.environ[key] = val


_load_env(os.path.join(ROOT, ".env"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest").strip()
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 12


app = Flask(__name__, static_folder=None)
CORS(app, resources={r"/api/*": {"origins": "*"}})


@app.after_request
def _no_cache_html(resp):
    # never cache the HTML so the latest UI is always served
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _get(url: str, **kw) -> requests.Response:
    kw.setdefault("headers", HEADERS)
    kw.setdefault("timeout", REQUEST_TIMEOUT)
    return requests.get(url, **kw)


def _is_google_news(url: str) -> bool:
    try:
        return "news.google.com" in urllib.parse.urlparse(url).netloc
    except Exception:
        return False


def fetch_og_image(url: str) -> str:
    """Best-effort: pull an article's hero photo (og:image / twitter:image)."""
    try:
        r = _get(url, timeout=6)
        if not r.ok:
            return ""
        soup = BeautifulSoup(r.text[:200000], "html.parser")
        meta = (soup.find("meta", property="og:image")
                or soup.find("meta", attrs={"name": "og:image"})
                or soup.find("meta", attrs={"property": "og:image:url"})
                or soup.find("meta", attrs={"name": "twitter:image"})
                or soup.find("meta", attrs={"property": "twitter:image"}))
        img = (meta.get("content") or "").strip() if meta else ""
        return urllib.parse.urljoin(url, img) if img else ""
    except Exception:
        return ""


def enrich_thumbnails(results: list) -> None:
    """Fill in real article photos in parallel. Reddit previews are kept;
    Google News redirect links are skipped (their article can't be fetched)."""
    def _one(item):
        if item.get("thumbnail") or _is_google_news(item["link"]):
            return
        item["thumbnail"] = fetch_og_image(item["link"])

    if results:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            list(ex.map(_one, results))


# --------------------------------------------------------------------------
# Trending sources
# --------------------------------------------------------------------------

def src_google_news(q: str, limit: int = 6) -> list:
    """Google News RSS search."""
    out = []
    try:
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(q)
            + "&hl=en-US&gl=US&ceid=US:en"
        )
        resp = _get(url)
        root = ET.fromstring(resp.content)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            source_el = item.find("source")
            source = (source_el.text.strip() if source_el is not None and source_el.text
                      else "Google News")
            # GN RSS carries no reliable image; the UI shows a letter tile.
            if title and link:
                out.append({
                    "title": html.unescape(title),
                    "link": link,
                    "source": html.unescape(source),
                    "thumbnail": "",
                })
            if len(out) >= limit:
                break
    except Exception:
        traceback.print_exc()
    return out


def src_reddit(q: str, limit: int = 6) -> list:
    """Reddit search JSON."""
    out = []
    try:
        url = ("https://www.reddit.com/search.json?q="
               + urllib.parse.quote(q) + f"&sort=relevance&limit={limit*2}&raw_json=1")
        resp = _get(url, headers={**HEADERS, "Accept": "application/json"})
        data = resp.json()
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            title = (d.get("title") or "").strip()
            permalink = d.get("permalink")
            if not title or not permalink:
                continue
            link = "https://www.reddit.com" + permalink
            thumb = d.get("thumbnail") or ""
            if thumb in ("self", "default", "nsfw", "spoiler", "image", ""):
                # prefer a real preview image if present, else leave blank (letter tile)
                try:
                    thumb = (d["preview"]["images"][0]["source"]["url"]
                             .replace("&amp;", "&"))
                except Exception:
                    thumb = ""
            out.append({
                "title": title,
                "link": link,
                "source": "r/" + (d.get("subreddit") or "reddit"),
                "thumbnail": thumb,
            })
            if len(out) >= limit:
                break
    except Exception:
        traceback.print_exc()
    return out


def src_hackernews(q: str, limit: int = 6) -> list:
    """Hacker News via the Algolia search API."""
    out = []
    try:
        url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage="
               + str(limit * 2) + "&query=" + urllib.parse.quote(q))
        resp = _get(url, headers={**HEADERS, "Accept": "application/json"})
        for hit in resp.json().get("hits", []):
            title = (hit.get("title") or "").strip()
            if not title:
                continue
            link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            out.append({
                "title": title,
                "link": link,
                "source": "Hacker News",
                "thumbnail": "",
            })
            if len(out) >= limit:
                break
    except Exception:
        traceback.print_exc()
    return out


def find_trending(q: str, top_n: int = 5) -> list:
    """Run the three sources in parallel, then interleave for diversity."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_gn = ex.submit(src_google_news, q)
        f_rd = ex.submit(src_reddit, q)
        f_hn = ex.submit(src_hackernews, q)
        buckets = [f_gn.result(), f_rd.result(), f_hn.result()]

    # round-robin so the top 5 isn't dominated by one source
    merged, seen = [], set()
    for i in range(max((len(b) for b in buckets), default=0)):
        for b in buckets:
            if i < len(b):
                item = b[i]
                key = item["link"]
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)

    results = merged[:top_n]
    for idx, item in enumerate(results, start=1):
        item["rank"] = idx
    enrich_thumbnails(results)   # fetch real article photos (parallel)
    return results


# --------------------------------------------------------------------------
# Article fetch + extraction
# --------------------------------------------------------------------------

def fetch_article(url: str):
    """Return (title, clean_text, raw_soup) for an article URL."""
    resp = _get(url, allow_redirects=True)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # title
    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else url

    # remove chrome before pulling text
    container = soup.find("article") or soup.find("main") or soup.body or soup
    work = BeautifulSoup(str(container), "html.parser")
    for tag in work(["script", "style", "nav", "aside", "footer", "header",
                     "form", "noscript", "iframe"]):
        tag.decompose()

    text = work.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)
    return title, text, soup


def gemini_extract(title: str, text: str) -> list:
    """PRIMARY extractor. Returns [{rank, idea}] or raises on failure."""
    if not GEMINI_API_KEY:
        raise RuntimeError("no GEMINI_API_KEY configured")

    prompt = (
        "You are given the full text of a web article. Identify the ACTUAL ranked "
        "list of products or ideas that the article is about (for example a listicle "
        "like '10 best ...', 'Top 7 ...', or a numbered roundup).\n"
        "Rules:\n"
        "- Ignore navigation menus, sidebars, ads, cookie notices, newsletter prompts, "
        "related-article links, author bios and comments.\n"
        "- Preserve the article's own order / ranking.\n"
        "- Each 'idea' should be a concise product or idea name (you may add a short "
        "clarifying phrase, but keep it tight).\n"
        "- If there is no explicit list, return the article's main points/takeaways in order.\n"
        "Respond ONLY with JSON of the form: "
        '{"title": string, "items": [{"rank": integer, "idea": string}]}.\n\n'
        f"ARTICLE TITLE: {title}\n\nARTICLE TEXT:\n{text[:14000]}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(
        GEMINI_URL, params={"key": GEMINI_API_KEY}, json=body, timeout=60
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(raw)
    items = parsed.get("items") if isinstance(parsed, dict) else parsed
    out = []
    for i, it in enumerate(items or [], start=1):
        if isinstance(it, dict):
            idea = (it.get("idea") or it.get("text") or it.get("name") or "").strip()
            rank = it.get("rank") or i
        else:
            idea, rank = str(it).strip(), i
        if idea:
            out.append({"rank": int(rank), "idea": idea})
    if not out:
        raise RuntimeError("gemini returned no usable items")
    return out


def fallback_extract(soup: BeautifulSoup, text: str) -> list:
    """HTML pattern-matching fallback (only used if Gemini is unavailable)."""
    items = []

    # 1) the longest <ol> on the page is usually the listicle
    best_ol = None
    for ol in soup.find_all("ol"):
        lis = ol.find_all("li", recursive=False)
        if best_ol is None or len(lis) > len(best_ol.find_all("li", recursive=False)):
            best_ol = ol
    if best_ol:
        for li in best_ol.find_all("li", recursive=False):
            t = li.get_text(" ", strip=True)
            # skip footnote / citation lists (e.g. Wikipedia references)
            if t.startswith("^") or re.match(r"^(Jump up|\d{4}[\.\)])", t):
                continue
            if 3 <= len(t) <= 240:
                items.append(t)

    # 2) numbered headings: "1. Thing", "2) Thing"
    if not items:
        for h in soup.find_all(["h2", "h3"]):
            t = h.get_text(" ", strip=True)
            m = re.match(r"^\s*(\d{1,2})[\.\)\:]\s*(.+)$", t)
            if m and 3 <= len(m.group(2)) <= 240:
                items.append(m.group(2).strip())

    # 3) numbered lines in the plain text
    if not items:
        for line in text.split("\n"):
            m = re.match(r"^\s*(\d{1,2})[\.\)\:]\s+(.{3,240})$", line.strip())
            if m:
                items.append(m.group(2).strip())

    # de-dupe, keep order, cap
    seen, clean = set(), []
    for t in items:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            clean.append(t)
    return [{"rank": i, "idea": t} for i, t in enumerate(clean[:30], start=1)]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "service": "trend-finder",
        "gemini": bool(GEMINI_API_KEY),
        "model": GEMINI_MODEL,
    })


@app.route("/api/trending")
def api_trending():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "missing query parameter ?q="}), 400
    if len(q) > 200:
        return jsonify({"error": "query too long"}), 400
    started = time.time()
    try:
        results = find_trending(q, top_n=5)
        return jsonify({
            "query": q,
            "count": len(results),
            "duration_ms": int((time.time() - started) * 1000),
            "results": results,
        })
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": f"trending failed: {exc}"}), 500


@app.route("/api/extract")
def api_extract():
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing query parameter ?url="}), 400
    if not re.match(r"^https?://", url):
        return jsonify({"error": "url must start with http(s)://"}), 400

    # Google News links are JS redirects a server can't follow — ask the user
    # to open it in their browser and paste back the final article URL.
    if _is_google_news(url):
        return jsonify({
            "needs_redirect": True,
            "open_url": url,
            "url": url,
            "message": ("This is a Google News redirect link. Open it in your "
                        "browser, wait for it to land on the real article, then "
                        "paste that final URL here."),
        })

    try:
        title, text, soup = fetch_article(url)
    except Exception as exc:
        return jsonify({"error": f"could not fetch article: {exc}"}), 502

    method = "gemini"
    items = []
    try:
        items = gemini_extract(title, text)
    except Exception as exc:
        print(f"[extract] gemini unavailable, falling back: {exc}")
        method = "fallback"
        items = fallback_extract(soup, text)

    return jsonify({
        "title": title,
        "url": url,
        "method": method,
        "count": len(items),
        "items": items,
    })


# --------------------------------------------------------------------------
# Static convenience (Node normally serves the UI; this is a fallback)
# --------------------------------------------------------------------------

ALLOWED_EXT = {".html", ".htm", ".css", ".js", ".mjs", ".png", ".jpg",
               ".jpeg", ".webp", ".svg", ".ico", ".json", ".txt"}


# A neutral placeholder returned by /img on ANY failure, so a broken image
# never re-triggers the browser's onerror handler (which would loop forever).
_PLACEHOLDER_SVG = (
    b"<svg xmlns='http://www.w3.org/2000/svg' width='92' height='92'>"
    b"<rect width='100%' height='100%' fill='#e7e4d9'/>"
    b"<circle cx='46' cy='46' r='12' fill='none' stroke='#c4b8a4' stroke-width='3'/>"
    b"</svg>"
)


def _placeholder_img():
    return Response(_PLACEHOLDER_SVG, content_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.route("/img")
def img_proxy():
    """Thumbnail proxy (mirrors serve.mjs). ALWAYS returns 200 — on any failure
    it serves a placeholder so a broken thumbnail can never loop via onerror."""
    target = (request.args.get("url") or "").strip()
    if not re.match(r"^https?://", target):
        return _placeholder_img()
    try:
        r = _get(target, headers={**HEADERS, "Accept": "image/*,*/*;q=0.8"})
        ctype = r.headers.get("content-type", "")
        if not r.ok or not ctype.startswith("image"):
            return _placeholder_img()
        return Response(r.content, content_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return _placeholder_img()


@app.route("/")
def index():
    return send_from_directory(ROOT, "Trend Finder.html")


@app.route("/<path:filename>")
def static_files(filename: str):
    if ".." in filename or filename.startswith("."):
        abort(404)
    _, ext = os.path.splitext(filename.lower())
    if ext and ext not in ALLOWED_EXT:
        abort(404)
    if not os.path.isfile(os.path.join(ROOT, filename)):
        abort(404)
    return send_from_directory(ROOT, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    host = os.environ.get("HOST", "127.0.0.1")
    print("\n  Trend Finder backend (Flask) is up.")
    print(f"  API   : http://{host}:{port}/api/trending?q=ai+gadgets")
    print(f"  Gemini: {'configured (' + GEMINI_MODEL + ')' if GEMINI_API_KEY else 'NOT configured — extraction will use HTML fallback'}")
    print("  Tip   : start the Node server too:  node serve.mjs\n")
    app.run(host=host, port=port, debug=False, threaded=True)
