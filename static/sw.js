/**
 * NovelAI Proxy — Service Worker
 *
 * Strategy:
 *   Static assets (CSS, icons, scripts, manifest): Cache-first
 *   HTML pages:                                      Network-only (may contain sensitive data)
 *   API / POST requests:                            Network-only (never cached)
 */

const CACHE_STATIC = "nai-proxy-static-v3";

// ── Assets to pre-cache immediately on install ──────────────────────
const PRECACHE = [
  "/static/style.css",
  "/static/vendor/lucide.min.js",
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/favicon-32.png",
  "/static/icon.svg",
];

// ── Install: pre-cache critical assets ──────────────────────────────
self.addEventListener("install", (event) => {
  console.log("[SW] Installing — pre-caching static assets");
  event.waitUntil(
    caches
      .open(CACHE_STATIC)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
      .catch((err) => console.warn("[SW] Pre-cache partial failure:", err))
  );
});

// ── Activate: clean old caches ──────────────────────────────────────
self.addEventListener("activate", (event) => {
  console.log("[SW] Activating — cleaning old caches");
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k !== CACHE_STATIC)
            .map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
});

function isNavigation(request) {
  return request.mode === "navigate";
}

function isStaticAsset(url) {
  return url.pathname.startsWith("/static/");
}

// ── Fetch: route by request type ────────────────────────────────────
self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Non-GET requests are never cached
  if (request.method !== "GET") return;

  // ── Navigation (HTML pages): network-only ─────────────────────
  if (isNavigation(request)) {
    event.respondWith(fetch(request));
    return;
  }

  // ── Static assets: cache-first ─────────────────────────────────
  if (isStaticAsset(url)) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request).then((response) => {
          if (response.ok) {
            const cloned = response.clone();
            caches.open(CACHE_STATIC).then((cache) => cache.put(request, cloned));
          }
          return response;
        });
      })
    );
    return;
  }

  // ── Everything else (API, etc.): network-only ──────────────────
});
