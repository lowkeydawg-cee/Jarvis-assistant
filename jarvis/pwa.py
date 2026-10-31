"""What a phone needs to install Jarvis as an app: a manifest, a service worker and icons.

The icons are drawn here in plain Python (the amber ring from the page's status
light) so there are no image files to ship and nothing extra to install.
"""

from __future__ import annotations

import json
import math
import struct
import zlib
from functools import lru_cache

BG = (18, 18, 17)
ACCENT = (231, 162, 59)

MANIFEST = {
    "name": "Jarvis",
    "short_name": "Jarvis",
    "description": "Your personal assistant, from your laptop.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#121211",
    "theme_color": "#121211",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icon-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

# Jarvis lives on the laptop, so there is nothing useful to cache: the worker only
# exists so phones offer "Install app", and shows a clear page when the laptop is off.
SERVICE_WORKER = """
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(() => new Response(
    '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<body style="background:#121211;color:#edebe5;font:16px system-ui;padding:32px">' +
    '<h1 style="font-size:22px">Can\\'t reach your laptop</h1>' +
    '<p>Jarvis runs on your laptop. Make sure it is on, Jarvis is running, and Tailscale is connected on both.</p>' +
    '<p><a style="color:#e7a23b" href="/">Try again</a></p>',
    { headers: { "Content-Type": "text/html" } })));
});
""".strip().encode()


def manifest() -> bytes:
    return json.dumps(MANIFEST).encode()


@lru_cache(maxsize=4)
def icon(size: int, maskable: bool = False) -> bytes:
    """A PNG of the Jarvis ring. Maskable icons keep the ring inside Android's safe circle."""
    scale = 0.62 if maskable else 0.8
    c = size / 2
    outer = c * scale
    ring = outer * 0.16
    dot = outer * 0.3
    rows = []
    for y in range(size):
        row = bytearray([0])  # PNG filter: none
        for x in range(size):
            d = math.hypot(x + 0.5 - c, y + 0.5 - c)
            # coverage in 0..1 with a one-pixel soft edge, so the curves look smooth
            on_ring = _clamp(min(outer - d, d - (outer - ring)) + 0.5)
            on_dot = _clamp(dot - d + 0.5)
            a = max(on_ring, on_dot)
            row += bytes(round(BG[i] + (ACCENT[i] - BG[i]) * a) for i in range(3))
        rows.append(bytes(row))
    return _png(size, size, b"".join(rows))


def _clamp(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def _png(w: int, h: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def asset(path: str) -> tuple[bytes, str] | None:
    """(body, content type) for the app files, or None if `path` isn't one."""
    if path == "/manifest.webmanifest":
        return manifest(), "application/manifest+json"
    if path == "/sw.js":
        return SERVICE_WORKER, "text/javascript"
    if path == "/icon-192.png":
        return icon(192), "image/png"
    if path == "/icon-512.png":
        return icon(512), "image/png"
    if path == "/icon-maskable.png":
        return icon(512, maskable=True), "image/png"
    if path == "/apple-touch-icon.png":
        return icon(180), "image/png"
    return None
