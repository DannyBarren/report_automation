/*
 * GenerSwift service worker — minimal, recording-safe.
 *
 * Goals:
 *   - Enable "Add to Home Screen" (installability) on mobile.
 *   - Cache the static app shell (CSS/JS/icon) so the UI loads fast and survives flaky
 *     job-site connections.
 *
 * Deliberately conservative: we NEVER cache POST requests, video uploads, API calls, or
 * navigations to dynamic routes. The recorder's own offline durability is handled by
 * IndexedDB in field_recorder.js — the SW must not interfere with the live camera flow.
 */

const CACHE = 'generswift-shell-v1';
const SHELL = [
  '/static/css/app.css',
  '/static/css/field_recorder.css',
  '/static/js/field_recorder.js',
  '/static/icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // Only handle same-origin GETs; everything else (uploads, APIs, POSTs) goes to network.
  if (req.method !== 'GET' || new URL(req.url).origin !== self.location.origin) {
    return;
  }

  // Cache-first for static shell assets; refresh in the background.
  if (req.url.includes('/static/')) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const network = fetch(req)
          .then((resp) => {
            if (resp && resp.ok) {
              const clone = resp.clone();
              caches.open(CACHE).then((cache) => cache.put(req, clone)).catch(() => {});
            }
            return resp;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
  }
  // All other GETs (pages) are network-first and left to the browser by default.
});
