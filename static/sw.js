const CACHE_NAME = 'crack-v19';
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

    const bypassCache = url.pathname.startsWith('/static/security.js') || url.pathname.startsWith('/sw.js');
    if (bypassCache) {
        return;
    }

    // HTML, API, 업로드, 외부 리소스 및 변경 요청은 서비스 워커가 가로채지 않고
    // 브라우저 기본 동작에 맡긴다. (respondWith 안에서 fetch()로 재요청하면
    // 요청 종류(script/img 등)에 맞는 CSP 지시문이 아니라 connect-src로
    // 재검사되어 정상적인 외부 리소스까지 차단되는 문제가 있었음)
    if (event.request.method !== 'GET' || event.request.mode === 'navigate' || !isPublicStatic) {
        return;
    }

    event.respondWith(
        caches.match(event.request).then((response) => {
            return response || fetch(event.request);
        })
    );
});
