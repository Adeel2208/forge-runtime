"""What makes Studio installable rather than merely visitable.

A manifest, a service worker and an icon. Together these are what let a browser
offer "Install", after which Studio has a Start Menu or Applications entry, its
own window with no browser furniture, and its own icon in the dock - which is
the whole practical difference between a page and a program.

Chromium requires three things before it will offer installation: a manifest
with 192px and 512px icons, a service worker with a fetch handler, and a secure
origin. Studio is served on loopback, which counts as secure, so all three are
satisfiable with no build step and no fifth dependency.

The service worker caches the shell, not the data. Studio's data is a live
repository and a running agent; serving either from a cache would show someone
a branch that has since moved. So the worker is network-first for everything
under /code/ and cache-first only for the shell itself, which is what makes the
app open instantly and still tell the truth.
"""

from __future__ import annotations

import json

__all__ = ["ICON_SVG", "MANIFEST", "SERVICE_WORKER", "manifest_json"]

#: An anvil, drawn rather than sourced, so nothing here reaches a CDN.
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="96" fill="#16202b"/>
  <path d="M96 208h136l40-40h56l-28 40h116v40c0 44-36 80-80 80H208
           c-44 0-80-36-80-80v-8H96z" fill="#6da8e0"/>
  <path d="M176 328h160l28 72H148z" fill="#6da8e0" opacity=".55"/>
  <rect x="120" y="400" width="272" height="36" rx="12" fill="#6da8e0"/>
</svg>"""


MANIFEST: dict[str, object] = {
    "name": "FORGE Studio",
    "short_name": "FORGE",
    "description": "A coding agent whose work you read before you merge it.",
    "start_url": "/code",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#0c1015",
    "theme_color": "#16202b",
    "categories": ["developer", "productivity", "utilities"],
    "icons": [
        {"src": "/icon.svg", "sizes": "192x192", "type": "image/svg+xml", "purpose": "any"},
        {"src": "/icon.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "any"},
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "maskable"},
    ],
    "shortcuts": [
        {"name": "Run console", "url": "/", "description": "Every phase, permit and effect"},
    ],
}


def manifest_json() -> str:
    return json.dumps(MANIFEST, indent=2)


SERVICE_WORKER = """
// FORGE Studio service worker.
//
// The shell is cached so the window opens instantly and survives a restart of
// the server. Repository data is not: it is a live working tree and a running
// agent, and a cached branch is a lie told confidently. So /code/* and /runs*
// are always network-first, and only the shell falls back to cache.

const SHELL = "forge-shell-v3";
const SHELL_FILES = ["/code", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;

  // Live data: never answer from cache. Being briefly offline is better than
  // being confidently wrong about what is on a branch.
  const isData = url.pathname.startsWith("/code/") || url.pathname.startsWith("/runs");
  if (isData) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Shell: serve from cache immediately, refresh it in the background.
  event.respondWith(
    caches.match(event.request).then((hit) => {
      const live = fetch(event.request)
        .then((res) => {
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(SHELL).then((c) => c.put(event.request, copy));
          }
          return res;
        })
        .catch(() => hit);
      return hit || live;
    })
  );
});
"""
