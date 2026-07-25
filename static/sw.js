// RunCoach service worker: network-first (always-fresh UI + data online),
// cache fallback for offline. Its presence also makes the app installable.
const CACHE = 'runcoach-v1';
const SHELL = ['/', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith((async () => {
    try {
      const resp = await fetch(e.request);
      const cache = await caches.open(CACHE);
      cache.put(e.request, resp.clone());
      return resp;
    } catch (err) {
      const cached = await caches.match(e.request);
      return cached || caches.match('/');
    }
  })());
});
