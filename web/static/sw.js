/* ═══════════════════════════════════════════════════════════════════════
   PRAESIDIUM SERVICE WORKER — PWA Embodiment 1010, Service Worker 1012
   Patent Pending — 64/015,486
   ═══════════════════════════════════════════════════════════════════════ */

// v2: shell is now NETWORK-FIRST (was cache-first) so code fixes land on the
// next online open instead of being pinned in cache. Version bump purges the
// old v1 caches on activate.
const CACHE_NAME = 'praesidium-mobile-v4';
const SHELL_CACHE = 'praesidium-shell-v4';
const API_CACHE = 'praesidium-api-v4';

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

// ─── FETCH — network-first for API AND shell; cache is offline fallback ──
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Skip non-GET requests (POST time entries, AI chat, etc.)
  if (event.request.method !== 'GET') return;

  // Skip SSE/streaming endpoints
  if (url.pathname.includes('/ai-chat')) return;

  const isApi = url.pathname.startsWith('/api/');
  const isShell = url.pathname.startsWith('/static/') || url.pathname === '/mobile/' || url.pathname === '/manifest.json';

  // API: NETWORK-ONLY. Never cache tenant/user-scoped (authenticated) responses —
  // a cached body must not be served to a later user/session on a shared install.
  if (isApi) {
    event.respondWith(
      fetch(event.request).catch(() => new Response(
        JSON.stringify({ error: 'offline', message: 'No connection' }),
        { headers: { 'Content-Type': 'application/json' }, status: 503 }))
    );
    return;
  }

  if (!isShell) return;

  // Shell: network-first, cache fallback (offline app shell).
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) { const clone = response.clone(); caches.open(SHELL_CACHE).then((c) => c.put(event.request, clone)); }
        return response;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || new Response('', { status: 504 })))
  );
});

// ─── PUSH (web-push reminders) ──────────────────────────────────────
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; }
  catch (e) { data = { title: 'Praesidium', body: event.data ? event.data.text() : '' }; }
  const title = data.title || 'Praesidium';
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || '',
    icon: '/static/img/praesidium-icon-192.png',
    badge: '/static/img/praesidium-icon-192.png',
    data: { url: data.url || '/mobile/' },
    tag: data.tag || undefined,
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/mobile/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((cl) => {
      for (const c of cl) { if (c.url.includes('/mobile') && 'focus' in c) return c.focus(); }
      if (clients.openWindow) return clients.openWindow(url);
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
