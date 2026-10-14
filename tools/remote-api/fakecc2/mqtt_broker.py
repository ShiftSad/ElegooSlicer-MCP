"""Minimal MQTT 3.1.1 broker (asyncio), just enough for the elegoo-link SDK's Paho client.

On a real Centauri Carbon 2 the printer itself is the MQTT broker (tcp://<printer>:1883), so the
fake printer has to be one too. Supported:
- CONNECT / CONNACK with the username/password checked by a callback. MQTT 3.1.1 ("MQTT", level 4)
  and MQTT 3.1 ("MQIsdp", level 3): Paho's default version retries with 3.1 after a refusal.
- SUBSCRIBE / SUBACK (with + and # wildcards), UNSUBSCRIBE / UNSUBACK.
- PUBLISH from clients at QoS 0/1/2 (PUBACK, or PUBREC + PUBCOMP), delivery to subscribers at
  QoS 0/1 (we never send QoS 2), PINGREQ / PINGRESP, DISCONNECT, keep-alive timeout, and a new
  connection with an existing client id replaces the old one (as the MQTT spec says).
Not supported, because the SDK does not use them: retained messages, will messages (parsed and
ignored), persistent sessions (every session is treated as clean), MQTT 5.
"""
import asyncio
import itertools
import struct
import traceback

# Control packet types (high nibble of the first byte).
CONNECT = 1
CONNACK = 2
PUBLISH = 3
PUBACK = 4
PUBREC = 5
PUBREL = 6
PUBCOMP = 7
SUBSCRIBE = 8
SUBACK = 9
UNSUBSCRIBE = 10
UNSUBACK = 11
PINGREQ = 12
PINGRESP = 13
DISCONNECT = 14

# CONNACK return codes (MQTT 3.1.1 section 3.2.2.3).
CONNACK_ACCEPTED = 0
CONNACK_BAD_PROTOCOL_VERSION = 1
CONNACK_IDENTIFIER_REJECTED = 2
CONNACK_NOT_AUTHORIZED = 5  # what mosquitto answers for a wrong password; the SDK maps 4 and 5 alike

CONNECT_TIMEOUT_S = 10  # a client must send CONNECT this soon after opening the socket


class ProtocolError(Exception):
    """The client sent something that is not valid MQTT; we drop the connection."""


def encode_remaining_length(length):
    """MQTT variable-length integer: 7 bits per byte, high bit means 'more bytes follow'."""
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            return bytes(out)


def encode_string(text):
    data = text.encode("utf-8")
    return struct.pack("!H", len(data)) + data


def make_packet(packet_type, flags, body=b""):
    return bytes([(packet_type << 4) | flags]) + encode_remaining_length(len(body)) + body


def topic_matches(topic_filter, topic):
    """True when `topic` matches a subscription filter that may use + (one level) and # (rest)."""
    filter_parts = topic_filter.split("/")
    topic_parts = topic.split("/")
    for i, part in enumerate(filter_parts):
        if part == "#":
            return True
        if i >= len(topic_parts):
            return False
        if part != "+" and part != topic_parts[i]:
            return False
    return len(filter_parts) == len(topic_parts)


async def read_packet(reader):
    """Read one control packet. Returns (packet_type, flags, body)."""
    first = (await reader.readexactly(1))[0]
    length = 0
    multiplier = 1
    for _ in range(4):
        byte = (await reader.readexactly(1))[0]
        length += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            break
        multiplier *= 128
    else:
        raise ProtocolError("remaining length longer than 4 bytes")
    body = await reader.readexactly(length) if length else b""
    return first >> 4, first & 0x0F, body


class BodyReader:
    """Reads the fields of a packet body in order."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def _take(self, count):
        if self.pos + count > len(self.data):
            raise ProtocolError("packet body too short")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def u8(self):
        return self._take(1)[0]

    def u16(self):
        return struct.unpack("!H", self._take(2))[0]

    def binary(self):
        return self._take(self.u16())

    def string(self):
        return self.binary().decode("utf-8", "replace")

    def rest(self):
        chunk = self.data[self.pos:]
        self.pos = len(self.data)
        return chunk

    def at_end(self):
        return self.pos >= len(self.data)


class Session:
    """One connected MQTT client."""

    def __init__(self, writer, peer):
        self.writer = writer
        self.peer = peer
        self.client_id = None
        self.subscriptions = {}  # topic filter -> granted QoS (0 or 1)
        self.packet_ids = itertools.cycle(range(1, 65536))
        self.closed = False

    def send(self, data):
        if not self.closed:
            self.writer.write(data)

    def close(self):
        if not self.closed:
            self.closed = True
            self.writer.close()


class MqttBroker:
    """The broker. The printer logic plugs in through callbacks (all called on the event loop):

    authenticate(username, password) -> bool
    on_connect(client_id)
    on_message(client_id, topic, payload_bytes) -> list of (topic, payload_bytes) to publish
    on_disconnect(client_id)
    is_online() -> bool   (False: new connections are closed straight away, like an offline printer)
    """

    def __init__(self, authenticate, on_connect, on_message, on_disconnect, is_online, log):
        self.authenticate = authenticate
        self.on_connect = on_connect
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.is_online = is_online
        self.log = log
        self.sessions = {}  # client_id -> Session
        self.server = None
        self.anonymous_ids = itertools.count(1)

    async def start(self, host, port):
        self.server = await asyncio.start_server(self._serve, host, port)

    def publish(self, topic, payload, qos=0):
        """Deliver a message to every session with a matching subscription."""
        for session in list(self.sessions.values()):
            granted = [q for f, q in session.subscriptions.items() if topic_matches(f, topic)]
            if not granted:
                continue
            delivery_qos = min(qos, max(granted))
            body = encode_string(topic)
            if delivery_qos:
                body += struct.pack("!H", next(session.packet_ids))
            session.send(make_packet(PUBLISH, delivery_qos << 1, body + payload))

    def disconnect_all(self):
        """Drop every client (offline printer, network glitch). The SDK reconnects on its own."""
        for session in list(self.sessions.values()):
            session.close()

    def connected_client_ids(self):
        return sorted(self.sessions)

    async def _serve(self, reader, writer):
        peer = writer.get_extra_info("peername")
        if not self.is_online():
            writer.close()  # an offline printer does not answer at all
            return
        session = Session(writer, peer)
        try:
            packet_type, _, body = await asyncio.wait_for(read_packet(reader), CONNECT_TIMEOUT_S)
            if packet_type != CONNECT:
                raise ProtocolError("first packet is not CONNECT")
            keepalive = self._handle_connect(session, body)
            await writer.drain()
            if keepalive is None:
                return
            # MQTT 3.1.1 section 3.1.2.10: drop the client after 1.5 keep-alive periods of silence.
            timeout = keepalive * 1.5 if keepalive else None
            while not session.closed:
                packet_type, flags, body = await asyncio.wait_for(read_packet(reader), timeout)
                if packet_type == DISCONNECT:
                    break
                self._dispatch(session, packet_type, flags, body)
                await writer.drain()
        except asyncio.TimeoutError:
            self.log.info("mqtt: %s timed out" % (session.client_id or peer,))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass  # client went away
        except ProtocolError as error:
            self.log.info("mqtt: dropping %s: %s" % (session.client_id or peer, error))
        finally:
            self._forget(session)
            session.close()

    def _handle_connect(self, session, body):
        """Answer CONNACK. Returns the keep-alive in seconds, or None when the connection is refused."""
        r = BodyReader(body)
        protocol = r.string()
        level = r.u8()
        flags = r.u8()
        keepalive = r.u16()
        client_id = r.string()
        if flags & 0x04:  # will flag: will topic + will message, not used by the SDK
            r.string()
            r.binary()
        username = r.string() if flags & 0x80 else None
        password = r.binary().decode("utf-8", "replace") if flags & 0x40 else None

        if (protocol, level) not in (("MQTT", 4), ("MQIsdp", 3)):
            session.send(make_packet(CONNACK, 0, bytes([0, CONNACK_BAD_PROTOCOL_VERSION])))
            self.log.info("mqtt: refused %s: protocol %s level %d" % (session.peer, protocol, level))
            return None
        if not client_id:
            if not flags & 0x02:  # an empty client id needs a clean session
                session.send(make_packet(CONNACK, 0, bytes([0, CONNACK_IDENTIFIER_REJECTED])))
                return None
            client_id = "anonymous-%d" % next(self.anonymous_ids)
        if not self.authenticate(username, password):
            session.send(make_packet(CONNACK, 0, bytes([0, CONNACK_NOT_AUTHORIZED])))
            self.log.info("mqtt: refused %s: wrong username or access code (user=%r)" % (client_id, username))
            return None

        old = self.sessions.get(client_id)
        if old is not None:
            self.log.info("mqtt: %s reconnected, closing its old connection" % client_id)
            old.close()
        session.client_id = client_id
        self.sessions[client_id] = session
        session.send(make_packet(CONNACK, 0, bytes([0, CONNACK_ACCEPTED])))
        self.log.info("mqtt: %s connected (%s, keepalive %ds)" % (client_id, protocol, keepalive))
        self.on_connect(client_id)
        return keepalive

    def _dispatch(self, session, packet_type, flags, body):
        r = BodyReader(body)
        if packet_type == PUBLISH:
            qos = (flags >> 1) & 0x03
            topic = r.string()
            packet_id = r.u16() if qos else None
            payload = r.rest()
            if qos == 1:
                session.send(make_packet(PUBACK, 0, struct.pack("!H", packet_id)))
            elif qos == 2:
                session.send(make_packet(PUBREC, 0, struct.pack("!H", packet_id)))
            self.log.debug("mqtt: %s -> %s %s" % (session.client_id, topic, payload[:300]))
            self.publish(topic, payload, min(qos, 1))  # other subscribers (e.g. a monitor on elegoo/#)
            try:
                replies = self.on_message(session.client_id, topic, payload)
            except Exception:  # a bug in the printer logic must not drop the slicer's connection
                self.log.info("mqtt: error handling %s from %s:\n%s" % (topic, session.client_id, traceback.format_exc()))
                replies = []
            for reply_topic, reply_payload in replies:
                self.log.debug("mqtt: printer -> %s %s" % (reply_topic, reply_payload[:300]))
                self.publish(reply_topic, reply_payload)
        elif packet_type == PUBREL:
            session.send(make_packet(PUBCOMP, 0, struct.pack("!H", r.u16())))
        elif packet_type in (PUBACK, PUBREC, PUBCOMP):
            pass  # acknowledgements of our QoS 1 deliveries: nothing to retry, nothing to do
        elif packet_type == SUBSCRIBE:
            packet_id = r.u16()
            granted = []
            while not r.at_end():
                topic_filter = r.string()
                requested = r.u8() & 0x03
                session.subscriptions[topic_filter] = min(requested, 1)
                granted.append(min(requested, 1))
                self.log.debug("mqtt: %s subscribed to %s" % (session.client_id, topic_filter))
            session.send(make_packet(SUBACK, 0, struct.pack("!H", packet_id) + bytes(granted)))
        elif packet_type == UNSUBSCRIBE:
            packet_id = r.u16()
            while not r.at_end():
                session.subscriptions.pop(r.string(), None)
            session.send(make_packet(UNSUBACK, 0, struct.pack("!H", packet_id)))
        elif packet_type == PINGREQ:
            session.send(make_packet(PINGRESP, 0))
        else:
            raise ProtocolError("unexpected packet type %d" % packet_type)

    def _forget(self, session):
        if session.client_id and self.sessions.get(session.client_id) is session:
            del self.sessions[session.client_id]
            self.log.info("mqtt: %s disconnected" % session.client_id)
            self.on_disconnect(session.client_id)
