"""Lightweight HTTP status bridge for the breaktimer mobile companion.

Reads the shared status snapshot and serves it over HTTP so any device on
the local network can check the mana bar without a native app:

  GET /        — mobile-friendly HTML page (auto-refreshes every 2 s)
  GET /status  — live Snapshot as JSON (the cross-process contract from status.py)

Runs as a standalone sibling to the timer core and ambient bar; either
process can restart without the other.  Binds to all interfaces by default
so a phone on the same LAN can reach it.  Port defaults to 8642.
"""
import argparse
import json
import logging
import socketserver
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer

import status

log = logging.getLogger("breaktimer.web")


# Mobile-friendly status page.  Color stops are generated from status.COLOR_STOPS
# at module load so the phone bar always matches the ambient desktop strip.
_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>breaktimer</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #0e0e0e;
  color: #bbb;
  font-family: system-ui, -apple-system, sans-serif;
  min-height: 100dvh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 2rem 1rem;
  gap: .35rem;
}
#rail { position: fixed; top: 0; left: 0; right: 0; height: 5px; background: #1a1a1a; }
#fill { height: 100%; transition: width 1.5s ease, background-color 1.5s ease; }
#time {
  font-size: 4rem;
  font-weight: 300;
  letter-spacing: .05em;
  font-variant-numeric: tabular-nums;
  transition: color 1.5s ease;
}
#state  { font-size: .85rem; color: #555; }
#grace  { color: #f44; font-size: .95rem; font-weight: 600; min-height: 1.2em; }
#history { font-size: .7rem; color: #444; text-align: center; margin-top: 1.2rem; }
</style>
</head>
<body>
<div id="rail"><div id="fill"></div></div>
<div id="time">--:--</div>
<div id="state">connecting…</div>
<div id="grace"></div>
<div id="history"></div>
<script>
const STOPS = __STOPS__;
function barColor(f) {
  f = Math.max(0, Math.min(1, f));
  for (let i = 0; i < STOPS.length - 1; i++) {
    const [lo, r0, g0, b0] = STOPS[i];
    const [hi, r1, g1, b1] = STOPS[i + 1];
    if (f >= lo && f <= hi) {
      const t = (f - lo) / (hi - lo);
      return `rgb(${r0 + t * (r1 - r0) | 0},${g0 + t * (g1 - g0) | 0},${b0 + t * (b1 - b0) | 0})`;
    }
  }
  return `rgb(${STOPS[STOPS.length - 1].slice(1).join(',')})`;
}
function fmtTime(s) {
  s = Math.max(0, s + .5 | 0);
  const h = s / 3600 | 0, m = (s % 3600) / 60 | 0, sec = s % 60;
  const pad = n => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}
function render(d) {
  const frac = d.max_seconds > 0 ? d.remaining_seconds / d.max_seconds : 0;
  const c = barColor(frac);
  document.getElementById('fill').style.cssText = `width:${(frac * 100).toFixed(2)}%;background:${c}`;
  const timeEl = document.getElementById('time');
  timeEl.textContent = fmtTime(d.remaining_seconds);
  timeEl.style.color = c;
  let st = d.is_active ? '● active' : '○ idle';
  if (d.refill_rate <= 0) st += ' · no refill';
  else if (d.refill_rate < 1) st += ` · refill ${d.refill_rate * 100 + .5 | 0}%`;
  document.getElementById('state').textContent = st;
  document.getElementById('grace').textContent = d.grace_remaining != null
    ? `⚠ shutting down in ${fmtTime(d.grace_remaining)}` : '';
  document.getElementById('history').textContent = d.history || '';
}
function offline() {
  document.getElementById('fill').style.cssText = 'width:0;background:#222';
  const timeEl = document.getElementById('time');
  timeEl.textContent = '--:--'; timeEl.style.color = '#333';
  document.getElementById('state').textContent = 'core offline';
  document.getElementById('grace').textContent = '';
  document.getElementById('history').textContent = '';
}
async function poll() {
  try {
    const r = await fetch('/status', {cache: 'no-store'});
    const d = await r.json();
    d.offline ? offline() : render(d);
  } catch (_) { offline(); }
}
poll();
setInterval(poll, 2000);

// Phone activity ping — while this page is foregrounded, phone use drains
// the shared mana bar exactly like laptop keyboard/mouse input.
let _pingTimer = null;
function _ping() {
  fetch('/ping', {method: 'POST', cache: 'no-store'}).catch(() => {});
}
function _startPing() {
  if (!_pingTimer) { _ping(); _pingTimer = setInterval(_ping, 2000); }
}
function _stopPing() { clearInterval(_pingTimer); _pingTimer = null; }
document.addEventListener('visibilitychange',
  () => document.visibilityState === 'visible' ? _startPing() : _stopPing());
document.addEventListener('touchstart', _ping, {passive: true});
document.addEventListener('scroll',     _ping, {passive: true});
if (document.visibilityState === 'visible') _startPing();
</script>
</body>
</html>"""

_HTML = _HTML_TEMPLATE.replace(
    "__STOPS__",
    json.dumps([[lo, r, g, b] for lo, r, g, b in status.COLOR_STOPS]),
)
_HTML_BYTES = _HTML.encode()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request stdout noise; errors go to module logger

    def do_GET(self):
        if self.path == "/status":
            self._serve_status()
        elif self.path in ("/", "/index.html"):
            self._serve_html()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/ping":
            self._handle_ping()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_ping(self):
        # Drain any request body before replying (fetch() sends none, but be safe).
        n = int(self.headers.get("Content-Length", 0))
        if n:
            self.rfile.read(n)
        try:
            status.write_phone_ping()
        except OSError as e:
            log.warning("phone ping write failed: %s", e)
        self.send_response(204)
        self.end_headers()

    def _serve_status(self):
        snap = status.Snapshot.read(max_age_seconds=5)
        body = json.dumps(asdict(snap) if snap else {"offline": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        body = _HTML_BYTES
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(socketserver.ThreadingMixIn, HTTPServer):
    """Thread-per-request so a slow client can't stall the next poll."""
    daemon_threads = True


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="breaktimer HTTP status bridge")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Interface to bind (default: 0.0.0.0 = all)")
    parser.add_argument("--port", type=int, default=8642,
                        help="Port to listen on (default: 8642)")
    args = parser.parse_args()

    lock = status.acquire_singleton_lock("web")
    if lock is None:
        log.error("breaktimer web server already running — exiting")
        sys.exit(1)

    server = _Server((args.host, args.port), _Handler)
    log.info("breaktimer web: http://%s:%d/", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
