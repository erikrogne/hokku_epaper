// Bump when changing the public assets below. No private runtime data is cached.
const CACHE = 'hokku-public-v1';
const PUBLIC_ASSETS = [
  '/hokku/static/offline.html',
  '/hokku/static/mobile.css',
  '/hokku/static/mobile.js',
  '/hokku/static/icon-192.png',
  '/hokku/static/icon-512.png',
];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(PUBLIC_ASSETS)));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key.startsWith('hokku-public-') && key !== CACHE).map(key => caches.delete(key))
  )));
});
self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (request.mode === 'navigate' && url.pathname === '/hokku/ui') {
    event.respondWith(fetch(request).catch(() => caches.match('/hokku/static/offline.html')));
    return;
  }
  // Exact allowlist: API, photos, previews, config and firmware bypass this worker.
  if (url.search || !PUBLIC_ASSETS.includes(url.pathname)) return;
  // Online pages always see the matching server assets; the cache is only a
  // fallback, so a waiting worker cannot pair fresh HTML with stale scripts.
  event.respondWith(fetch(request).catch(() => caches.match(request)));
});
