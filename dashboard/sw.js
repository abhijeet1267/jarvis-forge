/* Vantage — the phone client's service worker.

   Caches the shell so the installed app opens instantly and shows its own chrome without
   a round trip. It deliberately never caches an answer from the laptop: a stored battery
   level, device list or pending confirmation would be indistinguishable from a live one,
   and this project's whole discipline is that a claim must be observed to be reported.
   Anything that is not the shell goes straight to the network.

   The cache name is versioned on purpose. Phones that installed the old shell keep
   that cache under the old name, and activate() deletes anything that is not the
   current name -- so a change to the shell has to change the version here as well,
   or every installed phone keeps serving the previous shell. Bumped to v2 in Phase 58
   when the page learned to open on a named card. */

const PRECACHE = ["/phone", "/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png"];

const SHELL = PRECACHE.join("|");

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open("jarvis-phone-shell-v2")
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== "jarvis-phone-shell-v2")
        .map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;              // a command is never served from cache

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;   // never proxy a third party's answer

  const isShell = PRECACHE.some((path) => url.pathname === path);
  if (isShell) {
    // Network first: the page carries a fresh single-use credential each time it is
    // served, so a cached copy would show a phone a code the laptop has already spent.
    event.respondWith(
      fetch(request).then((response) => {
        const copy = response.clone();
        caches.open("jarvis-phone-shell-v2").then((cache) => cache.put(request, copy));
        return response;
      }).catch(() => caches.match(request))
    );
    return;
  }
  event.respondWith(fetch(request));                  // every API read/write, live or failed
});
