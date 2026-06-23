const CACHE_NAME = 'pwa-shell-v8';
const ASSETS = [
  'index.html', 'styles.css?v=8', 'app.js?v=8', 'manifest.json'
];

self.addEventListener('install', evt => {
  evt.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', evt => {
  evt.waitUntil(
    caches.keys().then(names => 
      Promise.all(names.filter(n => n !== CACHE_NAME).map(n => caches.delete(n)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', evt => {
  const url = new URL(evt.request.url);
  
  // Network-first for API calls
  if(url.pathname.startsWith('/api/')){
    evt.respondWith(
      fetch(evt.request)
        .then(res => {
          if(res.ok) return res;
          return caches.match('index.html');
        })
        .catch(() => caches.match('index.html'))
    );
    return;
  }
  
  // Network-first for HTML (always check server first)
  if(url.pathname.endsWith('.html') || url.pathname === '/'){
    evt.respondWith(
      fetch(evt.request)
        .then(res => {
          if(res.ok){
            caches.open(CACHE_NAME).then(c => c.put(evt.request, res.clone()));
            return res;
          }
          return caches.match(evt.request);
        })
        .catch(() => caches.match(evt.request))
    );
    return;
  }
  
  // Network-first for JS/CSS (force fresh assets)
  if(url.pathname.endsWith('.js') || url.pathname.endsWith('.css')){
    evt.respondWith(
      fetch(evt.request)
        .then(res => {
          if(res.ok){
            caches.open(CACHE_NAME).then(c => c.put(evt.request, res.clone()));
            return res;
          }
          return caches.match(evt.request);
        })
        .catch(() => caches.match(evt.request))
    );
    return;
  }
  
  // Cache-first for other assets
  evt.respondWith(caches.match(evt.request).then(res => res || fetch(evt.request)));
});
