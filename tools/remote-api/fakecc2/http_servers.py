"""The fake printer's HTTP servers, each in its own thread (http.server from the standard library).

- PrinterHttpHandler  (:80)    GET /system/info, PUT /upload, GET /download[/udisk|/sdcard]
- CameraHandler       (:8080)  MJPEG stream on any path, no auth (like the real CC2)
- ControlHandler      (:18080) GET/POST /state for tests: inspect and force the fake's state
"""
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from camera_frame import JPEG

MAX_CHUNK_BYTES = 16 * 1024 * 1024  # the SDK sends 1 MiB chunks; refuse anything absurd


class FakeServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind the same port silently; we want
    # "port already in use" instead, so a stale fake cannot steal the slicer's traffic.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address, handler, printer, log, opts):
        self.printer = printer
        self.log = log
        self.opts = opts
        super().__init__(address, handler)


def start_server(handler, host, port, printer, log, opts):
    """Bind (raises OSError when the port is taken) and serve in a daemon thread."""
    server = FakeServer((host, port), handler, printer, log, opts)
    threading.Thread(target=server.serve_forever, name=handler.__name__, daemon=True).start()
    return server


def parse_query(raw_path):
    """Split path and query. Lenient on purpose: the SDK's download URL is
    /download?X-Token=<code>?file_name=<name> (two '?', ElegooFdmCC2HttpTransfer::doDownload)."""
    path, _, query = raw_path.partition("?")
    params = {}
    for part in re.split(r"[?&]", query):
        key, _, value = part.partition("=")
        if key:
            params[urllib.parse.unquote_plus(key)] = urllib.parse.unquote_plus(value)
    return path, params


def header_text(value):
    """http.server decodes header bytes as latin-1; cpp-httplib sends file names as raw UTF-8."""
    if value is None:
        return None
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


class BaseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: the SDK reuses one connection for all chunks
    server_version = "FakeCC2/1.0"

    def send_json(self, status, body, pretty=False):
        data = json.dumps(body, indent=2 if pretty else None, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            self.send_json(413, {"error_code": 1003})
            self.close_connection = True
            return None
        return self.rfile.read(length) if length else b""

    def drop_if_offline(self):
        """An offline printer does not answer: close the socket without a response."""
        if not self.server.printer.is_online():
            self.close_connection = True
            return True
        return False

    def log_message(self, fmt, *args):
        self.server.log.debug("http %s: %s" % (self.server.server_address[1], fmt % args))


class PrinterHttpHandler(BaseHandler):
    """The printer's own web server on port 80."""

    def token(self, query):
        # The SDK sends the access code both as ?X-Token= and as an X-Token header.
        return query.get("X-Token") or self.headers.get("X-Token")

    def do_GET(self):
        if self.drop_if_offline():
            return
        printer = self.server.printer
        path, query = parse_query(self.path)
        if path == "/system/info":
            status, body = printer.system_info(self.token(query))
            self.send_json(status, body)
        elif path in ("/download", "/download/udisk", "/download/sdcard"):
            if path != "/download":  # only internal storage is emulated
                self.send_json(404, {"error_code": 1021})
                return
            status, file_path = printer.download_path(self.token(query), query.get("file_name"))
            if status != 200:
                self.send_json(status, {"error_code": 1000 if status == 401 else 1021})
                return
            size = os.path.getsize(file_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(file_path, "rb") as f:
                while True:
                    block = f.read(1 << 20)
                    if not block:
                        break
                    self.wfile.write(block)
        elif path in ("/", "/index.html"):
            page = b"<!doctype html><title>Fake CC2</title><h1>Fake Centauri Carbon 2</h1>" \
                   b"<p>Test double for the ElegooSlicer MCP port. See tools/remote-api/fakecc2/README.md.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        else:
            self.send_json(404, {"error_code": 1001})

    def do_PUT(self):
        if self.drop_if_offline():
            return
        path, query = parse_query(self.path)
        body = self.read_body(MAX_CHUNK_BYTES)
        if body is None:
            return
        if path != "/upload":
            self.send_json(404, {"error_code": 1001})
            return
        status, reply = self.server.printer.upload_chunk(
            self.token(query),
            header_text(self.headers.get("X-File-Name")),
            self.headers.get("X-File-MD5"),
            self.headers.get("Content-Range"),
            body)
        self.send_json(status, reply)


class CameraHandler(BaseHandler):
    """MJPEG like the CC2's :8080 camera: multipart/x-mixed-replace; boundary=frame, any path."""

    protocol_version = "HTTP/1.0"  # the stream ends when the connection closes

    def do_GET(self):
        printer = self.server.printer
        if not printer.camera_enabled():
            self.close_connection = True
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        part_header = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(JPEG)
        interval = 1.0 / max(0.1, self.server.opts.camera_fps)
        try:
            while printer.camera_enabled():
                self.wfile.write(part_header + JPEG + b"\r\n")
                self.wfile.flush()
                time.sleep(interval)
        except OSError:
            pass  # viewer closed the stream
        self.close_connection = True


CONTROL_HELP = """Fake CC2 control endpoint
GET  /state   everything the fake knows: phase, status, task, CANVAS, clients, received files
              (size, md5, sha256), every MQTT request received, settings
POST /state   JSON object, any of these keys (applied in order, "reset" first):
  reset: true                         back to start-up state, like a reboot (MQTT clients are dropped
                                      and reconnect; received files are kept)
  offline: true|false                 drop and refuse MQTT/HTTP/UDP/camera like a powered-off printer
  kick_clients: true                  drop MQTT connections (the SDK reconnects by itself)
  state: idle|busy|printing|paused|completed     jump to a state without a real job
  machine_status: {"status": n, "sub_status": m} | null   raw override of machine_status
  progress: 0-100                     move the current print
  exceptions: [codes]                 machine_status.exception_status + exception.exception_code
  auto_refill: bool, canvas_connected: bool
  trays: [{"tray_id": 0, "canvas_id": 0, "filament_color": "#00FF00", ...}]   patch trays
  canvas_info: {...}                  replace the whole GET_CANVAS_STATUS canvas_info
  access_code, hostname, max_clients, rate_limit, missing_bed_leveling, camera
  fail_next: {"method": 1020, "error_code": 1009}   answer that method's next request with the error
  drop_next: 1022                     leave that method's next request unanswered (SDK timeout)
  delay_next: {"method": 1021, "seconds": 20}   carry out that method's next request at once but
                                      answer it late (the real CC2 answers pause/stop after parking)
  print_seconds, preheat_seconds, leveling_seconds, completed_hold_seconds
  clear_log: true
  delete_files: ["name.gcode", ...]   remove files from the received folder
"""


class ControlHandler(BaseHandler):
    """Inspection and fault injection for tests (not part of the real printer)."""

    def do_GET(self):
        path, _ = parse_query(self.path)
        if path == "/state":
            self.send_json(200, self.server.printer.snapshot(), pretty=True)
        else:
            data = CONTROL_HELP.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def do_POST(self):
        path, _ = parse_query(self.path)
        body = self.read_body(1024 * 1024)
        if body is None:
            return
        if path != "/state":
            self.send_json(404, {"error": "not_found", "hint": "POST /state"})
            return
        try:
            request = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError) as error:
            self.send_json(400, {"error": "bad_json", "detail": str(error)})
            return
        if not isinstance(request, dict):
            self.send_json(400, {"error": "bad_json", "detail": "expected a JSON object"})
            return
        status, reply = self.server.printer.apply_control(request)
        if status == 200:
            self.server.log.info("control: applied %s" % json.dumps(request, ensure_ascii=False)[:300])
        self.send_json(status, reply, pretty=True)
