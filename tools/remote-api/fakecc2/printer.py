"""The Elegoo Centauri Carbon 2 behaviour behind the fake broker and HTTP servers.

Every topic, JSON field, method id and error code here comes from the elegoo-link SDK's CC2
client (file:line references in README.md), so the slicer sees what it would see from a real
printer. Values the SDK does not read (and pycentauri did not capture) are marked as guesses.

All public methods take self.lock: they are called from the asyncio thread (MQTT, UDP discovery,
the ticker) and from the HTTP server threads (uploads, the control endpoint).
"""
import collections
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid

# Request method ids (elegoo_fdm_cc2_message_adapter.cpp COMMAND_MAPPING_TABLE, plus 1023/1042
# which the SDK has commented out but the firmware answers, per pycentauri).
METHOD_NAMES = {
    1001: "GET_PRINTER_ATTRIBUTES",
    1002: "GET_PRINTER_STATUS",
    1020: "START_PRINT",
    1021: "PAUSE_PRINT",
    1022: "STOP_PRINT",
    1023: "RESUME_PRINT",
    1042: "VIDEO_STREAM",
    1043: "UPDATE_PRINTER_NAME",
    1057: "SET_PRINTER_DOWNLOAD_FILE",
    1058: "CANCEL_PRINTER_DOWNLOAD_FILE",
    2004: "SET_AUTO_REFILL",
    2005: "GET_CANVAS_STATUS",
}
STATUS_PUSH_METHOD = 6000  # unsolicited status delta on elegoo/<sn>/api_status

# Printer error codes (the list in convertRequestErrorToElegooError and the upload codes).
OK = 0
ERR_TOKEN = 1000             # token validation failed
ERR_UNKNOWN_INTERFACE = 1001
ERR_INVALID_PARAMETER = 1003
ERR_BUSY = 1009
ERR_NOT_PRINTING = 1010      # "printer is not in printing state"
ERR_FILE_NOT_FOUND = 1021
ERR_NO_BED_LEVELING = 1026   # missing bed leveling data
ERR_UPLOAD_OFFSET = 9000     # file offset does not match the current file
ERR_UPLOAD_MD5 = 9004        # MD5 checksum failed

# machine_status (status, sub_status) per phase of the fake's state machine. These are the codes
# the SDK maps in handlePrinterStatus (status 1 idle, 2 printing + sub-states, 10 homing).
PHASE_CODES = {
    "idle": (1, 0),
    "busy": (10, 2801),          # homing: any non-idle state makes the slicer see "busy"
    "leveling": (2, 2901),
    "printing": (2, 2075),
    "pausing": (2, 2501),
    "paused": (2, 2502),
    "resuming": (2, 2401),
    "stopping": (2, 2503),
    "stopped": (2, 2504),
    "completed": (2, 2077),
}
BED_PREHEAT = (2, 1405)          # first half of "preheat"
NOZZLE_PREHEAT = (2, 1045)       # second half of "preheat"
HEATING_PHASES = {"preheat", "leveling", "printing", "pausing", "paused", "resuming", "stopping"}
RUNNING_PHASES = {"preheat", "leveling", "printing", "resuming"}  # phases that can be paused
TRANSITION_SECONDS = 1.0         # pausing/resuming/stopping/stopped last this long

AMBIENT_NOZZLE_C = 26.0
AMBIENT_BED_C = 25.0
PRINT_NOZZLE_C = 220
PRINT_BED_C = 60

TRAY_FIELDS = ("brand", "filament_type", "filament_name", "filament_code", "filament_color",
               "min_nozzle_temp", "max_nozzle_temp", "status")

CONTROL_KEYS = {
    "reset", "offline", "kick_clients", "state", "machine_status", "progress", "exceptions",
    "auto_refill", "canvas_connected", "trays", "canvas_info", "access_code", "hostname",
    "max_clients", "rate_limit", "missing_bed_leveling", "camera", "fail_next", "drop_next",
    "delay_next", "print_seconds", "preheat_seconds", "leveling_seconds", "completed_hold_seconds", "clear_log",
    "delete_files",
}


def make_tray(tray_id, brand, filament_type, name, color, status, temps=(190, 230)):
    """One CANVAS tray as GET_CANVAS_STATUS returns it (handleCanvasStatus field names).

    Tray status: 0 empty, 1 filament in the slot, 2 filament loaded into the extruder
    (ElegooLink.cpp maps 1 -> preloaded, 2 -> loaded).
    """
    return {
        "tray_id": tray_id,
        "brand": brand,
        "filament_type": filament_type,
        "filament_name": name,
        "filament_code": "0x0000" if filament_type else "",
        "filament_color": color,
        "min_nozzle_temp": temps[0] if filament_type else 0,
        "max_nozzle_temp": temps[1] if filament_type else 0,
        "status": status,
    }


def default_canvas_info():
    """One CANVAS unit: three ELEGOO PLA spools and one empty tray, auto refill off."""
    trays = [
        make_tray(0, "ELEGOO", "PLA", "PLA", "#FF0000", 1),
        make_tray(1, "ELEGOO", "PLA", "PLA", "#FFFFFF", 1),
        make_tray(2, "ELEGOO", "PLA", "PLA Matte", "#000000", 1),
        make_tray(3, "", "", "", "", 0),
    ]
    return {
        "active_canvas_id": 0,
        "active_tray_id": -1,  # -1: no tray loaded into the extruder
        "auto_refill": False,
        "canvas_list": [{"canvas_id": 0, "connected": 1, "tray_list": trays}],
    }


def json_delta(old, new):
    """The parts of `new` that differ from `old`, like the printer's 6000 status pushes.

    Nested objects are diffed key by key (the SDK merges them recursively); arrays and
    "exception_code" are sent whole because the SDK overwrites those instead of merging
    (mergeStatusUpdateJson).
    """
    delta = {}
    for key, value in new.items():
        if key not in old:
            delta[key] = value
        elif isinstance(value, dict) and isinstance(old[key], dict) and key != "exception_code":
            sub_delta = json_delta(old[key], value)
            if sub_delta:
                delta[key] = sub_delta
        elif value != old[key]:
            delta[key] = value
    return delta


def safe_file_name(name):
    """The bare file name the printer stores, or None if it is not acceptable."""
    name = os.path.basename(str(name).replace("\\", "/")).strip()
    if name in ("", ".", "..") or name.endswith(".part"):
        return None
    if any(ch in name for ch in '<>:"|?*') or any(ord(ch) < 32 for ch in name):
        return None
    return name


def parse_duration(text):
    """'1d 2h 3m 4s' (the slicer's get_time_dhms format) -> seconds."""
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(number) * units[unit] for number, unit in re.findall(r"(\d+)\s*([dhms])", text))


def read_gcode_info(path):
    """Best effort: total layers and estimated time from the G-code the slicer wrote.

    ElegooSlicer writes "; total layer number: N" in the header and, for non-Bambu printers like
    the CC2, "; estimated printing time (normal mode) = 1h 2m 3s" just before the CONFIG_BLOCK at
    the END of the file (GCode.cpp; GCodeProcessor.cpp fills the placeholders). The config block is
    ~22 KB for the CC2 profile but grows with long custom G-code, so we read the first 64 KiB and
    the last 1 MiB instead of the whole file.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(65536)
        f.seek(max(0, size - 1024 * 1024))
        tail = f.read()
    text = (head + b"\n" + tail).decode("utf-8", "replace")
    layers = re.search(r";\s*total layer number:\s*(\d+)", text)
    estimate = re.search(r";\s*estimated printing time \(normal mode\)\s*=\s*([0-9dhms ]+)", text)
    if estimate is None:
        estimate = re.search(r"total estimated time:\s*([0-9dhms ]+)", text)
    return {
        "total_layer": int(layers.group(1)) if layers else 0,
        "estimated_s": parse_duration(estimate.group(1)) if estimate else 0,
    }


def file_hashes(path):
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            md5.update(block)
            sha256.update(block)
    return md5.hexdigest(), sha256.hexdigest()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + ".%03d" % (time.time() % 1 * 1000)


def encode(message):
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class FakeCC2:
    def __init__(self, opts, log):
        self.opts = opts
        self.log = log
        self.sn = opts.sn
        self.lock = threading.RLock()
        self.received_dir = os.path.abspath(opts.received_dir)
        os.makedirs(self.received_dir, exist_ok=True)
        self.hash_cache = {}  # (name, size, mtime_ns) -> (md5, sha256)
        # Status pushes keep their sequence for the whole run (also across a control "reset"):
        # the SDK expects every 6000 id to be the previous one + 1.
        self.push_seq = 0
        self.last_pushed_status = None
        self.connected_clients = set()  # MQTT client ids with an open connection
        self.handlers = {
            1001: self._get_attributes,
            1002: self._get_status,
            1020: self._start_print,
            1021: self._pause_print,
            1022: self._stop_print,
            1023: self._resume_print,
            1042: self._video_stream,
            1043: self._update_name,
            2004: self._set_auto_refill,
            2005: self._get_canvas,
        }
        self.reset()

    def reset(self):
        """Back to the start-up state (the control endpoint's {"reset": true})."""
        with self.lock:
            opts = self.opts
            self.access_code = opts.access_code
            self.hostname = opts.hostname
            self.offline = False
            self.camera_on = not opts.no_camera
            self.max_clients = opts.max_clients
            self.rate_limit = opts.rate_limit
            self.missing_bed_leveling = False
            self.print_seconds = opts.print_seconds
            self.preheat_seconds = opts.preheat_seconds
            self.leveling_seconds = opts.leveling_seconds
            self.completed_hold_seconds = opts.completed_hold_seconds
            self.phase = "idle"
            self.phase_since = time.monotonic()
            self.last_advance = time.monotonic()
            self.resume_to = None
            self.task = None
            self.last_task = None
            self.forced_status = None    # (status, sub_status) set by the control endpoint
            self.exceptions = {}         # exception code -> unix time it appeared
            self.canvas_info = default_canvas_info()
            self.led = 1
            self.registered = {}         # registered client id -> info
            self.request_log = collections.deque(maxlen=1000)
            self.pings = 0
            self.discovery_requests = 0
            self.fail_next = {}          # method -> error code, answered once
            self.drop_next = set()       # methods left unanswered once
            self.delay_next = {}         # method -> seconds: executed now, answered that much later, once
            self.delayed_replies = []    # [(monotonic due time, topic, payload bytes)], sent by tick()
            self.request_times = {}      # client id -> recent request times (rate limit)
            self.cooldown_until = {}     # client id -> monotonic time
            self.uploads = {}            # file name -> upload in progress
            self.kick_pending = False

    # ------------------------------------------------------------------ broker hooks

    def check_credentials(self, username, password):
        """MQTT login: user "elegoo", password = access code (ElegooCC2MqttProtocol::configureConnectionOptions)."""
        with self.lock:
            return username == "elegoo" and password == self.access_code

    def is_online(self):
        with self.lock:
            return not self.offline

    def client_connected(self, client_id):
        with self.lock:
            self.connected_clients.add(client_id)

    def client_disconnected(self, client_id):
        with self.lock:
            self.connected_clients.discard(client_id)
            for registered_id in [c for c, info in self.registered.items() if info["mqtt_client"] == client_id]:
                del self.registered[registered_id]
                self.log.info("printer: %s unregistered (disconnected)" % registered_id)

    def handle_mqtt(self, mqtt_client_id, topic, payload):
        """A client published `payload` on `topic`. Returns [(topic, payload bytes)] to publish."""
        with self.lock:
            self._advance()
            parts = topic.split("/")
            if len(parts) < 3 or parts[0] != "elegoo" or parts[1] != self.sn:
                return []  # not for this printer (or one of our own topics echoed back)
            try:
                message = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self.log.info("printer: ignoring non-JSON message on %s" % topic)
                return []
            if not isinstance(message, dict):
                return []
            if len(parts) == 3 and parts[2] == "api_register":
                return self._register(mqtt_client_id, message)
            if len(parts) == 4 and parts[3] == "api_request":
                client_id = parts[2]
                reply_topic = "elegoo/%s/%s/api_response" % (self.sn, client_id)
                if message.get("type") == "PING":
                    # Heartbeat (ElegooCC2MqttProtocol::createHeartbeatMessage / handleHeartbeatResponse).
                    self.pings += 1
                    return [(reply_topic, encode({"type": "PONG"}))]
                if client_id not in self.registered:
                    self.log.info("printer: request from unregistered client %s ignored" % client_id)
                    self._log_request(client_id, message, outcome="ignored (client not registered)")
                    return []
                response = self._handle_request(client_id, message)
                replies = [] if response is None else [(reply_topic, encode(response))]
                return replies + self._status_push()
            return []

    # ------------------------------------------------------------------ ticker

    def tick(self):
        """Called every --tick seconds. Returns ([(topic, payload)], kick_all_clients)."""
        with self.lock:
            self._advance()
            kick = self.kick_pending
            self.kick_pending = False
            now = time.monotonic()
            due = [(topic, payload) for when, topic, payload in self.delayed_replies if when <= now]
            self.delayed_replies = [r for r in self.delayed_replies if r[0] > now]
            # Pushes first: a real CC2 reports the new state while it is still busy with the
            # command and answers when the movement is done.
            return self._status_push() + due, kick

    # ------------------------------------------------------------------ UDP discovery

    def discovery_reply(self, data):
        """Answer the SDK's discovery probe {"id": 0, "method": 7000} (ElegooFdmCC2DiscoveryStrategy)."""
        with self.lock:
            if self.offline:
                return None
            try:
                request = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return None
            if not isinstance(request, dict) or request.get("method") != 7000:
                return None
            self.discovery_requests += 1
            return encode({
                "id": request.get("id", 0),
                "result": {
                    "host_name": self.hostname,
                    "machine_model": self.opts.model,
                    "sn": self.sn,
                    "token_status": 1,  # 1: an access code is required
                    "lan_status": 1,    # 1: "LAN only" mode, so the SDK treats it as a LAN printer
                },
            })

    # ------------------------------------------------------------------ HTTP (:80)

    def system_info(self, token):
        """GET /system/info: how the SDK learns the serial number when the printer entry has none."""
        with self.lock:
            if token != self.access_code:
                return 401, {"error_code": ERR_TOKEN}
            return 200, {
                "error_code": OK,
                # Only "sn" is read by the SDK (and pycentauri); the other fields are guesses.
                "system_info": {"sn": self.sn, "hostname": self.hostname,
                                "machine_model": self.opts.model, "ota_version": self.opts.firmware},
            }

    def upload_chunk(self, token, raw_name, file_md5, content_range, body):
        """PUT /upload: one chunk of at most 1 MiB (ElegooFdmCC2HttpTransfer::uploadChunkWithSession).

        Returns (http status, json body). Chunks must arrive in order; the file is complete when
        the last byte arrives and its MD5 matches X-File-MD5 (the whole-file MD5, sent with every
        chunk). Until then it is kept as <name>.part so it never shows up as received.
        """
        with self.lock:
            if token != self.access_code:
                return 401, {"error_code": ERR_TOKEN}
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range or "")
            name = safe_file_name(raw_name or "")
            if not match or not name or not file_md5:
                return 200, {"error_code": ERR_INVALID_PARAMETER}
            start, end, total = (int(x) for x in match.groups())
            if end - start + 1 != len(body) or end >= total:
                return 200, {"error_code": ERR_INVALID_PARAMETER}
            part_path = os.path.join(self.received_dir, name + ".part")
            upload = self.uploads.get(name)
            if start == 0:
                upload = {"name": name, "total": total, "received": 0, "md5": file_md5.lower(),
                          "started": now_iso()}
                self.uploads[name] = upload
                open(part_path, "wb").close()
                self.log.info("http: receiving %s (%d bytes)" % (name, total))
            elif upload is None or start != upload["received"] or total != upload["total"]:
                return 200, {"error_code": ERR_UPLOAD_OFFSET}
            with open(part_path, "ab") as f:
                f.write(body)
            upload["received"] += len(body)
            if upload["received"] < total:
                return 200, {"error_code": OK}

            del self.uploads[name]
            md5, _ = file_hashes(part_path)
            if md5 != upload["md5"]:
                os.remove(part_path)
                self.log.info("http: %s rejected, MD5 %s does not match X-File-MD5" % (name, md5))
                return 200, {"error_code": ERR_UPLOAD_MD5}
            os.replace(part_path, os.path.join(self.received_dir, name))
            self.log.info("http: received %s (%d bytes, md5 %s)" % (name, total, md5))
            return 200, {"error_code": OK}

    def download_path(self, token, raw_name):
        """GET /download?file_name=...: path of a received file, or an HTTP error status."""
        with self.lock:
            if token != self.access_code:
                return 401, None
            name = safe_file_name(raw_name or "")
            path = os.path.join(self.received_dir, name) if name else None
            if not path or not os.path.isfile(path):
                return 404, None
            return 200, path

    def camera_enabled(self):
        with self.lock:
            return self.camera_on and not self.offline

    # ------------------------------------------------------------------ control endpoint (:18080)

    def snapshot(self):
        with self.lock:
            self._advance()
            return self._snapshot()

    def apply_control(self, body):
        """POST /state. Returns (http status, json body)."""
        with self.lock:
            unknown = sorted(k for k in body if k not in CONTROL_KEYS)
            if unknown:
                return 400, {"error": "unknown_keys", "keys": unknown, "known": sorted(CONTROL_KEYS)}
            self._advance()
            try:
                if body.get("reset"):
                    self.reset()
                    # Like a reboot: drop the MQTT clients so they reconnect and register again
                    # (reset forgets registrations; a still-connected client would be ignored).
                    self.kick_pending = True
                for key, value in body.items():
                    if key != "reset":
                        self._apply_control_key(key, value)
            except (TypeError, ValueError, KeyError) as error:
                return 400, {"error": "bad_value", "detail": str(error)}
            return 200, self._snapshot()

    # ------------------------------------------------------------------ registration and requests

    def _register(self, mqtt_client_id, message):
        """elegoo/<sn>/api_register -> elegoo/<sn>/<request_id>/register_response.

        The SDK waits for {"client_id": <its id>, "error": "ok"}; "too many clients" is the one
        error text it recognises (ElegooCC2MqttProtocol::validateRegistrationResponse).
        """
        client_id = str(message.get("client_id", ""))
        request_id = str(message.get("request_id", ""))
        if not client_id or not request_id:
            return []
        if client_id not in self.registered and len(self.registered) >= self.max_clients:
            error = "too many clients"
        else:
            self.registered[client_id] = {"mqtt_client": mqtt_client_id, "since": now_iso()}
            error = "ok"
        self.log.info("printer: register %s -> %s" % (client_id, error))
        self.request_log.append({"time": now_iso(), "client_id": client_id, "method": "register",
                                 "outcome": error})
        topic = "elegoo/%s/%s/register_response" % (self.sn, request_id)
        return [(topic, encode({"client_id": client_id, "error": error}))]

    def _log_request(self, client_id, message, outcome=None):
        method = message.get("method")
        entry = {"time": now_iso(), "client_id": client_id, "id": message.get("id"), "method": method,
                 "name": METHOD_NAMES.get(method, "UNKNOWN"), "params": message.get("params")}
        if outcome:
            entry["outcome"] = outcome
        self.request_log.append(entry)
        return entry

    def _handle_request(self, client_id, message):
        """{"id", "method", "params"} -> {"id", "method", "result": {..., "error_code"}} or None (no answer).

        The SDK matches the answer by "id" and needs "method" to be present (parseMessageType).
        """
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        entry = self._log_request(client_id, message)
        if self._rate_limited(client_id):
            entry["outcome"] = "dropped (rate limit cooldown)"
            return None
        if method in self.drop_next:
            self.drop_next.discard(method)
            entry["outcome"] = "dropped (drop_next)"
            self.log.info("printer: %s %s left unanswered (drop_next)" % (client_id, entry["name"]))
            return None
        if method in self.fail_next:
            result = {"error_code": self.fail_next.pop(method)}
        elif method in self.handlers:
            result = self.handlers[method](params)
        else:
            result = {"error_code": ERR_UNKNOWN_INTERFACE}
        entry["error_code"] = result["error_code"]
        self.log.info("printer: %s %s(%s) -> error_code %d" % (
            client_id, entry["name"], method, result["error_code"]))
        response = {"id": message.get("id"), "method": method, "result": result}
        if method in self.delay_next:
            # The command is carried out now, the answer comes later (a real CC2 answers pause/stop/
            # resume only after the head has parked or reheated; pycentauri measured more than the
            # SDK's 15 s timeout for a resume). tick() publishes it when it is due.
            seconds = self.delay_next.pop(method)
            topic = "elegoo/%s/%s/api_response" % (self.sn, client_id)
            self.delayed_replies.append((time.monotonic() + seconds, topic, encode(response)))
            entry["outcome"] = "answered %.1f s late (delay_next)" % seconds
            self.log.info("printer: %s %s answer held back %.1f s (delay_next)" % (client_id, entry["name"], seconds))
            return None
        return response

    def _rate_limited(self, client_id):
        """Optional (--rate-limit): 3+ requests within 1 s start a 5 s cooldown in which the
        printer silently drops answers (seen on firmware 01.03.02.51, pycentauri PROTOCOL.md)."""
        if not self.rate_limit:
            return False
        now = time.monotonic()
        if now < self.cooldown_until.get(client_id, 0):
            return True
        times = [t for t in self.request_times.get(client_id, []) if now - t < 1.0] + [now]
        self.request_times[client_id] = times
        if len(times) >= 3:
            self.cooldown_until[client_id] = now + 5.0
            self.log.info("printer: %s hit the rate limit, answers dropped for 5 s" % client_id)
            return True
        return False

    # ------------------------------------------------------------------ request handlers

    def _get_attributes(self, params):
        # Fields read by handlePrinterAttributes: machine_model, software_version.ota_version, sn, hostname.
        return {
            "error_code": OK,
            "hostname": self.hostname,
            "machine_model": self.opts.model,
            "sn": self.sn,
            "ip": self.opts.host,
            "protocol_version": "1.0.0",
            "software_version": {"ota_version": self.opts.firmware},
        }

    def _get_status(self, params):
        result = {"error_code": OK}
        result.update(self._full_status())
        return result

    def _start_print(self, params):
        """1020 with the params ElegooFdmCC2MessageAdapter::convertRequest builds:
        {storage_media, filename, config: {delay_video, printer_check, print_layout, bedlevel_force, slot_map}}."""
        status, sub_status = self._machine_codes()
        if status != 1 and sub_status != 2077:  # only idle or "print completed" accept a new job
            return {"error_code": ERR_BUSY}
        name = safe_file_name(params.get("filename", ""))
        path = os.path.join(self.received_dir, name) if name else None
        if params.get("storage_media", "local") != "local" or not path or not os.path.isfile(path):
            return {"error_code": ERR_FILE_NOT_FOUND}
        config = params.get("config") if isinstance(params.get("config"), dict) else {}
        if self.missing_bed_leveling and config.get("printer_check") and not config.get("bedlevel_force"):
            return {"error_code": ERR_NO_BED_LEVELING}  # ElegooLink::sendPrintTask retries with bedlevel_force
        if config.get("bedlevel_force"):
            self.missing_bed_leveling = False  # a forced leveling creates the data
        info = read_gcode_info(path)
        self._begin_task(name, config, info)
        return {"error_code": OK}

    def _pause_print(self, params):
        if self.phase not in RUNNING_PHASES:
            return {"error_code": ERR_NOT_PRINTING}
        self.resume_to = self.resume_to if self.phase == "resuming" else self.phase
        self._set_phase("pausing")
        return {"error_code": OK}

    def _resume_print(self, params):
        if self.phase not in ("pausing", "paused"):
            return {"error_code": ERR_NOT_PRINTING}
        self._set_phase("resuming")
        return {"error_code": OK}

    def _stop_print(self, params):
        if self.phase not in HEATING_PHASES or self.phase == "stopping":
            return {"error_code": ERR_NOT_PRINTING}
        self._set_phase("stopping")
        return {"error_code": OK}

    def _video_stream(self, params):
        # Shape captured by pycentauri from firmware 02.01.00.00.
        return {"error_code": OK, "url": "http://%s:%d/?action=stream" % (self.opts.host, self.opts.camera_port)}

    def _update_name(self, params):
        hostname = params.get("hostname")
        if not isinstance(hostname, str) or not hostname.strip():
            return {"error_code": ERR_INVALID_PARAMETER}
        self.hostname = hostname.strip()
        return {"error_code": OK}

    def _set_auto_refill(self, params):
        if not isinstance(params.get("auto_refill"), bool):
            return {"error_code": ERR_INVALID_PARAMETER}
        self.canvas_info["auto_refill"] = params["auto_refill"]
        return {"error_code": OK}

    def _get_canvas(self, params):
        return {"error_code": OK, "canvas_info": copy.deepcopy(self.canvas_info)}

    # ------------------------------------------------------------------ print state machine

    def _begin_task(self, name, config, gcode_info):
        if self.task:  # a new job while "print completed" is still shown
            self._close_task("completed" if self.phase == "completed" else "replaced")
        self.forced_status = None
        self.task = {
            "filename": name,
            "uuid": str(uuid.uuid4()),
            "config": config,
            "begin_time": int(time.time()),
            "started": time.monotonic(),
            "print_elapsed": 0.0,  # real seconds spent in the "printing" phase
            "total_layer": gcode_info["total_layer"] or 100,
            "estimated_s": gcode_info["estimated_s"] or int(self.print_seconds),
            "result": None,
        }
        self.resume_to = None
        self._load_tray(config.get("slot_map"))
        self._set_phase("preheat")
        self.log.info("printer: printing %s (%d layers, %d s estimated, simulated in %.0f s)" % (
            name, self.task["total_layer"], self.task["estimated_s"], self.print_seconds))

    def _set_phase(self, phase):
        if phase != self.phase:
            self.log.info("printer: %s -> %s" % (self.phase, phase))
        self.phase = phase
        self.phase_since = time.monotonic()

    def _close_task(self, result):
        """The current job becomes last_task (with its result) and its tray is unloaded."""
        if self.task:
            self.task["result"] = result
            self.task["end_time"] = int(time.time())
            self.last_task = self.task
        self.task = None
        self._unload_tray()

    def _finish_task(self, result):
        self._close_task(result)
        self._set_phase("idle")

    def _advance(self):
        """Move the print state machine forward to now."""
        now = time.monotonic()
        elapsed = now - self.last_advance
        self.last_advance = now
        in_phase = now - self.phase_since
        phase = self.phase
        if phase == "printing":
            self.task["print_elapsed"] += elapsed
            if self.task["print_elapsed"] >= self.print_seconds:
                self.task["print_elapsed"] = self.print_seconds
                self._set_phase("completed")
        elif phase == "preheat" and in_phase >= self.preheat_seconds:
            leveling = self.task["config"].get("printer_check") and self.leveling_seconds > 0
            self._set_phase("leveling" if leveling else "printing")
        elif phase == "leveling" and in_phase >= self.leveling_seconds:
            self._set_phase("printing")
        elif phase == "pausing" and in_phase >= TRANSITION_SECONDS:
            self._set_phase("paused")
        elif phase == "resuming" and in_phase >= TRANSITION_SECONDS:
            self._set_phase(self.resume_to or "printing")
        elif phase == "stopping" and in_phase >= TRANSITION_SECONDS:
            self._set_phase("stopped")
        elif phase == "stopped" and in_phase >= TRANSITION_SECONDS:
            self._finish_task("stopped")
        elif phase == "completed" and 0 < self.completed_hold_seconds <= in_phase:
            self._finish_task("completed")

    def _machine_codes(self):
        if self.forced_status:
            return self.forced_status
        if self.phase == "preheat":
            first_half = time.monotonic() - self.phase_since < self.preheat_seconds / 2
            return BED_PREHEAT if first_half else NOZZLE_PREHEAT
        return PHASE_CODES[self.phase]

    def _find_tray(self, canvas_id, tray_id):
        for canvas in self.canvas_info.get("canvas_list", []):
            if canvas.get("canvas_id") == canvas_id:
                for tray in canvas.get("tray_list", []):
                    if tray.get("tray_id") == tray_id:
                        return tray
        return None

    def _load_tray(self, slot_map):
        """A print with a CANVAS mapping loads its first mapped tray (status 2, active tray)."""
        if not isinstance(slot_map, list) or not slot_map or not isinstance(slot_map[0], dict):
            return
        canvas_id = slot_map[0].get("canvas_id", 0)
        tray_id = slot_map[0].get("tray_id", 0)
        tray = self._find_tray(canvas_id, tray_id)
        if tray is not None and tray.get("status") == 1:
            tray["status"] = 2
        self.canvas_info["active_canvas_id"] = canvas_id
        self.canvas_info["active_tray_id"] = tray_id

    def _unload_tray(self):
        tray = self._find_tray(self.canvas_info.get("active_canvas_id"), self.canvas_info.get("active_tray_id"))
        if tray is not None and tray.get("status") == 2:
            tray["status"] = 1
        self.canvas_info["active_tray_id"] = -1

    # ------------------------------------------------------------------ status JSON

    def _task_numbers(self):
        """(progress, current_layer, total_layer, print_duration, remaining, total_duration).

        The print is simulated in --print-seconds but reported on the G-code's own time scale
        (its estimate), so a 1 h print shows sensible durations while finishing in a minute.
        """
        task = self.task
        if task is None:
            return 0, 0, 0, 0, 0, 0
        fraction = min(1.0, task["print_elapsed"] / self.print_seconds) if self.print_seconds > 0 else 1.0
        if self.phase == "completed":
            fraction = 1.0
        print_duration = int(fraction * task["estimated_s"])
        waited = max(0.0, time.monotonic() - task["started"] - task["print_elapsed"])
        current_layer = max(1, int(fraction * task["total_layer"])) if self.phase != "preheat" else 0
        return (int(fraction * 100), current_layer, task["total_layer"], print_duration,
                task["estimated_s"] - print_duration, print_duration + int(waited))

    def _temperatures(self):
        if self.phase not in HEATING_PHASES:
            return (AMBIENT_NOZZLE_C, 0), (AMBIENT_BED_C, 0)
        if self.phase == "preheat" and self.preheat_seconds > 0:
            fraction = min(1.0, (time.monotonic() - self.phase_since) / self.preheat_seconds)
        else:
            fraction = 1.0
        nozzle = round(AMBIENT_NOZZLE_C + (PRINT_NOZZLE_C - AMBIENT_NOZZLE_C) * fraction, 1)
        bed = round(AMBIENT_BED_C + (PRINT_BED_C - AMBIENT_BED_C) * fraction, 1)
        return (nozzle, PRINT_NOZZLE_C), (bed, PRINT_BED_C)

    def _full_status(self):
        """The GET_PRINTER_STATUS (1002) result, in the shape pycentauri captured from a CC2.

        The SDK reads machine_status, print_status, extruder, heater_bed, ztemperature_sensor,
        fans, led, external_device, canvas_info and exception (handlePrinterStatus).
        """
        status, sub_status = self._machine_codes()
        progress, layer, total_layer, print_s, remaining_s, total_s = self._task_numbers()
        (nozzle, nozzle_target), (bed, bed_target) = self._temperatures()
        printing = self.phase == "printing"
        heating = self.phase in HEATING_PHASES
        task = self.task or {}
        if printing:  # the head wanders around the bed while printing
            angle = task["print_elapsed"]
            position = {"x": round(128 + 60 * math.cos(angle), 2), "y": round(128 + 60 * math.sin(angle), 2),
                        "z": round(layer * 0.2, 2), "extruder": round(task["print_elapsed"] * 12.0, 2),
                        "speed": 6000, "speed_mode": 1}
        else:
            position = {"x": 0.0, "y": 0.0, "z": 0.0, "extruder": 0.0, "speed": 0, "speed_mode": 1}
        canvas_connected = any(c.get("connected") for c in self.canvas_info.get("canvas_list", []))
        # print_status.state is not read by the SDK; these words are guesses.
        state_word = {"preheat": "printing", "leveling": "printing", "printing": "printing",
                      "resuming": "printing", "pausing": "paused", "paused": "paused",
                      "stopping": "stopped", "stopped": "stopped", "completed": "completed"}
        return {
            "machine_status": {
                "status": status,
                "sub_status": sub_status,
                "sub_status_reason_code": 0,
                "exception_status": sorted(self.exceptions),
                "progress": progress,
            },
            "print_status": {
                "state": state_word.get(self.phase, ""),
                "filename": task.get("filename", ""),
                "uuid": task.get("uuid", ""),
                "current_layer": layer,
                "total_layer": total_layer,
                "print_duration": print_s,
                "remaining_time_sec": remaining_s,
                "total_duration": total_s,
                "bed_mesh_detect": bool(task.get("config", {}).get("printer_check", False)),
                "filament_detect": False,
                "enable": True,
            },
            "extruder": {"temperature": nozzle, "target": nozzle_target,
                         "filament_detect_enable": 1, "filament_detected": 1},
            "heater_bed": {"temperature": bed, "target": bed_target},
            "ztemperature_sensor": {"temperature": 28, "measured_max_temperature": 0,
                                    "measured_min_temperature": 0},
            "fans": {
                "fan": {"speed": 255.0 if printing else 0.0},
                "aux_fan": {"speed": 0.0},
                "box_fan": {"speed": 0.0},
                "controller_fan": {"speed": 255.0 if heating else 0.0},
                "heater_fan": {"speed": 255.0 if nozzle > 50 else 0.0},
            },
            "gcode_move": position,
            "led": {"status": self.led},
            "tool_head": {"homed_axes": "xyz" if heating else ""},
            "external_device": {"camera": self.camera_on, "u_disk": False,
                                "type": "0303" if canvas_connected else "0"},
            "canvas_info": copy.deepcopy(self.canvas_info),
            "exception": {"exception_code": {str(code): {"time": t} for code, t in sorted(self.exceptions.items())}},
        }

    def _status_push(self):
        """A 6000 push with what changed since the previous push, or nothing."""
        status = self._full_status()
        if self.last_pushed_status is None:
            self.last_pushed_status = status  # baseline: clients start from a 1002 full status
            return []
        delta = json_delta(self.last_pushed_status, status)
        if not delta:
            return []
        self.push_seq += 1
        self.last_pushed_status = status
        message = {"id": self.push_seq, "method": STATUS_PUSH_METHOD, "result": delta}
        return [("elegoo/%s/api_status" % self.sn, encode(message))]

    # ------------------------------------------------------------------ control helpers

    def _apply_control_key(self, key, value):
        if key == "offline":
            self.offline = bool(value)
            if self.offline:
                self.kick_pending = True
                self.registered.clear()
                self.delayed_replies = []  # answers to dropped connections are never sent
            self.log.info("control: printer %s" % ("OFFLINE" if self.offline else "online"))
        elif key == "kick_clients":
            self.kick_pending = bool(value)
            self.registered.clear()
            self.delayed_replies = []
        elif key == "state":
            self._force_state(value)
        elif key == "machine_status":
            if value is None:
                self.forced_status = None
            else:
                self.forced_status = (int(value["status"]), int(value.get("sub_status", 0)))
        elif key == "progress":
            if self.task is None:
                raise ValueError("progress needs a print in progress")
            self.task["print_elapsed"] = max(0.0, min(100.0, float(value))) / 100.0 * self.print_seconds
        elif key == "exceptions":
            self.exceptions = {int(code): int(time.time()) for code in value}
        elif key == "auto_refill":
            self.canvas_info["auto_refill"] = bool(value)
        elif key == "canvas_connected":
            for canvas in self.canvas_info.get("canvas_list", []):
                canvas["connected"] = 1 if value else 0
        elif key == "trays":
            for patch in value:
                tray = self._find_tray(int(patch.get("canvas_id", 0)), int(patch["tray_id"]))
                if tray is None:
                    raise ValueError("no tray %s in canvas %s" % (patch["tray_id"], patch.get("canvas_id", 0)))
                tray.update({k: v for k, v in patch.items() if k in TRAY_FIELDS})
        elif key == "canvas_info":
            canvases = value.get("canvas_list") if isinstance(value, dict) else None
            if not isinstance(canvases, list) or not all(
                    isinstance(c, dict) and isinstance(c.get("tray_list", []), list)
                    and all(isinstance(t, dict) for t in c.get("tray_list", [])) for c in canvases):
                raise ValueError("canvas_info needs canvas_list: [{canvas_id, connected, tray_list: [{...}]}] "
                                 "(the GET_CANVAS_STATUS shape)")
            self.canvas_info = copy.deepcopy(value)
        elif key == "access_code":
            self.access_code = str(value)
        elif key == "hostname":
            self.hostname = str(value)
        elif key == "max_clients":
            self.max_clients = int(value)
        elif key == "rate_limit":
            self.rate_limit = bool(value)
        elif key == "missing_bed_leveling":
            self.missing_bed_leveling = bool(value)
        elif key == "camera":
            self.camera_on = bool(value)
        elif key == "fail_next":
            self.fail_next[int(value["method"])] = int(value["error_code"])
        elif key == "drop_next":
            self.drop_next.add(int(value))
        elif key == "delay_next":
            seconds = float(value["seconds"])
            if seconds < 0:
                raise ValueError("delay_next seconds must be >= 0")
            self.delay_next[int(value["method"])] = seconds
        elif key in ("print_seconds", "preheat_seconds", "leveling_seconds", "completed_hold_seconds"):
            if float(value) < 0:
                raise ValueError("%s must be >= 0" % key)
            setattr(self, key, float(value))
        elif key == "clear_log":
            if value:
                self.request_log.clear()
                self.pings = 0
                self.discovery_requests = 0
        elif key == "delete_files":
            for raw_name in value:
                name = safe_file_name(raw_name)
                path = os.path.join(self.received_dir, name) if name else None
                if path and os.path.isfile(path):
                    os.remove(path)
                    self.log.info("control: deleted received file %s" % name)

    def _force_state(self, state):
        """Jump to a state without a real print job (for tests of the slicer's status handling)."""
        if state not in ("idle", "busy", "printing", "paused", "completed"):
            raise ValueError("state must be idle, busy, printing, paused or completed")
        self.forced_status = None
        if state in ("idle", "busy"):
            if self.task:
                self._finish_task("stopped")
            self._set_phase(state)
            return
        if self.task is None:
            self._begin_task("forced.gcode", {}, {"total_layer": 100, "estimated_s": 3600})
        if state == "completed":
            self.task["print_elapsed"] = self.print_seconds
        if state == "paused":
            self.resume_to = "printing"
        self._set_phase(state)

    def _received_files(self):
        files = []
        for name in sorted(os.listdir(self.received_dir)):
            path = os.path.join(self.received_dir, name)
            if name.endswith(".part") or not os.path.isfile(path):
                continue
            stat = os.stat(path)
            key = (name, stat.st_size, stat.st_mtime_ns)
            if key not in self.hash_cache:
                self.hash_cache[key] = file_hashes(path)
            md5, sha256 = self.hash_cache[key]
            files.append({"name": name, "size": stat.st_size, "md5": md5, "sha256": sha256,
                          "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime))})
        return files

    def _slicer_status_name(self, status, sub_status):
        """What the slicer should show, following ElegooLink.cpp parseElegooStatus + the SDK mapping."""
        if status == 1:
            return "idle"
        if status == 2:
            return {2501: "pausing", 2502: "paused", 2505: "paused", 2077: "print_completed"}.get(sub_status, "printing")
        return {0: "initializing", 3: "filament_operating", 4: "filament_operating", 5: "auto_leveling",
                6: "pid_calibrating", 7: "resonance_testing", 8: "self_checking", 9: "updating",
                10: "homing", 11: "file_transferring", 12: "video_composing", 13: "extruder_operating",
                14: "emergency_stop", 15: "power_loss_recovery"}.get(status, "unknown")

    def _task_summary(self, task):
        if task is None:
            return None
        summary = {k: task[k] for k in ("filename", "uuid", "config", "begin_time", "total_layer",
                                         "estimated_s", "result") if k in task}
        if "end_time" in task:
            summary["end_time"] = task["end_time"]
        return summary

    def _snapshot(self):
        status = self._full_status()
        machine = status["machine_status"]
        return {
            "printer": {
                "sn": self.sn,
                "model": self.opts.model,
                "hostname": self.hostname,
                "firmware": self.opts.firmware,
                "access_code": self.access_code,  # the fake's own test code, not a real secret
                "host": self.opts.host,
                "ports": {"mqtt": self.opts.mqtt_port, "http": self.opts.http_port,
                          "camera": self.opts.camera_port, "discovery": self.opts.discovery_port,
                          "control": self.opts.control_port},
            },
            "offline": self.offline,
            "camera": self.camera_on,
            "phase": self.phase,
            "machine_status": machine,
            "expected_slicer_status": self._slicer_status_name(machine["status"], machine["sub_status"]),
            "print_status": status["print_status"],
            "task": self._task_summary(self.task),
            "last_task": self._task_summary(self.last_task),
            "canvas_info": copy.deepcopy(self.canvas_info),
            "mqtt_connections": sorted(self.connected_clients),
            "registered_clients": [dict(client_id=c, **info) for c, info in sorted(self.registered.items())],
            "received_files": self._received_files(),
            "uploads_in_progress": [dict(u) for u in self.uploads.values()],
            "requests": list(self.request_log),
            "pings": self.pings,
            "discovery_requests": self.discovery_requests,
            "status_pushes": self.push_seq,
            "settings": {
                "print_seconds": self.print_seconds,
                "preheat_seconds": self.preheat_seconds,
                "leveling_seconds": self.leveling_seconds,
                "completed_hold_seconds": self.completed_hold_seconds,
                "max_clients": self.max_clients,
                "rate_limit": self.rate_limit,
                "missing_bed_leveling": self.missing_bed_leveling,
                "fail_next": {str(m): c for m, c in self.fail_next.items()},
                "drop_next": sorted(self.drop_next),
                "delay_next": {str(m): s for m, s in self.delay_next.items()},
            },
            "delayed_replies_pending": len(self.delayed_replies),
        }
