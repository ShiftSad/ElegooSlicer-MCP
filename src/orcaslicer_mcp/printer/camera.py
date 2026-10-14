"""get_camera_snapshot: the first complete JPEG from an Elegoo printer's camera stream.

The Centauri Carbon 2 serves its camera as an MJPEG stream (multipart/x-mixed-replace: one JPEG
after another, each in its own part) without authentication on port 8080; the Centauri Carbon
(CC1) on port 3031 at /video. Neither ElegooSlicer nor its printer SDK reads the camera (their
video goes through Elegoo's cloud): these addresses come from the community library pycentauri
and are to be confirmed on the hardware. The address of the printer comes from ElegooSlicer's
printer list, and only printers on the local network are tried. One HTTP GET, read until the
first whole picture, within 10 s and 8 MB. Read-only."""
from __future__ import annotations
import asyncio

import httpx

from .elegoo import host_url

TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 3.0
MAX_BYTES = 8 * 1024 * 1024
SOI = b"\xff\xd8"  # a JPEG starts with this marker...
EOI = b"\xff\xd9"  # ...and ends with this one


class CameraError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def as_dict(self) -> dict:
        out = {"error": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


def camera_url(printer: dict) -> str:
    """The camera stream's address for a printer from GET /printers, or CameraError
    "unsupported" saying why there is none."""
    name = printer.get("name") or "This printer"
    if printer.get("network") != "lan":
        raise CameraError("unsupported",
                          f"{name} is connected through Elegoo's cloud, and its camera can only be "
                          "read on the local network.",
                          hint="Add the printer in ElegooSlicer's Device tab by its IP address.")
    base = host_url(printer.get("host"))
    if not base:
        raise CameraError("unsupported", f"ElegooSlicer has no network address for {name}.")
    model = str(printer.get("model") or "").lower()
    if "centauri carbon 2" in model:
        return f"{base}:8080/"
    if "centauri carbon" in model:
        return f"{base}:3031/video"
    raise CameraError("unsupported",
                      f"No camera address is known for a {printer.get('model') or 'printer of this model'}: "
                      "only the Elegoo Centauri Carbon and Centauri Carbon 2 are supported.")


def boundary_of(content_type: str) -> bytes | None:
    """The multipart boundary named in a Content-Type header, or None. A leading '--' some
    cameras put into the parameter itself is dropped, so the delimiter is always '--' + this."""
    for param in (content_type or "").split(";")[1:]:
        key, _, value = param.strip().partition("=")
        if key.strip().lower() == "boundary":
            value = value.strip().strip('"').lstrip("-")
            return value.encode("latin-1") if value else None
    return None


def _jpeg(frame) -> bytes | None:
    """The frame as bytes when it is one whole JPEG (around it only line breaks, or the '--' of
    a delimiter written longer than the boundary), else None."""
    frame = bytes(frame).lstrip(b"\r\n").rstrip(b"\r\n-")
    return frame if frame.startswith(SOI) and frame.endswith(EOI) else None


def _content_length(headers: bytes) -> int | None:
    for line in headers.split(b"\r\n"):
        key, _, value = line.partition(b":")
        if key.strip().lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def first_jpeg(data, boundary: bytes | None, ended: bool = False) -> bytes | None:
    """The first complete JPEG in what the camera has sent so far, or None while there is none.
    `ended`: the stream is over, so its last part is all there will be.

    With a boundary, each part is headers, a blank line and one picture; a part whose length
    header says more than arrived, or whose picture is cut off, is skipped (a stream joined in the
    middle of a frame starts with such a part). A part without a length header ends at the next
    boundary. Without a boundary (a plain image/jpeg reply) the bytes from the first start marker
    to the next end marker are the picture."""
    data = bytes(data)
    if boundary is None:
        start = data.find(SOI)
        end = data.find(EOI, start + 2) if start >= 0 else -1
        return data[start:end + 2] if end >= 0 else None
    parts = data.split(b"--" + boundary)
    for i, part in enumerate(parts[1:], start=1):  # parts[0] came before the first boundary
        arriving = i == len(parts) - 1 and not ended
        head_end = part.find(b"\r\n\r\n")
        if head_end < 0:
            continue
        headers, body = part[:head_end], part[head_end + 4:]
        length = _content_length(headers)
        if length is not None:
            if len(body) < length:
                continue  # still arriving, or a cut-off part
            frame = _jpeg(body[:length])
        elif arriving:
            continue  # without a length, the part is whole only once the next boundary came
        else:
            frame = _jpeg(body)
        if frame:
            return frame
    return None


async def read_snapshot(url: str, *, timeout_s: float = TIMEOUT_S, max_bytes: int = MAX_BYTES) -> bytes:
    """GET the camera stream and return its first complete JPEG; CameraError otherwise."""
    async def read() -> bytes:
        timeout = httpx.Timeout(timeout_s, connect=min(CONNECT_TIMEOUT_S, timeout_s))
        async with httpx.AsyncClient(timeout=timeout) as http:
            async with http.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise CameraError("camera_error", f"The camera answered HTTP {resp.status_code}.")
                boundary = boundary_of(resp.headers.get("content-type", ""))
                received = bytearray()
                async for chunk in resp.aiter_bytes():
                    received += chunk
                    frame = first_jpeg(received, boundary)
                    if frame:
                        return frame
                    if len(received) > max_bytes:
                        raise CameraError("too_large",
                                          f"No complete picture within the first {max_bytes // (1024 * 1024)} MB "
                                          "from the camera.")
                frame = first_jpeg(received, boundary, ended=True)
                if frame:
                    return frame
                raise CameraError("camera_error", "The camera sent no complete picture.")

    timed_out = CameraError("camera_timeout", f"The camera sent no complete picture within {timeout_s:g} s.",
                            hint="Is the printer on? Its camera may be off or busy; try again.")
    try:
        return await asyncio.wait_for(read(), timeout_s)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise timed_out from None
    except httpx.HTTPError as e:
        raise CameraError("camera_unreachable", f"The camera didn't answer at {url}.",
                          hint="Is the printer switched on and on the same network as this computer?") from e
