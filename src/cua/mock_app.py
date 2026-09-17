"""Local mock core-banking UI: no test IDs, table layout, runtime exceptions."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
DEFAULT_PORT = 8765

MEMBERS = {
    "12345": {"name": "Jane Doe", "savings": "1840.22"},
}


def _page(title: str, heading: str, body: str, overlay: bool = False) -> bytes:
    overlay_html = ""
    if overlay:
        overlay_html = """
        <div class="overlay" role="alertdialog">
          <div class="overlay-card">
            <p>System notification</p>
            <p>Scheduled maintenance window tonight.</p>
            <button type="button" onclick="document.querySelector('.overlay').remove()">OK</button>
          </div>
        </div>
        """
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <style>
    body {{ font-family: Tahoma, sans-serif; background: #e8e4d9; margin: 0; }}
    .top {{ background: #1f3b2c; color: #fff; padding: 8px 16px; }}
    .wrap {{ margin: 16px; }}
    table.entry {{ background: #fff; border: 1px solid #888; }}
    table.entry td {{ padding: 6px 10px; border: 1px solid #ccc; }}
    table.grid {{ border-collapse: collapse; background: #fff; }}
    table.grid td, table.grid th {{ border: 1px solid #666; padding: 4px 8px; }}
    .banner {{ background: #f6e0e0; border: 1px solid #a44; padding: 8px; margin: 8px 0; }}
    .overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,.45); display: flex; align-items: center; justify-content: center; }}
    .overlay-card {{ background: #fff; padding: 20px; border: 2px solid #333; min-width: 280px; }}
    .danger {{ color: #7a1010; }}
  </style>
</head>
<body>
  <div class="top">Northbridge Credit Union — Core servicing (internal)</div>
  <div class="wrap">
    <h1>{heading}</h1>
    {body}
  </div>
  {overlay_html}
</body>
</html>
"""
    return html.encode("utf-8")


def lookup_form(message: str | None = None, overlay: bool = False) -> bytes:
    banner = f'<div class="banner">{message}</div>' if message else ""
    body = f"""
    {banner}
    <table class="entry">
      <tr>
        <td>Member ID</td>
        <td><input aria-label="Member ID" name="member_id" size="16"/></td>
      </tr>
      <tr>
        <td colspan="2"><button type="button" onclick="var v=document.querySelector('input[name=member_id]').value; location='/lookup?member_id='+encodeURIComponent(v)">Search</button></td>
      </tr>
    </table>
    <p>Use the member identifier from the servicing ticket.</p>
    """
    return _page("Member lookup", "Member lookup", body, overlay=overlay)


def summary(member_id: str, rec: dict) -> bytes:
    body = f"""
    <table class="grid">
      <tr><th>Field</th><th>Value</th></tr>
      <tr><td>Member ID</td><td>{member_id}</td></tr>
      <tr><td>Name</td><td>{rec['name']}</td></tr>
      <tr><td>Savings</td><td>{rec['savings']}</td></tr>
    </table>
    <p>
      <button type="button" class="danger">Transfer</button>
      <button type="button">Print</button>
    </p>
    """
    return _page("Account summary", "Account summary", body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path in {"/", "/lookup"} and "member_id" not in qs:
            overlay = path == "/"
            self._ok(lookup_form(overlay=overlay))
            return
        if path in {"/", "/lookup"}:
            member_id = (qs.get("member_id") or [""])[0].strip()
            if member_id == "":
                self._ok(lookup_form("Member ID is required."))
                return
            if member_id == "40301":
                self._ok(lookup_form("You do not have permission to view this member."))
                return
            rec = MEMBERS.get(member_id)
            if not rec:
                self._ok(lookup_form(f"No member found for {member_id}."))
                return
            self._ok(summary(member_id, rec))
            return
        if path == "/health":
            self._ok(b"ok")
            return
        self.send_error(404)

    def _ok(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = DEFAULT_PORT) -> None:
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    print(f"Mock core listening on http://{HOST}:{port}/")
    httpd.serve_forever()


def serve_in_thread(port: int = DEFAULT_PORT):
    import threading

    httpd = ThreadingHTTPServer((HOST, port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd
