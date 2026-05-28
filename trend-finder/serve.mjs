// Trend Finder — Node.js frontend server (Server 1).
//
// Runs on http://localhost:3002 and does three jobs:
//   1. Serves the frontend HTML + static assets from this folder.
//   2. Proxies thumbnail images:  GET /img?url=<encoded>
//      (fetched server-side so hot-link / referer / mixed-content blocks
//       don't break the cards).
//   3. Proxies the API:           /api/*  ->  http://localhost:5001/api/*
//      The Gemini-backed /api/extract therefore flows browser -> Node ->
//      Flask -> Gemini, so the GEMINI_API_KEY is never exposed to the browser.
//
// Dependency-free: uses Node built-ins + global fetch (Node 18+).
//
// Run:   node serve.mjs

import http from "node:http";
import { readFile } from "node:fs/promises";
import { existsSync, statSync } from "node:fs";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";

const ROOT = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 3002);
const FLASK = process.env.FLASK_BASE || "http://localhost:5001";
const INDEX = "Trend Finder.html";

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 " +
  "(KHTML, like Gecko) Version/17.4 Safari/605.1.15";

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".htm": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
};

const send = (res, status, body, headers = {}) => {
  res.writeHead(status, headers);
  res.end(body);
};

// --- 2) image proxy -------------------------------------------------------
// neutral placeholder so a broken thumbnail never re-triggers onerror (loop)
const PLACEHOLDER = Buffer.from(
  "<svg xmlns='http://www.w3.org/2000/svg' width='92' height='92'>" +
  "<rect width='100%' height='100%' fill='#e7e4d9'/>" +
  "<circle cx='46' cy='46' r='12' fill='none' stroke='#c4b8a4' stroke-width='3'/></svg>"
);
function placeholderImg(res) {
  send(res, 200, PLACEHOLDER, {
    "Content-Type": "image/svg+xml",
    "Cache-Control": "public, max-age=3600",
    "Access-Control-Allow-Origin": "*",
  });
}

async function handleImage(req, res, u) {
  const target = u.searchParams.get("url");
  if (!target || !/^https?:\/\//.test(target)) return placeholderImg(res);
  try {
    const r = await fetch(target, {
      headers: { "User-Agent": UA, Accept: "image/*,*/*;q=0.8" },
      redirect: "follow",
    });
    const ctype = r.headers.get("content-type") || "";
    // ALWAYS 200: on any non-image / error response, serve the placeholder
    if (!r.ok || !ctype.startsWith("image")) return placeholderImg(res);
    const buf = Buffer.from(await r.arrayBuffer());
    send(res, 200, buf, {
      "Content-Type": ctype,
      "Cache-Control": "public, max-age=86400",
      "Access-Control-Allow-Origin": "*",
    });
  } catch {
    return placeholderImg(res);
  }
}

// --- 3) API proxy ---------------------------------------------------------
async function handleApi(req, res, u) {
  const targetUrl = FLASK + u.pathname + u.search;
  try {
    const init = {
      method: req.method,
      headers: { Accept: "application/json" },
      redirect: "follow",
    };
    if (req.method !== "GET" && req.method !== "HEAD") {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      init.body = Buffer.concat(chunks);
      init.headers["Content-Type"] =
        req.headers["content-type"] || "application/json";
    }
    const r = await fetch(targetUrl, init);
    const buf = Buffer.from(await r.arrayBuffer());
    send(res, r.status, buf, {
      "Content-Type": r.headers.get("content-type") || "application/json",
      "Access-Control-Allow-Origin": "*",
    });
  } catch (e) {
    send(res, 502, JSON.stringify({
      error: `cannot reach Flask backend at ${FLASK}. Is server.py running? (${e.message})`,
    }), { "Content-Type": "application/json" });
  }
}

// --- 1) static files ------------------------------------------------------
async function handleStatic(req, res, u) {
  let rel = decodeURIComponent(u.pathname);
  if (rel === "/" || rel === "") rel = "/" + INDEX;
  // prevent path traversal & dotfiles (.env etc.)
  const safe = normalize(rel).replace(/^(\.\.[/\\])+/, "");
  if (safe.includes("..") || /[/\\]\./.test(safe)) return send(res, 404, "not found");

  const filePath = join(ROOT, safe);
  if (!existsSync(filePath) || !statSync(filePath).isFile()) {
    return send(res, 404, "not found");
  }
  try {
    const data = await readFile(filePath);
    const mime = MIME[extname(filePath).toLowerCase()] || "application/octet-stream";
    const headers = { "Content-Type": mime };
    // never cache the HTML so edits show up on a normal reload
    if (mime.startsWith("text/html")) headers["Cache-Control"] = "no-cache, no-store, must-revalidate";
    send(res, 200, data, headers);
  } catch (e) {
    send(res, 500, "read error: " + e.message);
  }
}

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, `http://localhost:${PORT}`);
  if (u.pathname === "/img") return handleImage(req, res, u);
  if (u.pathname.startsWith("/api/")) return handleApi(req, res, u);
  return handleStatic(req, res, u);
});

server.listen(PORT, () => {
  console.log(`\n  Trend Finder frontend (Node) is up.`);
  console.log(`  Open  : http://localhost:${PORT}/`);
  console.log(`  Image : /img?url=...   API proxy: /api/* -> ${FLASK}`);
  console.log(`  Tip   : start the Flask backend too:  python server.py\n`);
});
