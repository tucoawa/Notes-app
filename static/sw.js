const CACHE_NAME = 'notes-app-v2';

// Only precache what the app loads on EVERY page:
// the HTML shell's CSS, the JS modules, the markdown renderer,
// the highlight theme, and the fonts.
//
// Leaflet / leaflet-gpx / leaflet-locate are NOT precached:
// they're only loaded for map notes, and the network-first
// handler below will cache them on first use.
const PRECACHE = [
  '/static/style.css',
  '/static/app.js',
  '/static/editor.js',
  '/static/vendor/marked.min.js',
  '/static/vendor/cm/codemirror-bundle.js',
  '/static/vendor/highlight.min.js',
  '/static/vendor/highlight-github-dark.min.css',
  '/static/fonts/material-symbols-rounded.woff2',
  '/static/fonts/roboto-400.woff2',
  '/static/fonts/roboto-500.woff2',
  '/static/fonts/roboto-600.woff2',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      // Best-effort: one missing file shouldn't block install.
      return Promise.all(
        PRECACHE.map((url) =>
          cache.add(url).catch((err) => console.warn('[sw] failed to cache', url, err))
        )
      );
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Navigations and API calls: pass through, never cache.
  if (req.mode === 'navigate' || url.pathname.startsWith('/api/') || url.pathname.startsWith('/note/')) {
    return;
  }

  // Static assets: network-first, cache fallback (for offline).
  // Any static file (Leaflet, GPX, icons, whatever) gets cached the
  // first time it's actually fetched by a real page load. That keeps
  // the precache list small and correct, while still giving offline
  // support for anything the user has actually seen.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      fetch(req).then((resp) => {
        if (resp && resp.ok) {
          const respClone = resp.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(req, respClone).catch(() => {});
          });
        }
        return resp;
      }).catch(() => {
        return caches.match(req);
      })
    );
    return;
  }

  // Everything else: cache-first, network fallback.
  event.respondWith(
    caches.match(req).then((cached) => {
      return cached || fetch(req);
    })
  );
});