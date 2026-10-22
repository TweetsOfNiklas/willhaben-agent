// SecondHandler's service worker (served as /sw.js, scope /): when the app does not answer a page request – a
// restart or deploy, the network, or the reverse proxy's plain "404 page not found" while the container is away –
// it shows the cached offline page (/static/offline.html), which reloads by itself once /healthz answers.
// Everything else goes to the network untouched: the app's own pages are never served from the cache.
const CACHE = 'sh-offline-v1';
const OFFLINE = '/static/offline.html';
const KEEP = [OFFLINE, '/static/icons/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(KEEP)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// a proxy's answer for an app that is away: plain text ("404 page not found", "Bad Gateway"), never one of the
// app's own pages (HTML) or answers (JSON, e.g. a real 404 of an unknown address)
const away = (r) => [404, 502, 503, 504].includes(r.status) && /^text\/plain|^$/.test(r.headers.get('content-type') || '');

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).then((r) => (away(r) ? caches.match(OFFLINE).then((o) => o || r) : r))
      .catch(() => caches.match(OFFLINE)));
  } else if (KEEP.includes(new URL(req.url).pathname)) {
    e.respondWith(fetch(req).catch(() => caches.match(req)));     // the offline page's own icon, also offline
  }
});
