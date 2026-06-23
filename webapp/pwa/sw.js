const CACHE_NAME = 'pwa-shell-v2';
const ASSETS = [
  'index.html', 'styles.css', 'app.js', 'manifest.json'
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
  
  evt.respondWith(caches.match(evt.request).then(res => res || fetch(evt.request)));
});
