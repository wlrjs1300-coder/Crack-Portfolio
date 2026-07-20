const CACHE_NAME = 'crack-v17';
const ASSETS_TO_CACHE = [
    '/static/icons/icon-192.png?v=3',
    '/static/icons/icon-512.png?v=3'
];

self.addEventListener('install', (event) => {
    self.skipWaiting();
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(ASSETS_TO_CACHE);
        })
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name))
            );
        })
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    const isPublicStatic = url.origin === self.location.origin && (
        url.pathname.startsWith('/static/') || url.pathname === '/manifest.json' || url.pathname === '/sw.js'
    );

    // HTML, API, 업로드, 외부 리소스 및 변경 요청은 캐시하지 않는다.
    if (event.request.method !== 'GET' || event.request.mode === 'navigate' || !isPublicStatic) {
        event.respondWith(fetch(event.request));
        return;
    }

    event.respondWith(
        caches.match(event.request).then((response) => {
            return response || fetch(event.request);
        })
    );
});
