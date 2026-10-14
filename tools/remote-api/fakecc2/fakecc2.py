"""Fake Elegoo Centauri Carbon 2 for testing ElegooSlicer's printer features without hardware.

Usage:  py -3 -I tools\\remote-api\\fakecc2\\fakecc2.py [options]        (Ctrl+C to stop; --help for options)

Starts, all bound to 127.0.0.1 by default:
  MQTT broker + CC2 logic   :1883   (the SDK hard-codes this port)
  printer HTTP              :80     /system/info, PUT /upload, /download
  camera (MJPEG)            :8080
  UDP discovery             :52700  answers {"id":0,"method":7000}
  control/inspection HTTP   :18080  GET/POST /state (test helper, not part of a real printer)
See README.md for what is emulated, what is not, and how to point the slicer at it.
"""
import argparse
import asyncio
import ipaddress
import os
import sys
import time
import traceback

# Keep `py -3 -I` (isolated mode, no script dir on sys.path) able to import the sibling modules.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from http_servers import CameraHandler, ControlHandler, PrinterHttpHandler, start_server  # noqa: E402
from mqtt_broker import MqttBroker  # noqa: E402
from printer import FakeCC2  # noqa: E402


class Log:
    def __init__(self, verbose):
        self.verbose = verbose

    def info(self, message):
        stamp = time.strftime("%H:%M:%S") + ".%03d" % (time.time() % 1 * 1000)
        print("%s %s" % (stamp, message), flush=True)

    def debug(self, message):
        if self.verbose:
            self.info(message)


class DiscoveryProtocol(asyncio.DatagramProtocol):
    """UDP :52700. The SDK broadcasts its probe to every adapter's broadcast address, including
    127.255.255.255 for the Windows loopback adapter, which reaches a socket bound to 127.0.0.1."""

    def __init__(self, printer, log):
        self.printer = printer
        self.log = log
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        reply = self.printer.discovery_reply(data)
        if reply is not None:
            self.log.info("udp: discovery probe from %s:%d answered" % addr)
            self.transport.sendto(reply, addr)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Fake Elegoo Centauri Carbon 2 (MQTT + HTTP + camera + discovery).")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to bind and to report (default 127.0.0.1; anything else exposes the fake on your network)")
    p.add_argument("--mqtt-port", type=int, default=1883, help="MQTT port (default 1883; the SDK always uses 1883)")
    p.add_argument("--http-port", type=int, default=80,
                   help="printer HTTP port (default 80; another port works if the slicer's printer host is '127.0.0.1:<port>')")
    p.add_argument("--camera-port", type=int, default=8080, help="MJPEG camera port (default 8080)")
    p.add_argument("--discovery-port", type=int, default=52700, help="UDP discovery port (default 52700)")
    p.add_argument("--control-port", type=int, default=18080, help="control/inspection HTTP port (default 18080)")
    p.add_argument("--no-camera", action="store_true", help="start with the camera off")
    p.add_argument("--no-discovery", action="store_true", help="do not answer UDP discovery")
    p.add_argument("--sn", default="FAKECC2SN0001", help="serial number (MQTT topics are elegoo/<sn>/...)")
    p.add_argument("--access-code", default="FAKE42", help="access code = MQTT password and HTTP X-Token")
    p.add_argument("--hostname", default="Fake CC2", help="printer name it reports")
    p.add_argument("--model", default="Centauri Carbon 2", help="machine_model it reports")
    p.add_argument("--firmware", default="02.01.00.00", help="software_version.ota_version it reports")
    p.add_argument("--received-dir", default=os.path.join(HERE, "received"), help="where uploaded files are stored")
    p.add_argument("--print-seconds", type=float, default=60.0, help="real seconds a print takes (default 60)")
    p.add_argument("--preheat-seconds", type=float, default=4.0, help="heating phase length (default 4)")
    p.add_argument("--leveling-seconds", type=float, default=3.0, help="auto-leveling phase when printer_check is on (default 3)")
    p.add_argument("--completed-hold-seconds", type=float, default=10.0,
                   help="how long 'print completed' is shown before idle; 0 keeps it (default 10)")
    p.add_argument("--tick", type=float, default=0.5, help="state machine / status push interval in seconds (default 0.5)")
    p.add_argument("--max-clients", type=int, default=4, help="registered client slots before 'too many clients' (default 4)")
    p.add_argument("--rate-limit", action="store_true",
                   help="emulate the firmware's cooldown: 3+ requests within 1 s -> answers dropped for 5 s")
    p.add_argument("--camera-fps", type=float, default=2.0, help="camera frames per second (default 2)")
    p.add_argument("--verbose", action="store_true", help="log every MQTT message and HTTP request")
    return p.parse_args(argv)


async def run(opts, log):
    printer = FakeCC2(opts, log)
    broker = MqttBroker(
        authenticate=printer.check_credentials,
        on_connect=printer.client_connected,
        on_message=printer.handle_mqtt,
        on_disconnect=printer.client_disconnected,
        is_online=printer.is_online,
        log=log)
    loop = asyncio.get_running_loop()

    try:
        await broker.start(opts.host, opts.mqtt_port)
    except OSError as error:
        exit_port_in_use("MQTT", opts.host, opts.mqtt_port, error)
    for what, handler, port in (("printer HTTP", PrinterHttpHandler, opts.http_port),
                                ("camera", CameraHandler, opts.camera_port),
                                ("control HTTP", ControlHandler, opts.control_port)):
        try:
            start_server(handler, opts.host, port, printer, log, opts)
        except OSError as error:
            exit_port_in_use(what, opts.host, port, error)
    if not opts.no_discovery:
        try:
            await loop.create_datagram_endpoint(lambda: DiscoveryProtocol(printer, log),
                                                local_addr=(opts.host, opts.discovery_port))
        except OSError as error:
            exit_port_in_use("UDP discovery", opts.host, opts.discovery_port, error)

    log.info("fake CC2 '%s' sn=%s access code=%s model='%s' firmware %s" % (
        opts.hostname, opts.sn, opts.access_code, opts.model, opts.firmware))
    log.info("listening on %s: mqtt %d, http %d, camera %d, discovery %s, control %d" % (
        opts.host, opts.mqtt_port, opts.http_port, opts.camera_port,
        "off" if opts.no_discovery else str(opts.discovery_port), opts.control_port))
    log.info("received files go to %s" % printer.received_dir)
    log.info("ready (inspect: http://%s:%d/state)" % (opts.host, opts.control_port))

    while True:  # the ticker: advances the print and pushes status deltas
        await asyncio.sleep(opts.tick)
        try:
            publishes, kick = printer.tick()
        except Exception:  # keep the fake alive and show the bug
            log.info("ticker error:\n%s" % traceback.format_exc())
            continue
        if kick:
            broker.disconnect_all()
        for topic, payload in publishes:
            broker.publish(topic, payload)


def exit_port_in_use(what, host, port, error):
    sys.exit("cannot bind %s on %s:%d (%s). Is another fake or service using it? "
             "Check with: netstat -ano | findstr :%d" % (what, host, port, error, port))


def main(argv=None):
    opts = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        # UTF-8 also when redirected to a file (Windows would use the ANSI code page): file names
        # sent by the slicer are UTF-8.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    log = Log(opts.verbose)
    try:
        if not ipaddress.ip_address(opts.host).is_loopback:
            log.info("WARNING: binding to %s exposes the fake printer (fixed access code) on your network" % opts.host)
    except ValueError:
        sys.exit("--host must be an IP address")
    try:
        asyncio.run(run(opts, log))
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
