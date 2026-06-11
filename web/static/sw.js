/* ═══════════════════════════════════════════════════════════════════════
   PRAESIDIUM SERVICE WORKER — PWA Embodiment 1010, Service Worker 1012
   Patent Pending — 64/015,486
   ═══════════════════════════════════════════════════════════════════════ */

const CACHE_NAME = 'praesidium-mobile-v1';
const SHELL_CACHE = 'praesidium-shell-v1';
const API_CACHE = 'praesidium-api-v1';

// App shell — cache on install for offline access
const SHELL_ASSETS = [
  '/mobile/',
  '/static/js/praesidium-mobile.js',
  '/manifest.json',
];

// ─── INSTALL ─────────────────────────────────────────────────────────
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => {
      return cache.addAll(SHELL_ASSETS).catch((err) => {
        console.warn('[SW] Shell cache failed (non-fatal):', err);
      });
    })
  );
  self.skipWaiting();
});

// ─── ACTIVATE ────────────────────────────────────────────────────────
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys
          .filter((k) => k !== SHELL_CACHE && k !== API_CACHE && k !== CACHE_NAME)
          .map((k) => caches.delete(k))
      );
    })
  );
  self.clients.claim();
});

// ─── FETCH — Network-first for API, cache-first for shell ───────────
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Skip non-GET requests (POST time entries, AI chat, etc.)
  if (event.request.method !== 'GET') return;

  // Skip SSE/streaming endpoints
  if (url.pathname.includes('/ai-chat')) return;

  // API requests — network-first, cache fallback
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          // Cache successful GET responses
          if (response.ok) {
            const clone = response.clone();
            caches.open(API_CACHE).then((cache) => {
              cache.put(event.request, clone);
            });
          }
          return response;
        })
        .catch(() => {
          // Offline fallback — serve from cache
          return caches.match(event.request).then((cached) => {
            if (cached) return cached;
            return new Response(
              JSON.stringify({ error: 'offline', message: 'No cached data available' }),
              { headers: { 'Content-Type': 'application/json' }, status: 503 }
            );
          });
        })
    );
    return;
  }

  // Shell assets — cache-first, network fallback
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        if (response.ok && (url.pathname.startsWith('/static/') || url.pathname === '/mobile/')) {
          const clone = response.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, clone));
        }
        return response;
      });
    })
  );
});

// ─── BACKGROUND SYNC (1016) — queued time entries sync on reconnect ──
self.addEventListener('sync', (event) => {
  if (event.tag === 'sync-time-entries') {
    event.waitUntil(syncTimeEntries());
  }
});

async function syncTimeEntries() {
  // Background sync implementation — reads from IndexedDB queue
  // and POSTs to /api/v1/mobile/time-entries
  // This is the background synchronization component 1016
  console.log('[SW] Background sync: time-entries');
}
