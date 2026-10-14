"""Self-test for the fake CC2: drives it exactly the way the elegoo-link SDK drives a real printer.

Usage:
  py -3 -I tools\\remote-api\\fakecc2\\selftest.py            start a private fake on spare ports and test it (default)
  py -3 -I tools\\remote-api\\fakecc2\\selftest.py --running  test a fake already running on the default ports
                                                 (it changes its timings and resets it at the end)

The MQTT client below is written independently of the fake's broker (mqtt_broker.py) so that a
bug in one is not hidden by the same bug in the other. The sequence follows the SDK:
UDP discovery -> GET /system/info -> MQTT CONNECT (user "elegoo", password = access code) ->
SUBSCRIBE api_response/api_status/register_response -> api_register -> 1002 status -> 1001
attributes -> PING -> 2005 canvas -> 2004 auto refill -> PUT /upload in 1 MiB chunks -> 1020 start
-> 6000 status pushes -> 1021 pause -> 1023 resume -> 1022 stop.
It also checks the JSON types of every status/CANVAS field the SDK reads (JsonUtils getters fall
back to defaults on a wrong type), the slicer's view of the trays (A1.. names, tray status), and
that add_fake_printer.py writes the printer_list.json entry the slicer reads back (rules taken
from this repository's src\\ when present).
"""
import argparse
import copy
import hashlib
import http.client
import itertools
import json
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_PORTS = {"mqtt": 1883, "http": 80, "camera": 8080, "discovery": 52700, "control": 18080}
PRIVATE_PORTS = {"mqtt": 21883, "http": 21880, "camera": 21881, "discovery": 52701, "control": 21882}
CUBE_NAME = "selftest_cube.gcode"
UNICODE_NAME = "selftest peça ç.gcode"  # non-ASCII on purpose: X-File-Name carries raw UTF-8


# ---------------------------------------------------------------------------- tiny MQTT client

def mqtt_string(text):
    data = text.encode("utf-8")
    return struct.pack("!H", len(data)) + data


def mqtt_packet(first_byte, body):
    length = len(body)
    encoded = bytearray()
    while True:
        byte = length % 128
        length //= 128
        encoded.append(byte | (0x80 if length else 0))
        if not length:
            break
    return bytes([first_byte]) + bytes(encoded) + body


class MiniMqttClient:
    """Just enough MQTT 3.1.1 for the test: CONNECT, SUBSCRIBE, PUBLISH QoS 0/1, PINGREQ, DISCONNECT."""

    def __init__(self, host, port, client_id, username, password, protocol=("MQTT", 4), keepalive=60):
        self.host, self.port = host, port
        self.client_id, self.username, self.password = client_id, username, password
        self.protocol, self.keepalive = protocol, keepalive
        self.sock = None
        self.alive = False
        self.packet_ids = itertools.count(1)
        self.cv = threading.Condition()
        self.acks = []    # (packet type, packet id or None, body)
        self.inbox = []   # [topic, payload bytes, consumed]
        self.send_lock = threading.Lock()

    def connect(self, timeout=5):
        """Returns the CONNACK return code, or -1 when the server closed or never answered."""
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
            flags = 0x02 | 0x80 | 0x40  # clean session, username, password
            body = (mqtt_string(self.protocol[0]) + bytes([self.protocol[1], flags])
                    + struct.pack("!H", self.keepalive) + mqtt_string(self.client_id)
                    + mqtt_string(self.username) + mqtt_string(self.password))
            self.sock.sendall(mqtt_packet(0x10, body))
            packet_type, _, ack = self._read_packet()
        except (OSError, ConnectionError):
            self.close()
            return -1
        if packet_type != 2 or len(ack) != 2:
            self.close()
            return -1
        if ack[1] != 0:
            self.close()
            return ack[1]
        self.sock.settimeout(None)
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        return 0

    def _recv_exact(self, count):
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError("closed by server")
            data += chunk
        return data

    def _read_packet(self):
        first = self._recv_exact(1)[0]
        length, multiplier = 0, 1
        while True:
            byte = self._recv_exact(1)[0]
            length += (byte & 0x7F) * multiplier
            multiplier *= 128
            if not byte & 0x80:
                break
        return first >> 4, first & 0x0F, self._recv_exact(length) if length else b""

    def _reader(self):
        try:
            while True:
                packet_type, flags, body = self._read_packet()
                if packet_type == 3:  # PUBLISH
                    qos = (flags >> 1) & 3
                    topic_len = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2 + topic_len].decode("utf-8")
                    pos = 2 + topic_len
                    if qos:
                        packet_id = body[pos:pos + 2]
                        pos += 2
                        self._send(mqtt_packet(0x40, packet_id))  # PUBACK
                    with self.cv:
                        self.inbox.append([topic, body[pos:], False])
                        self.cv.notify_all()
                else:
                    packet_id = struct.unpack("!H", body[:2])[0] if len(body) >= 2 else None
                    with self.cv:
                        self.acks.append((packet_type, packet_id, body))
                        self.cv.notify_all()
        except (OSError, ConnectionError, struct.error):
            pass
        with self.cv:
            self.alive = False
            self.cv.notify_all()

    def _send(self, data):
        with self.send_lock:
            self.sock.sendall(data)

    def _wait(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        with self.cv:
            while True:
                result = predicate()
                if result is not None:
                    return result
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.cv.wait(remaining)

    def _wait_ack(self, packet_type, packet_id, timeout=5):
        def find():
            for ack in self.acks:
                if ack[0] == packet_type and ack[1] == packet_id:
                    self.acks.remove(ack)
                    return ack
            return None
        return self._wait(find, timeout)

    def subscribe(self, topics, qos=1):
        packet_id = next(self.packet_ids)
        body = struct.pack("!H", packet_id) + b"".join(mqtt_string(t) + bytes([qos]) for t in topics)
        self._send(mqtt_packet(0x82, body))
        ack = self._wait_ack(9, packet_id)
        return list(ack[2][2:]) if ack else None

    def publish(self, topic, payload, qos=1):
        """QoS 1 waits for PUBACK (the SDK waits up to 2 s for the delivery token)."""
        body = mqtt_string(topic)
        packet_id = None
        if qos:
            packet_id = next(self.packet_ids)
            body += struct.pack("!H", packet_id)
        self._send(mqtt_packet(0x30 | (qos << 1), body + payload))
        return True if not qos else self._wait_ack(4, packet_id, 2) is not None

    def ping(self, timeout=5):
        self._send(mqtt_packet(0xC0, b""))
        return self._wait_ack(13, None, timeout) is not None

    def wait_message(self, predicate, timeout):
        """First unconsumed message (in arrival order) for which predicate(topic, json) is true."""
        def find():
            for message in self.inbox:
                if not message[2]:
                    try:
                        obj = json.loads(message[1].decode("utf-8"))
                    except ValueError:
                        obj = None
                    if predicate(message[0], obj):
                        message[2] = True
                        return message[0], obj
            return None
        return self._wait(find, timeout)

    def take_messages(self, topic):
        with self.cv:
            taken = [m for m in self.inbox if not m[2] and m[0] == topic]
            for m in taken:
                m[2] = True
        return [json.loads(m[1].decode("utf-8")) for m in taken]

    def wait_closed(self, timeout):
        return self._wait(lambda: True if not self.alive else None, timeout) is not None

    def disconnect(self):
        try:
            self._send(mqtt_packet(0xE0, b""))
        except OSError:
            pass
        self.close()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.alive = False


# ---------------------------------------------------------------------------- SDK-like session

PRINTER_REQUEST_IDS = itertools.count(100000)  # BaseMessageAdapter::generatePrinterRequestId


def merge_status(target, source):
    """ElegooFdmCC2MessageAdapter::mergeStatusUpdateJson."""
    for key, value in source.items():
        if key == "exception_code":
            target[key] = value
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_status(target[key], value)
        else:
            target[key] = value


class SdkLikeClient:
    """What ElegooCC2MqttProtocol + ElegooFdmCC2MessageAdapter do on the wire."""

    def __init__(self, cfg, client_id=None, password=None):
        self.cfg = cfg
        self.client_id = client_id or "1_PC_%d" % random.randint(1000, 9999)
        self.request_id = self.client_id + "_req"
        sn = cfg.sn
        self.topic_request = "elegoo/%s/%s/api_request" % (sn, self.client_id)
        self.topic_response = "elegoo/%s/%s/api_response" % (sn, self.client_id)
        self.topic_status = "elegoo/%s/api_status" % sn
        self.topic_register_response = "elegoo/%s/%s/register_response" % (sn, self.request_id)
        self.mqtt = MiniMqttClient(cfg.host, cfg.ports["mqtt"], self.client_id, "elegoo",
                                   password if password is not None else cfg.access_code)
        self.status = None       # merged status cache, like the SDK's cachedFullStatusJson_
        self.last_request_id = None
        self.last_push_id = None
        self.push_count = 0
        self.push_gaps = 0
        self.bad_pushes = 0
        self.seen_codes = set()  # every (status, sub_status) seen in the merged cache

    def connect(self):
        return self.mqtt.connect()

    def subscribe(self):
        return self.mqtt.subscribe([self.topic_response, self.topic_status, self.topic_register_response], 1)

    def register(self, timeout=5):
        payload = json.dumps({"client_id": self.client_id, "request_id": self.request_id}).encode()
        if not self.mqtt.publish("elegoo/%s/api_register" % self.cfg.sn, payload, 1):
            return None
        reply = self.mqtt.wait_message(lambda t, o: t == self.topic_register_response, timeout)
        if reply is None or reply[1] is None or reply[1].get("client_id") != self.client_id:
            return None
        return reply[1].get("error")

    def connect_and_register(self):
        if self.connect() != 0 or self.subscribe() != [1, 1, 1]:
            return None
        return self.register()

    def request(self, method, params=None, timeout=5):
        request_id = next(PRINTER_REQUEST_IDS)
        self.last_request_id = request_id
        message = {"id": request_id, "method": method, "params": params if params is not None else {}}
        if not self.mqtt.publish(self.topic_request, json.dumps(message).encode(), 1):
            return None
        reply = self.mqtt.wait_message(
            lambda t, o: t == self.topic_response and isinstance(o, dict) and o.get("id") == request_id, timeout)
        if reply is None:
            return None
        response = reply[1]
        if method == 1002 and response.get("result", {}).get("error_code") == 0:
            self.status = copy.deepcopy(response["result"])  # full status replaces the cache
        return response

    def ping(self, timeout=5):
        if not self.mqtt.publish(self.topic_request, b'{"type":"PING"}', 1):
            return False
        reply = self.mqtt.wait_message(
            lambda t, o: t == self.topic_response and isinstance(o, dict) and o.get("type") == "PONG", timeout)
        return reply is not None

    def process_pushes(self):
        for push in self.mqtt.take_messages(self.topic_status):
            if push.get("method") != 6000 or not isinstance(push.get("result"), dict):
                self.bad_pushes += 1
                continue
            push_id = push.get("id")
            # handlePrinterStatus: a push is continuous when id == previous + 1 (or 0).
            if self.last_push_id is not None and push_id != self.last_push_id + 1 and push_id != 0:
                self.push_gaps += 1
            self.last_push_id = push_id
            self.push_count += 1
            if self.status is not None:
                merge_status(self.status, push["result"])
                self.seen_codes.add(machine(self.status))

    def wait_status(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while True:
            self.process_pushes()
            if self.status is not None and predicate(self.status):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def machine(status):
    return status["machine_status"]["status"], status["machine_status"]["sub_status"]


# ---------------------------------------------------------------------------- what the SDK / slicer read

def is_int(value):
    """A JSON integer, as JsonUtils::safeGetInt / safeGetInt64 require (is_number_integer): a float
    like 12.0 or a bool makes the SDK silently use its default (0) instead."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_hex_color(value):
    """StandardColorMatcher::parse_hex (PrinterMmsManager): optional '#' + exactly 6 hex digits."""
    body = value[1:] if value.startswith("#") else value
    return len(body) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in body)


def canvas_type_problems(canvas, where="canvas_info"):
    """Fields of a CANVAS object whose JSON type would make ElegooFdmCC2MessageAdapter::
    handleCanvasStatus fall back to a default (it uses safeGetInt/safeGetBool/safeGetString)."""
    problems = []
    if not isinstance(canvas, dict):
        return [where]
    for key in ("active_canvas_id", "active_tray_id"):
        if not is_int(canvas.get(key)):
            problems.append("%s.%s" % (where, key))
    if not isinstance(canvas.get("auto_refill"), bool):
        problems.append(where + ".auto_refill")
    units = canvas.get("canvas_list")
    if not isinstance(units, list):
        return problems + [where + ".canvas_list"]
    for u, unit in enumerate(units):
        here = "%s.canvas_list[%d]" % (where, u)
        if not is_int(unit.get("canvas_id")) or not isinstance(unit.get("connected"), (int, bool)):
            problems.append(here + ".canvas_id/connected")
        for t, tray in enumerate(unit.get("tray_list", [])):
            spot = "%s.tray_list[%d]" % (here, t)
            for key in ("tray_id", "min_nozzle_temp", "max_nozzle_temp", "status"):
                if not is_int(tray.get(key)):
                    problems.append("%s.%s" % (spot, key))
            for key in ("brand", "filament_type", "filament_name", "filament_code", "filament_color"):
                if not isinstance(tray.get(key), str):
                    problems.append("%s.%s" % (spot, key))
            color = tray.get("filament_color")
            if isinstance(color, str) and color and not is_hex_color(color):
                problems.append("%s.filament_color %r (not #RRGGBB)" % (spot, color))
    return problems


def sdk_type_problems(status):
    """Fields of a full status (a 1002 result or the merged cache) that handlePrinterStatus could
    not read: missing, or of a JSON type its JsonUtils getter does not accept."""
    problems = []
    ms = status.get("machine_status")
    if not isinstance(ms, dict):
        return ["machine_status"]
    for key in ("status", "sub_status", "progress"):
        if not is_int(ms.get(key)):
            problems.append("machine_status." + key)
    if not isinstance(ms.get("exception_status"), list) or not all(is_int(c) for c in ms["exception_status"]):
        problems.append("machine_status.exception_status (list of int)")
    ps = status.get("print_status") or {}
    if not isinstance(ps.get("filename"), str):
        problems.append("print_status.filename")
    for key in ("total_duration", "print_duration", "remaining_time_sec", "total_layer", "current_layer"):
        if not is_int(ps.get(key)):
            problems.append("print_status." + key)
    for heater in ("extruder", "heater_bed"):
        part = status.get(heater) or {}
        if not is_number(part.get("temperature")) or not is_number(part.get("target")):
            problems.append(heater + ".temperature/target")
    fans = status.get("fans") or {}
    for fan in ("fan", "heater_fan", "controller_fan", "box_fan", "aux_fan"):
        if not is_number((fans.get(fan) or {}).get("speed")):
            problems.append("fans.%s.speed" % fan)
    if not is_number((status.get("led") or {}).get("status")):
        problems.append("led.status")
    device = status.get("external_device") or {}
    for key in ("camera", "u_disk"):  # safeGet<bool>: get<bool>() throws on a number -> default false
        if not isinstance(device.get(key), bool):
            problems.append("external_device." + key)
    if not isinstance(device.get("type"), (str, int)) or isinstance(device.get("type"), bool):
        problems.append("external_device.type")
    problems += canvas_type_problems(status.get("canvas_info"))
    codes = (status.get("exception") or {}).get("exception_code")
    if not isinstance(codes, dict) or not all(isinstance(v, dict) and is_int(v.get("time")) for v in codes.values()):
        problems.append("exception.exception_code ({code: {time: int}})")
    return problems


def slicer_trays(canvas):
    """What ElegooLink::getPrinterMmsInfo + PrinterMmsManager::getPrinterMmsInfo make of a 2005
    canvas_info: tray names A1.. / B1.. by unit order and list position (NOT by tray_id), and the
    TrayStatus (0 -> empty, 1 -> preloaded, 2 -> loaded, "_unknown_filament" without type/name)."""
    trays = []
    for unit_index, unit in enumerate(canvas.get("canvas_list", [])):
        for position, tray in enumerate(unit.get("tray_list", []), 1):
            filament_type = tray.get("filament_type", "")
            filament_name = tray.get("filament_name", "")
            if filament_name in ("", " "):
                filament_name = filament_type
            known = bool(filament_type) and bool(filament_name)
            status = {0: "empty", 1: "preloaded", 2: "loaded"}.get(tray.get("status"), "error")
            if status in ("preloaded", "loaded") and not known:
                status += "_unknown_filament"
            trays.append(("ABCDEFGH"[unit_index] + str(position), tray.get("tray_id"), status))
    return trays


# ---------------------------------------------------------------------------- helpers

class Checks:
    def __init__(self):
        self.results = []

    def check(self, name, ok, detail=""):
        ok = bool(ok)
        self.results.append((name, ok, detail))
        line = ("PASS  " if ok else "FAIL  ") + name
        if detail and not ok:
            line += "  -- " + str(detail)
        print(line, flush=True)
        return ok


class Config:
    def __init__(self, host, ports, sn, access_code):
        self.host, self.ports, self.sn, self.access_code = host, ports, sn, access_code


def http_request(cfg, port_name, method, path, body=None, headers=None, timeout=5):
    """Returns (status, body bytes); (None, error text) when the connection fails."""
    try:
        conn = http.client.HTTPConnection(cfg.host, cfg.ports[port_name], timeout=timeout)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data
    except (OSError, http.client.HTTPException) as error:
        return None, str(error)


def control(cfg, body=None):
    if body is None:
        status, data = http_request(cfg, "control", "GET", "/state")
    else:
        status, data = http_request(cfg, "control", "POST", "/state", json.dumps(body).encode(),
                                    {"Content-Type": "application/json"})
    if status != 200:
        raise RuntimeError("control endpoint answered %s: %s" % (status, data[:300]))
    return json.loads(data.decode("utf-8"))


def udp_probe(host, port, timeout=2.0, broadcast=False):
    """The SDK's discovery probe (ElegooFdmCC2DiscoveryStrategy::getDiscoveryMessage)."""
    message = b'{\n        "id": 0,\n        "method": 7000\n        }\n    '
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(timeout)
    try:
        sock.sendto(message, ("127.255.255.255" if broadcast else host, port))
        data, sender = sock.recvfrom(4096)
        return json.loads(data.decode("utf-8")), sender[0]
    except (OSError, ValueError):
        return None, None
    finally:
        sock.close()


def sdk_upload(cfg, data, file_name, token=None, md5=None, chunk_size=1024 * 1024):
    """ElegooFdmCC2HttpTransfer::doUpload: PUT /upload, 1 MiB chunks on one keep-alive connection.
    Returns the list of (http status, json) per chunk; stops at the first failure."""
    md5 = md5 or hashlib.md5(data).hexdigest()
    total = len(data)
    conn = http.client.HTTPConnection(cfg.host, cfg.ports["http"], timeout=30)
    results = []
    offset = 0
    while offset < total:
        chunk = data[offset:offset + chunk_size]
        headers = {
            "User-Agent": "ElegooLink/selftest",
            "Accept": "application/json",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(chunk)),
            "Content-Range": "bytes %d-%d/%d" % (offset, offset + len(chunk) - 1, total),
            "X-File-Name": file_name.encode("utf-8"),  # cpp-httplib sends the raw UTF-8 bytes
            "X-File-MD5": md5,
            "X-Token": token if token is not None else cfg.access_code,
        }
        conn.request("PUT", "/upload", body=chunk, headers=headers)
        response = conn.getresponse()
        body = response.read()
        try:
            reply = json.loads(body.decode("utf-8")) if body else None
        except ValueError:
            reply = None
        results.append((response.status, reply))
        if response.status != 200 or not reply or reply.get("error_code") != 0:
            break
        offset += len(chunk)
    conn.close()
    return results


def make_gcode(target_bytes):
    """A G-code file with the header lines the fake reads (layers, estimated time)."""
    lines = ["; generated by tools/remote-api/fakecc2/selftest.py",
             "; total layer number: 42",
             "; estimated printing time (normal mode) = 1h 2m 3s",
             "G28", "M190 S60", "M109 S220"]
    text = "\n".join(lines) + "\n"
    i = 0
    parts = [text]
    size = len(text)
    while size < target_bytes:
        line = "G1 X%.3f Y%.3f E%.5f F6000\n" % (100 + (i % 500) * 0.1, 100 + (i % 300) * 0.1, i * 0.00321)
        parts.append(line)
        size += len(line)
        i += 1
    return "".join(parts).encode("ascii")


def read_first_jpeg(cfg, timeout=5):
    """Like the MCP's get_camera_snapshot: first complete JPEG from the MJPEG stream."""
    try:
        sock = socket.create_connection((cfg.host, cfg.ports["camera"]), timeout=timeout)
        sock.sendall(b"GET /video HTTP/1.1\r\nHost: printer\r\n\r\n")
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(buf) < 8 * 1024 * 1024:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            head_end = buf.find(b"\r\n\r\n")
            part_start = buf.find(b"--frame\r\n", head_end + 4 if head_end >= 0 else 0)
            if head_end < 0 or part_start < 0:
                continue
            part_head_end = buf.find(b"\r\n\r\n", part_start)
            if part_head_end < 0:
                continue
            part_head = buf[part_start:part_head_end].decode("latin-1")
            length = int(part_head.split("Content-Length:")[1].split("\r\n")[0].strip())
            frame = buf[part_head_end + 4:part_head_end + 4 + length]
            if len(frame) == length:
                sock.close()
                return buf[:head_end].decode("latin-1"), frame
        sock.close()
    except (OSError, ValueError, IndexError):
        pass
    return None, None


# ---------------------------------------------------------------------------- add_fake_printer.py

SLICER_SRC = os.path.normpath(os.path.join(HERE, "..", "..", "..", "src"))
# Used only when the ElegooSlicer sources are not found there (copied from
# PrinterNetworkInfo.cpp convertPrinterNetworkInfoToJson / PrinterCache.cpp savePrinterList).
FALLBACK_WRITTEN_KEYS = [
    "printerId", "printerName", "host", "port", "vendor", "printerModel", "protocolVersion", "firmwareVersion",
    "hostType", "mainboardId", "serialNumber", "username", "password", "authMode", "token", "accessCode",
    "pinCode", "webUrl", "networkType", "extraInfo", "isPhysicalPrinter", "addTime", "modifyTime",
    "lastActiveTime", "connectStatus", "printerStatus", "exceptions", "deviceAssistantStatus", "printTask",
    "printCapabilities", "systemCapabilities", "isAdded"]
FALLBACK_RUNTIME_KEYS = ["connectStatus", "printerStatus", "exceptions", "deviceAssistantStatus", "printTask"]


def function_body(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}\n", start)]


def printer_list_rules():
    """What the slicer writes and reads for one printer_list.json entry, taken from its sources:
    (saved keys, {key: python type the loader needs}, capability sub-keys, where the rules came from)."""
    info_cpp = os.path.join(SLICER_SRC, "libslic3r", "PrinterNetworkInfo.cpp")
    cache_cpp = os.path.join(SLICER_SRC, "slic3r", "Utils", "Elegoo", "PrinterCache.cpp")
    if not (os.path.isfile(info_cpp) and os.path.isfile(cache_cpp)):
        return (set(FALLBACK_WRITTEN_KEYS) - set(FALLBACK_RUNTIME_KEYS), {}, None, "built-in lists")
    with open(info_cpp, encoding="utf-8", errors="replace") as f:
        info = f.read()
    with open(cache_cpp, encoding="utf-8", errors="replace") as f:
        cache = f.read()
    writer = function_body(info, "nlohmann::json convertPrinterNetworkInfoToJson(")
    loader = function_body(info, "PrinterNetworkInfo convertJsonToPrinterNetworkInfo(")
    written = set(re.findall(r'^\s*json\["(\w+)"\]', writer, re.M))
    runtime = set(re.findall(r'printerJson\.erase\("(\w+)"\)', cache))
    types = {}
    for getter, kind in (("safeGetString", str), ("safeGetInt", int), ("safeGetBool", bool), ("safeGet<uint64_t>", int)):
        for key in re.findall(re.escape(getter) + r'\(json, "(\w+)"', loader):
            types[key] = kind
    caps = {"printCapabilities": set(re.findall(r'printCapabilitiesJson\["(\w+)"\]', writer)),
            "systemCapabilities": set(re.findall(r'systemCapabilitiesJson\["(\w+)"\]', writer))}
    return written - runtime, types, caps, "slicer sources"


def slicer_printer_type(model, vendor):
    """ElegooLink.cpp getPrinterType(): which SDK printer class the slicer connects with."""
    model, vendor = model.lower(), vendor.lower()
    if vendor not in model:
        model = vendor + " " + model
    if model in ("elegoo centauri carbon 2", "elegoo centauri 2"):
        return "ELEGOO_FDM_CC2"
    if model in ("elegoo centauri carbon", "elegoo centauri"):
        return "ELEGOO_FDM_CC"
    return "ELEGOO_FDM_KLIPPER" if vendor == "elegoo" else "GENERIC_FDM_KLIPPER"


def check_add_fake_printer(c, cfg):
    """Run add_fake_printer.py on a throw-away data dir and check its printer_list.json against what
    the slicer reads back (PrinterCache::loadPrinterList -> convertJsonToPrinterNetworkInfo) and what
    PrinterManager::connectToPrinter compares with the printer's 1001 answer."""
    script = os.path.join(HERE, "add_fake_printer.py")
    # Not under %TEMP%: the script refuses any path containing "appdata", and %TEMP% is in AppData.
    datadir = tempfile.mkdtemp(prefix="_selftest_datadir_", dir=HERE)
    list_path = os.path.join(datadir, "user", "printer_list.json")

    def run(*extra, target=datadir):
        command = [sys.executable, "-I", script, target, "--sn", cfg.sn, "--access-code", cfg.access_code,
                   "--force"] + list(extra)  # --force: a running app must not make the test fail
        return subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=60)

    def entries():
        with open(list_path, encoding="utf-8") as f:
            data = json.load(f)
        return data, {pid: p for pid, p in data.items() if p.get("serialNumber") == cfg.sn
                      or p.get("printerName") == "Fake CC2" or p.get("host", "").startswith("127.0.0.1")}

    try:
        saved_keys, loader_types, caps, origin = printer_list_rules()
        os.makedirs(os.path.dirname(list_path))
        other = {"other-printer-id": {"printerId": "other-printer-id", "printerName": "Real printer",
                                      "host": "192.168.0.50", "hostType": "ElegooLink"}}
        with open(list_path, "w", encoding="utf-8") as f:
            json.dump(other, f)

        result = run()
        data, fakes = entries()
        entry = next(iter(fakes.values()), {})
        c.check("add_fake_printer: writes one fake entry next to the existing printer (exit 0)",
                result.returncode == 0 and len(fakes) == 1 and "other-printer-id" in data,
                result.stdout[-300:] + result.stderr[-300:])
        c.check("add_fake_printer: keeps a backup of the previous printer_list.json",
                any(n.startswith("printer_list.json.bak-") for n in os.listdir(os.path.dirname(list_path))))
        c.check("add_fake_printer: entry keys == what PrinterCache::savePrinterList writes (%s)" % origin,
                set(entry) == saved_keys, sorted(set(entry) ^ saved_keys))
        wrong = sorted(k for k, kind in loader_types.items()
                       if k in entry and not (isinstance(entry[k], kind) and (kind is bool or not isinstance(entry[k], bool))))
        if caps is not None:
            for group, keys in caps.items():
                if set(entry.get(group, {})) != keys or not all(isinstance(v, bool) for v in entry[group].values()):
                    wrong.append(group)
        c.check("add_fake_printer: every value has the JSON type convertJsonToPrinterNetworkInfo reads (%s)" % origin,
                not wrong, wrong)
        printer_id = next(iter(fakes), "")
        c.check("add_fake_printer: map key == printerId, host 127.0.0.1, hostType ElegooLink (ElegooPrinterNetwork)",
                entry.get("printerId") == printer_id and entry.get("host") == "127.0.0.1"
                and entry.get("hostType") == "ElegooLink", entry)
        c.check("add_fake_printer: vendor + printerModel -> getPrinterType() == ELEGOO_FDM_CC2",
                slicer_printer_type(entry.get("printerModel", ""), entry.get("vendor", "")) == "ELEGOO_FDM_CC2",
                (entry.get("vendor"), entry.get("printerModel")))
        c.check("add_fake_printer: LAN (networkType 0), authMode 2 = access code, accessCode = the fake's code",
                entry.get("networkType") == 0 and entry.get("authMode") == 2 and entry.get("accessCode") == cfg.access_code)
        c.check("add_fake_printer: serialNumber == mainboardId == the fake's sn "
                "(connectToPrinter compares both with the 1001 'sn', else ID_NOT_MATCH)",
                entry.get("serialNumber") == cfg.sn and entry.get("mainboardId") == cfg.sn, entry)

        run()
        data, fakes = entries()
        c.check("add_fake_printer: running it again replaces the entry (same printerId, still one)",
                list(fakes) == [printer_id] and "other-printer-id" in data, sorted(fakes))
        run("--http-port", "8081")
        data, fakes = entries()
        c.check("add_fake_printer --http-port 8081: host '127.0.0.1:8081', old entry replaced",
                len(fakes) == 1 and next(iter(fakes.values())).get("host") == "127.0.0.1:8081")
        run("--no-serial")
        data, fakes = entries()
        entry = next(iter(fakes.values()), {})
        c.check("add_fake_printer --no-serial: empty serialNumber/mainboardId (the SDK then asks GET /system/info)",
                len(fakes) == 1 and entry.get("serialNumber") == "" and entry.get("mainboardId") == "")
        result = run("--remove")
        data, fakes = entries()
        c.check("add_fake_printer --remove: fake gone, other printer kept",
                result.returncode == 0 and not fakes and "other-printer-id" in data, sorted(data))

        with open(list_path, "w", encoding="utf-8") as f:
            f.write("{broken")
        result = run()
        with open(list_path, encoding="utf-8") as f:
            untouched = f.read() == "{broken"
        c.check("add_fake_printer: refuses a printer_list.json that is not valid JSON and leaves it alone",
                result.returncode != 0 and untouched, result.stderr[-200:])
        appdata = os.environ.get("APPDATA") or "C:\\Users\\x\\AppData\\Roaming"
        target = os.path.join(appdata, "ElegooSlicer-fakecc2-selftest-never-created")
        result = run(target=target)
        c.check("add_fake_printer: refuses a data dir under AppData (the real profile)",
                result.returncode != 0 and "AppData" in result.stdout + result.stderr and not os.path.exists(target))
    finally:
        shutil.rmtree(datadir, ignore_errors=True)


# ---------------------------------------------------------------------------- the test

def run_tests(cfg, workdir):
    c = Checks()
    sn, code = cfg.sn, cfg.access_code

    check_add_fake_printer(c, cfg)

    state = control(cfg)
    c.check("control: GET /state answers", state["phase"] in ("idle", "completed"), state.get("phase"))
    control(cfg, {"preheat_seconds": 1.0, "leveling_seconds": 0.6, "print_seconds": 8.0,
                  "completed_hold_seconds": 1.5, "clear_log": True})

    # --- discovery (ElegooFdmCC2DiscoveryStrategy::parseResponse) ---
    reply, sender = udp_probe(cfg.host, cfg.ports["discovery"])
    result = (reply or {}).get("result", {})
    c.check("discovery: probe {id:0, method:7000} answered with id + result",
            reply is not None and "id" in reply and isinstance(reply.get("result"), dict), reply)
    c.check("discovery: sn, machine_model, host_name", result.get("sn") == sn and result.get("machine_model")
            and result.get("host_name"), result)
    c.check("discovery: token_status 1 + lan_status 1 (accessCode auth, LAN mode)",
            result.get("token_status") == 1 and result.get("lan_status") == 1, result)
    c.check("discovery: reply comes from %s (the SDK uses the sender IP as host)" % cfg.host, sender == cfg.host, sender)
    reply_b, _ = udp_probe(cfg.host, cfg.ports["discovery"], broadcast=True)
    if sys.platform == "win32":
        c.check("discovery: broadcast to 127.255.255.255 reaches it (loopback adapter)", reply_b is not None)
    else:
        print("INFO  discovery via 127.255.255.255: %s (only checked on Windows)" % ("ok" if reply_b else "no answer"))

    # --- HTTP serial bootstrap (ElegooCC2MqttProtocol::validateConnectionParams) ---
    status, body = http_request(cfg, "http", "GET", "/system/info?X-Token=" + code, headers={"X-Token": code})
    info = json.loads(body) if status == 200 else {}
    c.check("http: GET /system/info with the access code -> error_code 0 + system_info.sn",
            status == 200 and info.get("error_code") == 0 and info.get("system_info", {}).get("sn") == sn,
            (status, body[:200]))
    status, _ = http_request(cfg, "http", "GET", "/system/info?X-Token=nope", headers={"X-Token": "nope"})
    c.check("http: /system/info with a wrong code -> 401", status == 401, status)

    # --- MQTT authentication (configureConnectionOptions; Paho falls back to MQTT 3.1) ---
    rc = MiniMqttClient(cfg.host, cfg.ports["mqtt"], "1_PC_0001", "elegoo", "wrong").connect()
    c.check("mqtt: wrong access code -> CONNACK 5 (not authorized)", rc == 5, rc)
    rc = MiniMqttClient(cfg.host, cfg.ports["mqtt"], "1_PC_0002", "elegoo", "wrong", ("MQIsdp", 3)).connect()
    c.check("mqtt: wrong access code over MQTT 3.1 (Paho retry) -> CONNACK 5", rc == 5, rc)
    rc = MiniMqttClient(cfg.host, cfg.ports["mqtt"], "1_PC_0003", "elegoo", code, ("MQTT", 5)).connect()
    c.check("mqtt: unsupported protocol level -> CONNACK 1", rc == 1, rc)
    legacy = MiniMqttClient(cfg.host, cfg.ports["mqtt"], "1_PC_0004", "elegoo", code, ("MQIsdp", 3))
    c.check("mqtt: MQTT 3.1 with the right code -> accepted", legacy.connect() == 0)
    legacy.disconnect()

    # --- SDK connect sequence (MqttProtocol::Impl::doConnect) ---
    sdk = SdkLikeClient(cfg)
    c.check("mqtt: CONNECT user 'elegoo' + access code -> CONNACK 0", sdk.connect() == 0)
    granted = sdk.subscribe()
    c.check("mqtt: SUBSCRIBE api_response, api_status, register_response at QoS 1 -> granted [1,1,1]",
            granted == [1, 1, 1], granted)
    stranger = SdkLikeClient(cfg)  # connected but never registered
    stranger.connect()
    stranger.subscribe()
    c.check("printer: requests from an unregistered client are not answered",
            stranger.request(1002, timeout=1.0) is None)
    stranger.mqtt.disconnect()
    c.check("printer: api_register -> register_response {client_id, error: ok}", sdk.register() == "ok")

    response = sdk.request(1002)
    result = (response or {}).get("result", {})
    c.check("1002 GET_PRINTER_STATUS: id and method echoed, error_code 0",
            response is not None and response.get("method") == 1002 and result.get("error_code") == 0, response)
    c.check("1002: idle (machine_status.status 1, sub_status 0) with print_status/extruder/heater_bed/fans",
            machine(result) == (1, 0) and all(k in result for k in ("print_status", "extruder", "heater_bed", "fans")),
            result.get("machine_status"))
    problems = sdk_type_problems(result)
    c.check("1002 idle: every field handlePrinterStatus reads has the JSON type its JsonUtils getter accepts",
            not problems, problems)

    response = sdk.request(1001)
    result = (response or {}).get("result", {})
    c.check("1001 GET_PRINTER_ATTRIBUTES: sn, machine_model, hostname, software_version.ota_version",
            result.get("error_code") == 0 and result.get("sn") == sn and result.get("machine_model")
            and result.get("hostname") and result.get("software_version", {}).get("ota_version"), result)
    c.check("1001: machine_model + vendor 'Elegoo' -> getPrinterType() == ELEGOO_FDM_CC2 (also how a discovered "
            "printer is matched to the 'Elegoo Centauri Carbon 2' profile)",
            slicer_printer_type(result.get("machine_model", ""), "Elegoo") == "ELEGOO_FDM_CC2", result.get("machine_model"))
    hostname = result.get("hostname")
    c.check("heartbeat: {type: PING} -> {type: PONG} on api_response", sdk.ping())
    c.check("mqtt: PINGREQ -> PINGRESP", sdk.mqtt.ping())

    # --- 1043 rename (PrinterManager::updatePrinterName -> SDK updatePrinterName) ---
    response = sdk.request(1043, {"hostname": "Renamed CC2"})
    c.check("1043 UPDATE_PRINTER_NAME {hostname} -> error_code 0",
            (response or {}).get("result", {}).get("error_code") == 0, response)
    renamed = (sdk.request(1001) or {}).get("result", {}).get("hostname")
    found, _ = udp_probe(cfg.host, cfg.ports["discovery"])
    c.check("after 1043: 1001 hostname and discovery host_name report the new name",
            renamed == "Renamed CC2" and (found or {}).get("result", {}).get("host_name") == "Renamed CC2", (renamed, found))
    response = sdk.request(1043, {"hostname": "  "})
    c.check("1043 with a blank name -> error_code 1003", (response or {}).get("result", {}).get("error_code") == 1003)
    sdk.request(1043, {"hostname": hostname})

    response = sdk.request(1042)
    url = (response or {}).get("result", {}).get("url", "")
    c.check("1042 VIDEO_STREAM (commented out in the SDK, firmware 02.01 answers) -> url of the :%d MJPEG camera"
            % cfg.ports["camera"], url.endswith(":%d/?action=stream" % cfg.ports["camera"]), response)

    response = sdk.request(2005)
    canvas = (response or {}).get("result", {}).get("canvas_info", {})
    trays = canvas.get("canvas_list", [{}])[0].get("tray_list", [])
    c.check("2005 GET_CANVAS_STATUS: one connected CANVAS with 4 trays (3 PLA, 1 empty)",
            len(trays) == 4 and canvas["canvas_list"][0].get("connected") == 1
            and [t["status"] for t in trays] == [1, 1, 1, 0]
            and all(t["filament_type"] == "PLA" for t in trays[:3]), canvas)
    c.check("2005: auto_refill false, active_tray_id -1", canvas.get("auto_refill") is False
            and canvas.get("active_tray_id") == -1, canvas)
    problems = canvas_type_problems(canvas)
    c.check("2005: tray fields have the types handleCanvasStatus reads, colours are #RRGGBB", not problems, problems)
    c.check("2005 as the slicer shows it: A1..A3 preloaded (tray_id 0..2), A4 empty",
            slicer_trays(canvas) == [("A1", 0, "preloaded"), ("A2", 1, "preloaded"), ("A3", 2, "preloaded"),
                                     ("A4", 3, "empty")], slicer_trays(canvas))
    response = sdk.request(2004, {"auto_refill": True})
    c.check("2004 SET_AUTO_REFILL true -> error_code 0", (response or {}).get("result", {}).get("error_code") == 0)
    response = sdk.request(2005)
    c.check("2005 after 2004: auto_refill true",
            (response or {}).get("result", {}).get("canvas_info", {}).get("auto_refill") is True)
    response = sdk.request(2004, {"auto_refill": "yes"})
    c.check("2004 with a non-bool -> error_code 1003", (response or {}).get("result", {}).get("error_code") == 1003)
    sdk.request(2004, {"auto_refill": False})

    response = sdk.request(9999)
    c.check("unknown method -> error_code 1001 (unknown interface)",
            (response or {}).get("result", {}).get("error_code") == 1001, response)

    # --- upload (ElegooFdmCC2HttpTransfer) ---
    gcode = make_gcode(2_600_000)
    results = sdk_upload(cfg, gcode, CUBE_NAME)
    c.check("upload: 2.6 MB in 3 chunks of <= 1 MiB, each HTTP 200 + error_code 0",
            len(results) == 3 and all(s == 200 and r and r.get("error_code") == 0 for s, r in results), results)
    unicode_name = UNICODE_NAME
    small = make_gcode(5000)
    results = sdk_upload(cfg, small, unicode_name)
    c.check("upload: UTF-8 file name (raw bytes in X-File-Name)", results and results[-1][1].get("error_code") == 0,
            results)
    results = sdk_upload(cfg, small, "bad_token.gcode", token="wrong")
    c.check("upload: wrong X-Token -> HTTP 401 (SDK: INVALID_ACCESS_CODE)", results and results[0][0] == 401, results)
    results = sdk_upload(cfg, small, "bad_md5.gcode", md5="0" * 32)
    c.check("upload: wrong X-File-MD5 -> error_code 9004", results and results[-1][1].get("error_code") == 9004,
            results)
    status, body = http_request(cfg, "http", "PUT", "/upload", body=b"x" * 10, headers={
        "Content-Range": "bytes 500-509/1000", "X-File-Name": "gap.gcode", "X-File-MD5": "0" * 32,
        "X-Token": code, "Content-Type": "application/octet-stream"})
    c.check("upload: chunk at an unexpected offset -> error_code 9000",
            status == 200 and json.loads(body).get("error_code") == 9000, (status, body))
    state = control(cfg)
    received = {f["name"]: f for f in state["received_files"]}
    c.check("control: received_files lists the uploads with size + sha256",
            received.get(CUBE_NAME, {}).get("sha256") == hashlib.sha256(gcode).hexdigest()
            and received.get(CUBE_NAME, {}).get("size") == len(gcode)
            and unicode_name in received, sorted(received))
    c.check("control: failed uploads are not listed", "bad_md5.gcode" not in received
            and "bad_token.gcode" not in received and "gap.gcode" not in received, sorted(received))
    status, body = http_request(cfg, "http", "GET", "/download?X-Token=%s?file_name=%s" % (code, CUBE_NAME))
    c.check("download: GET /download?X-Token=..?file_name=.. (the SDK's URL) returns the same bytes",
            status == 200 and body == gcode, status)
    status, _ = http_request(cfg, "http", "GET", "/download?X-Token=wrong?file_name=%s" % CUBE_NAME)
    c.check("download: wrong token -> 401", status == 401, status)
    status, _ = http_request(cfg, "http", "GET", "/download/udisk?X-Token=%s?file_name=%s" % (code, CUBE_NAME))
    c.check("download: /download/udisk (not emulated) -> 404 (SDK: PRINTER_UNKNOWN_ERROR)", status == 404, status)

    # --- a second client, to check that status pushes reach every client ---
    watcher = SdkLikeClient(cfg)
    c.check("second client connects and registers", watcher.connect_and_register() == "ok")
    watcher.request(1002)
    sdk.request(1002)

    # --- start print (ElegooFdmCC2MessageAdapter::convertRequest START_PRINT) ---
    start_params = {"storage_media": "local", "filename": CUBE_NAME,
                    "config": {"delay_video": False, "printer_check": True, "print_layout": "A",
                               "bedlevel_force": False,
                               "slot_map": [{"t": 0, "canvas_id": 0, "tray_id": 0}]}}
    response = sdk.request(1020, start_params, timeout=10)
    c.check("1020 START_PRINT (SDK params) -> error_code 0",
            (response or {}).get("result", {}).get("error_code") == 0, response)
    c.check("status push: preheat (status 2, sub_status 1405 bed / 1045 nozzle)",
            sdk.wait_status(lambda s: machine(s) in ((2, 1405), (2, 1045)), 3), sdk.status and sdk.status["machine_status"])
    c.check("status push: auto leveling (sub_status 2901) because printer_check is true",
            sdk.wait_status(lambda s: machine(s) == (2, 2901), 4), sdk.status and sdk.status["machine_status"])
    c.check("status push: printing (sub_status 2075) with progress > 0",
            sdk.wait_status(lambda s: machine(s) == (2, 2075) and s["machine_status"]["progress"] > 0, 5),
            sdk.status and sdk.status["machine_status"])
    ps = sdk.status["print_status"]
    c.check("print_status: filename, total_layer 42 and durations from the G-code header (1h 2m 3s)",
            ps["filename"] == CUBE_NAME and ps["total_layer"] == 42
            and ps["print_duration"] + ps["remaining_time_sec"] == 3723, ps)
    c.check("print: heaters at target (extruder 220, bed 60)",
            sdk.status["extruder"]["target"] == 220 and sdk.status["heater_bed"]["target"] == 60)
    canvas = sdk.status["canvas_info"]
    c.check("CANVAS: slot_map tray 0 becomes the active, loaded tray (status 2)",
            canvas["active_tray_id"] == 0 and canvas["canvas_list"][0]["tray_list"][0]["status"] == 2, canvas)
    c.check("CANVAS as the slicer shows it while printing: A1 loaded",
            slicer_trays(canvas)[0] == ("A1", 0, "loaded"), slicer_trays(canvas))
    problems = sdk_type_problems(sdk.status)
    c.check("printing: the merged cache still has the types the SDK reads (deltas keep int fields int)",
            not problems, problems)
    response = sdk.request(1020, start_params)
    c.check("1020 while printing -> error_code 1009 (busy)",
            (response or {}).get("result", {}).get("error_code") == 1009, response)

    response = sdk.request(1021)
    c.check("1021 PAUSE_PRINT -> error_code 0", (response or {}).get("result", {}).get("error_code") == 0, response)
    c.check("status push: pausing (2501) then paused (2502)",
            sdk.wait_status(lambda s: machine(s) == (2, 2502), 4), sdk.status["machine_status"])
    problems = sdk_type_problems(sdk.status)
    c.check("paused: the merged cache has the types the SDK reads", not problems, problems)
    frozen = sdk.status["machine_status"]["progress"]
    time.sleep(1.0)
    sdk.process_pushes()
    c.check("paused: progress does not move", sdk.status["machine_status"]["progress"] == frozen,
            (frozen, sdk.status["machine_status"]["progress"]))
    response = sdk.request(1021)
    c.check("1021 while paused -> error_code 1010", (response or {}).get("result", {}).get("error_code") == 1010)
    response = sdk.request(1023)
    c.check("1023 RESUME_PRINT (commented out in the SDK, answered by the firmware) -> error_code 0",
            (response or {}).get("result", {}).get("error_code") == 0, response)
    c.check("status push: resuming (2401) then printing (2075)",
            sdk.wait_status(lambda s: machine(s) == (2, 2075), 4), sdk.status["machine_status"])

    control(cfg, {"drop_next": 1022})
    c.check("control drop_next: next 1022 left unanswered (SDK sees a timeout)", sdk.request(1022, timeout=1.5) is None)
    response = sdk.request(1022)
    c.check("1022 STOP_PRINT -> error_code 0", (response or {}).get("result", {}).get("error_code") == 0, response)
    c.check("status push: stopping (2503) / stopped (2504)",
            sdk.wait_status(lambda s: machine(s) in ((2, 2503), (2, 2504)), 3), sdk.status["machine_status"])
    c.check("status push: back to idle (status 1) after the stop",
            sdk.wait_status(lambda s: machine(s) == (1, 0), 5), sdk.status["machine_status"])
    c.check("CANVAS: tray unloaded after the print (active_tray_id -1, status 1)",
            sdk.status["canvas_info"]["active_tray_id"] == -1
            and sdk.status["canvas_info"]["canvas_list"][0]["tray_list"][0]["status"] == 1)

    for method, label in ((1021, "PAUSE"), (1022, "STOP"), (1023, "RESUME")):
        response = sdk.request(method)
        c.check("%d %s when idle -> error_code 1010" % (method, label),
                (response or {}).get("result", {}).get("error_code") == 1010, response)
    response = sdk.request(1020, dict(start_params, filename="missing.gcode"))
    c.check("1020 with a file the printer does not have -> error_code 1021",
            (response or {}).get("result", {}).get("error_code") == 1021, response)
    response = sdk.request(1020, dict(start_params, storage_media="u-disk"))
    c.check("1020 from u-disk (not emulated) -> error_code 1021",
            (response or {}).get("result", {}).get("error_code") == 1021, response)

    # --- missing bed leveling data: ElegooLink::sendPrintTask retries with bedlevel_force ---
    control(cfg, {"missing_bed_leveling": True})
    response = sdk.request(1020, start_params)
    c.check("1020 without bed mesh (printer_check, no bedlevel_force) -> error_code 1026",
            (response or {}).get("result", {}).get("error_code") == 1026, response)
    forced = copy.deepcopy(start_params)
    forced["config"]["bedlevel_force"] = True
    response = sdk.request(1020, forced)
    c.check("1020 retry with bedlevel_force -> error_code 0",
            (response or {}).get("result", {}).get("error_code") == 0, response)

    # --- a late answer: the real CC2 answers pause/stop only after parking (pycentauri) ---
    control(cfg, {"delay_next": {"method": 1021, "seconds": 1.5}})
    c.check("control delay_next: 1021 not answered within 1 s (the SDK would report OPERATION_TIMEOUT) ...",
            sdk.request(1021, timeout=1.0) is None)
    late_id = sdk.last_request_id
    c.check("... but carried out at once: status pushes reach paused (2502)",
            sdk.wait_status(lambda s: machine(s) == (2, 2502), 4), sdk.status["machine_status"])
    late = sdk.mqtt.wait_message(lambda t, o: t == sdk.topic_response and isinstance(o, dict)
                                 and o.get("id") == late_id, 3)
    c.check("... and the late answer arrives afterwards with the request's id, method 1021, error_code 0",
            late is not None and late[1].get("method") == 1021 and late[1].get("result", {}).get("error_code") == 0,
            late)
    sdk.request(1022)
    c.check("stopped again -> idle", sdk.wait_status(lambda s: machine(s) == (1, 0), 6), sdk.status["machine_status"])

    # --- a print that runs to the end, with the UTF-8 file name ---
    control(cfg, {"print_seconds": 2.0})
    params = {"storage_media": "local", "filename": unicode_name,
              "config": {"delay_video": False, "printer_check": False, "print_layout": "B",
                         "bedlevel_force": False, "slot_map": []}}
    sdk.seen_codes.clear()
    response = sdk.request(1020, params)
    c.check("1020 UTF-8 file name, printer_check false -> error_code 0",
            (response or {}).get("result", {}).get("error_code") == 0, response)
    c.check("no auto-leveling phase when printer_check is false",
            sdk.wait_status(lambda s: machine(s) == (2, 2075), 3) and (2, 2901) not in sdk.seen_codes,
            sorted(sdk.seen_codes))
    c.check("status push: completed (sub_status 2077, progress 100)",
            sdk.wait_status(lambda s: machine(s) == (2, 2077) and s["machine_status"]["progress"] == 100, 6),
            sdk.status["machine_status"])
    problems = sdk_type_problems(sdk.status)
    c.check("completed: the merged cache has the types the SDK reads", not problems, problems)
    c.check("'print completed' turns into idle after completed_hold_seconds",
            sdk.wait_status(lambda s: machine(s) == (1, 0), 5), sdk.status["machine_status"])
    last_task = control(cfg)["last_task"] or {}
    c.check("control: last_task records the completed print",
            last_task.get("result") == "completed" and last_task.get("filename") == unicode_name, last_task)

    # "print completed" counts as not busy (ElegooLink::isBusy), so a new job is accepted there.
    control(cfg, {"completed_hold_seconds": 0})
    sdk.request(1020, params)
    sdk.wait_status(lambda s: machine(s) == (2, 2077), 6)
    response = sdk.request(1020, params)
    c.check("1020 while 'print completed' is shown -> error_code 0",
            (response or {}).get("result", {}).get("error_code") == 0, response)
    last_task = control(cfg)["last_task"] or {}
    c.check("control: the completed job is closed as 'completed' when the next one starts",
            last_task.get("result") == "completed", last_task)
    sdk.request(1022)
    c.check("stopped -> idle", sdk.wait_status(lambda s: machine(s) == (1, 0), 6), sdk.status["machine_status"])
    control(cfg, {"completed_hold_seconds": 1.5})

    # --- pushes: continuity and fan-out ---
    sdk.process_pushes()
    watcher.process_pushes()
    c.check("6000 pushes: ids continuous (id == previous + 1), no malformed pushes",
            sdk.push_count > 10 and sdk.push_gaps == 0 and sdk.bad_pushes == 0,
            (sdk.push_count, sdk.push_gaps, sdk.bad_pushes))
    c.check("6000 pushes reach every subscribed client", watcher.push_count >= sdk.push_count - 2
            and watcher.push_gaps == 0, (watcher.push_count, sdk.push_count))
    c.check("merged status cache equals a fresh 1002 (deltas are complete)",
            strip_volatile(sdk.status) == strip_volatile(sdk.request(1002)["result"]))

    # --- fault injection ---
    control(cfg, {"fail_next": {"method": 2005, "error_code": 1009}})
    response = sdk.request(2005)
    c.check("control fail_next: next 2005 answers error_code 1009",
            (response or {}).get("result", {}).get("error_code") == 1009, response)
    response = sdk.request(2005)
    c.check("control fail_next is one-shot", (response or {}).get("result", {}).get("error_code") == 0)
    control(cfg, {"trays": [{"tray_id": 1, "filament_color": "#00FF00", "filament_name": "PLA Silk"}],
                  "exceptions": [803]})
    response = sdk.request(2005)
    tray = response["result"]["canvas_info"]["canvas_list"][0]["tray_list"][1]
    c.check("control trays patch shows in 2005", tray["filament_color"] == "#00FF00"
            and tray["filament_name"] == "PLA Silk", tray)
    c.check("control exceptions -> status push exception_status [803] + exception_code",
            sdk.wait_status(lambda s: s["machine_status"]["exception_status"] == [803]
                            and "803" in s["exception"]["exception_code"], 3), sdk.status.get("exception"))
    control(cfg, {"exceptions": [], "canvas_connected": False})
    c.check("control canvas_connected false -> pushed (external_device.type '0', canvas connected 0)",
            sdk.wait_status(lambda s: s["external_device"]["type"] == "0"
                            and s["canvas_info"]["canvas_list"][0]["connected"] == 0, 3))
    c.check("exceptions cleared -> the push empties exception_code (the SDK overwrites it, never merges it)",
            sdk.wait_status(lambda s: s["exception"]["exception_code"] == {}
                            and s["machine_status"]["exception_status"] == [], 3), sdk.status.get("exception"))
    control(cfg, {"canvas_connected": True, "state": "busy"})
    response = sdk.request(1020, start_params)
    c.check("control state busy -> status 10 (homing) and 1020 -> 1009",
            sdk.wait_status(lambda s: machine(s) == (10, 2801), 3)
            and (response or {}).get("result", {}).get("error_code") == 1009, response)
    control(cfg, {"state": "idle"})

    # --- client slots ---
    control(cfg, {"max_clients": 2})
    third = SdkLikeClient(cfg)
    c.check("printer: a client beyond max_clients gets 'too many clients'",
            third.connect_and_register() == "too many clients")
    third.mqtt.disconnect()
    c.check("printer: re-registering an already registered client id with every slot taken -> ok",
            sdk.register() == "ok")
    control(cfg, {"max_clients": 4})

    # --- camera ---
    headers, frame = read_first_jpeg(cfg)
    c.check("camera: MJPEG multipart/x-mixed-replace on any path, frame is a JPEG (FFD8..FFD9)",
            headers is not None and "multipart/x-mixed-replace" in headers and frame[:2] == b"\xff\xd8"
            and frame[-2:] == b"\xff\xd9", headers)

    # --- control log ---
    state = control(cfg)
    methods = {e.get("method") for e in state["requests"]}
    c.check("control: request log has every method the SDK sends (1001 1002 1020 1021 1022 1023 2004 2005)",
            {1001, 1002, 1020, 1021, 1022, 1023, 2004, 2005} <= methods, sorted(m for m in methods if isinstance(m, int)))
    c.check("control: registered clients listed",
            {sdk.client_id, watcher.client_id} <= {r["client_id"] for r in state["registered_clients"]},
            state["registered_clients"])

    # --- client id takeover: the SDK reconnects with the same client id, sometimes before the old
    #     TCP connection is gone (heartbeat timeout -> startAutoReconnect without a disconnect) ---
    twin = SdkLikeClient(cfg, client_id=watcher.client_id)
    c.check("mqtt: a new connection with an existing client id takes over (old connection dropped)",
            twin.connect() == 0 and watcher.mqtt.wait_closed(3))
    c.check("takeover: the new connection subscribes, registers and is answered",
            twin.subscribe() == [1, 1, 1] and twin.register() == "ok"
            and (twin.request(1002) or {}).get("result", {}).get("error_code") == 0)
    c.check("takeover: still one registration for that client id",
            [r["client_id"] for r in control(cfg)["registered_clients"]].count(watcher.client_id) == 1)
    twin.mqtt.disconnect()

    # --- robustness: malformed input must not take the fake down ---
    for label, data in (("garbage bytes", b"\x00\xff garbage \r\n" * 20),
                        ("truncated CONNECT", mqtt_packet(0x10, mqtt_string("MQTT") + b"\x04")),
                        ("remaining length > 4 bytes", b"\x10\xff\xff\xff\xff\xff")):
        try:
            raw = socket.create_connection((cfg.host, cfg.ports["mqtt"]), timeout=3)
            raw.sendall(data)
            closed = raw.recv(16) == b""
        except OSError:
            closed = True
        raw.close()
        c.check("mqtt: %s -> connection closed" % label, closed)
    c.check("mqtt: still serving after malformed connections", sdk.request(1002) is not None)
    sdk.mqtt.publish(sdk.topic_request, b"not json {", 1)
    sdk.mqtt.publish(sdk.topic_request, b'["a", "list"]', 1)
    c.check("printer: non-JSON / non-object requests are ignored, next request answered",
            (sdk.request(1001) or {}).get("result", {}).get("error_code") == 0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(b"\xff\xfe nonsense", (cfg.host, cfg.ports["discovery"]))
    sock.close()
    c.check("discovery: still answers after a garbage datagram", udp_probe(cfg.host, cfg.ports["discovery"])[0] is not None)
    status, body = http_request(cfg, "http", "PUT", "/upload", body=b"abc", headers={
        "Content-Range": "bytes=0-2/3", "X-File-Name": "x.gcode", "X-File-MD5": "0" * 32, "X-Token": code})
    c.check("upload: malformed Content-Range -> error_code 1003",
            status == 200 and json.loads(body).get("error_code") == 1003, (status, body))
    status, body = http_request(cfg, "http", "PUT", "/upload", body=b"abc", headers={
        "Content-Range": "bytes 0-2/3", "X-File-Name": "../../evil.gcode", "X-File-MD5": hashlib.md5(b"abc").hexdigest(),
        "X-Token": code})
    c.check("upload: a path in X-File-Name is reduced to the bare name inside received/",
            status == 200 and "evil.gcode" in {f["name"] for f in control(cfg)["received_files"]}
            and not os.path.exists(os.path.join(workdir, "evil.gcode")), (status, body))
    status, body = http_request(cfg, "control", "POST", "/state", b'{"nonsense": 1}')
    c.check("control: unknown key -> 400 unknown_keys", status == 400 and b"unknown_keys" in body, status)
    status, body = http_request(cfg, "control", "POST", "/state", b"{broken")
    c.check("control: bad JSON -> 400 bad_json", status == 400 and b"bad_json" in body, status)
    status, body = http_request(cfg, "control", "POST", "/state", b'{"canvas_info": {"canvas_list": [1, 2]}}')
    c.check("control: malformed canvas_info refused (400), CANVAS unchanged",
            status == 400 and (sdk.request(2005) or {}).get("result", {}).get("canvas_info", {}).get("canvas_list"),
            status)
    sleepy = MiniMqttClient(cfg.host, cfg.ports["mqtt"], "1_PC_sleepy", "elegoo", code, keepalive=1)
    c.check("mqtt: a client silent for 1.5x its keep-alive is dropped",
            sleepy.connect() == 0 and sleepy.wait_closed(4))

    # --- offline / reconnect ---
    control(cfg, {"offline": True})
    c.check("offline: existing MQTT connections are dropped", sdk.mqtt.wait_closed(3) and watcher.mqtt.wait_closed(3))
    probe = SdkLikeClient(cfg)
    c.check("offline: new MQTT connections get no CONNACK", probe.connect() == -1)
    status, _ = http_request(cfg, "http", "GET", "/system/info?X-Token=" + code)
    c.check("offline: HTTP does not answer", status is None, status)
    c.check("offline: discovery does not answer", udp_probe(cfg.host, cfg.ports["discovery"], 1.0)[0] is None)
    control(cfg, {"offline": False})
    again = SdkLikeClient(cfg, client_id=sdk.client_id)  # the SDK reconnects with the same client id
    c.check("back online: reconnect + register with the same client id", again.connect_and_register() == "ok")
    response = again.request(1002)
    c.check("back online: 1002 answered", (response or {}).get("result", {}).get("error_code") == 0)
    control(cfg, {"kick_clients": True})
    c.check("kick_clients: connection dropped", again.mqtt.wait_closed(3))
    again2 = SdkLikeClient(cfg, client_id=sdk.client_id)
    c.check("after a kick the client can reconnect", again2.connect_and_register() == "ok")

    # --- the access code changes on the printer while the slicer is connected ---
    control(cfg, {"access_code": "CHANGED9", "kick_clients": True})
    c.check("access code changed + connection dropped", again2.mqtt.wait_closed(3))
    stale = SdkLikeClient(cfg, client_id=sdk.client_id)  # the SDK's reconnect loop keeps the old code
    c.check("reconnect with the old code -> CONNACK 5 (SDK: INVALID_ACCESS_CODE; its loop retries every 5 s)",
            stale.connect() == 5)
    status, _ = http_request(cfg, "http", "GET", "/system/info?X-Token=" + code, headers={"X-Token": code})
    c.check("... and /system/info with the old code -> 401", status == 401, status)
    control(cfg, {"access_code": code})
    again2 = SdkLikeClient(cfg, client_id=sdk.client_id)
    c.check("code restored -> reconnect + register ok", again2.connect_and_register() == "ok")
    control(cfg, {"reset": True})
    c.check("control reset drops MQTT clients like a reboot", again2.mqtt.wait_closed(3))
    again3 = SdkLikeClient(cfg, client_id=sdk.client_id)
    c.check("after a reset the client reconnects, registers and is answered",
            again3.connect_and_register() == "ok"
            and (again3.request(1002) or {}).get("result", {}).get("error_code") == 0)
    again3.mqtt.disconnect()
    return c


VOLATILE = ("gcode_move",)


def strip_volatile(status):
    status = copy.deepcopy(status)
    status.pop("error_code", None)
    for key in VOLATILE:
        status.pop(key, None)
    return status


# ---------------------------------------------------------------------------- main

def wait_for_control(cfg, process, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        try:
            control(cfg)
            return True
        except (RuntimeError, ValueError):
            time.sleep(0.2)
    return False


def main():
    parser = argparse.ArgumentParser(description="Self-test for the fake CC2.")
    parser.add_argument("--running", action="store_true", help="test a fake already running on the default ports")
    parser.add_argument("--sn", default="FAKECC2SN0001")
    parser.add_argument("--access-code", default="FAKE42")
    args = parser.parse_args()

    ports = DEFAULT_PORTS if args.running else PRIVATE_PORTS
    cfg = Config("127.0.0.1", ports, args.sn, args.access_code)
    workdir = tempfile.mkdtemp(prefix="fakecc2_selftest_")
    process = None
    log_path = os.path.join(workdir, "fake.log")
    try:
        if not args.running:
            command = [sys.executable, "-I", os.path.join(HERE, "fakecc2.py"),
                       "--mqtt-port", str(ports["mqtt"]), "--http-port", str(ports["http"]),
                       "--camera-port", str(ports["camera"]), "--discovery-port", str(ports["discovery"]),
                       "--control-port", str(ports["control"]), "--sn", args.sn,
                       "--access-code", args.access_code, "--tick", "0.2",
                       "--received-dir", os.path.join(workdir, "received")]
            log_file = open(log_path, "w", encoding="utf-8", errors="replace")
            process = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            print("started fake: pid %d, ports %s, log %s" % (process.pid, ports, log_path), flush=True)
        if not wait_for_control(cfg, process):
            print("FAIL  the fake did not come up")
            return 1
        started = time.monotonic()
        checks = run_tests(cfg, workdir)
        failed = [r for r in checks.results if not r[1]]
        print("\n%d/%d checks passed in %.1f s" % (len(checks.results) - len(failed), len(checks.results),
                                                   time.monotonic() - started))
        return 1 if failed else 0
    finally:
        if args.running:  # leave the running fake as we found it
            try:
                control(cfg, {"reset": True, "delete_files": [CUBE_NAME, UNICODE_NAME, "evil.gcode"]})
            except (RuntimeError, ValueError):
                pass
        if process is not None:
            process.terminate()
            process.wait(10)
            log_file.close()
        if process is not None and os.environ.get("FAKECC2_KEEP_LOG"):
            print("fake log kept at", log_path)
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
