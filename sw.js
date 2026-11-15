const CACHE = 'agrieko-v1';
const SHELL = ['/', '/icon-192.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Sahifalar doim internetdan olinadi; internet yo'q bo'lsa, bosh sahifa ko'rsatiladi.
// /api so'rovlari keshlanmaydi, shuning uchun e'lonlar eskirib qolmaydi.
self.addEventListener('fetch', e => {
  const r = e.request;
  if (r.method !== 'GET') return;
  const u = new URL(r.url);
  if (u.origin !== location.origin || u.pathname.startsWith('/api')) return;
  if (r.mode === 'navigate') e.respondWith(fetch(r).catch(() => caches.match('/')));
});
