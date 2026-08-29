/* Portfolio Rebalancer — service worker (offline app shell) */
const CACHE = 'portfolio-rebalancer-v2.4.0';
const ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './icon-maskable-512.png',
  './apple-touch-icon.png',
  './logo.png'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Prices change daily: always try the network first, fall back to the last copy.
  if (url.pathname.endsWith('/prices.json') || url.pathname.endsWith('prices.json')) {
    e.respondWith(
      fetch(e.request).then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put('./prices.json', copy)).catch(() => {});
        return res;
      }).catch(() => caches.match('./prices.json'))
    );
    return;
  }
  // Everything else is a static app shell: cache-first, fall back to network.
  e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
});
