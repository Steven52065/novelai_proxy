/**
 * NovelAI Proxy — Service Worker
 *
 * Strategy:
 *   Static assets (CSS, icons, manifest): Cache-first
 *   CDN scripts (Lucide):              Stale-while-revalidate
 *   HTML pages:                         Network-first (admin data changes frequently)
 *   API / POST requests:               Network-only (never cached)
 */

const CACHE_STATIC = "nai-proxy-static-v1";
const CACHE_CDN = "nai-proxy-cdn-v1";

// ── Assets to pre-cache immediately on install ──────────────────────
const PRECACHE = [
  "/static/style.css",
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/favicon-32.png",
  "/static/icon.svg",
];

// ── CDN origins eligible for caching ────────────────────────────────
const CDN_ORIGINS = ["unpkg.com", "cdn.jsdelivr.net"];

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
            .filter((k) => k !== CACHE_STATIC && k !== CACHE_CDN)
            .map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
});

// ── Helpers ─────────────────────────────────────────────────────────
function isCDN(url) {
  return CDN_ORIGINS.some((origin) => url.hostname.endsWith(origin));
}

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

  // ── CDN scripts: stale-while-revalidate ───────────────────────
  if (isCDN(url)) {
    event.respondWith(
      caches.open(CACHE_CDN).then((cache) =>
        cache.match(request).then((cached) => {
          const fetched = fetch(request)
            .then((response) => {
              if (response.ok) cache.put(request, response.clone());
              return response;
            })
            .catch(() => cached);
          return cached || fetched;
        })
      )
    );
    return;
  }

  // ── Navigation (HTML pages): network-first, update cache ──────
  if (isNavigation(request)) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const cloned = response.clone();
            caches.open(CACHE_STATIC).then((cache) => cache.put(request, cloned));
          }
          return response;
        })
        .catch(() =>
          // Offline: try cache, fallback to /admin
          caches.match(request).then((cached) => cached || caches.match("/admin"))
        )
    );
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
