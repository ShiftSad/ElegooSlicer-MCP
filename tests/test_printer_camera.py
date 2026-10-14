"""get_camera_snapshot: reading the first whole JPEG from a printer's MJPEG stream."""
import asyncio

import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.printer import camera
from orcaslicer_mcp.printer.camera import CameraError, boundary_of, camera_url, first_jpeg, read_snapshot

F = "http://x:13130/api/v1"
CAM = "http://192.168.1.50:8080/"
MJPEG = "multipart/x-mixed-replace; boundary=frame"


def jpeg(n=1) -> bytes:
    """A small fake picture: the JPEG start marker, some bytes, the end marker."""
    return b"\xff\xd8" + bytes([n]) * 50 + b"\xff\xd9"


def part(body: bytes, length=True, boundary=b"frame") -> bytes:
    head = b"--" + boundary + b"\r\nContent-Type: image/jpeg\r\n"
    if length:
        head += b"Content-Length: %d\r\n" % len(body)
    return head + b"\r\n" + body + b"\r\n"


def printer(model="Elegoo Centauri Carbon 2", network="lan", host="192.168.1.50"):
    return {"id": "p1", "name": "CC2", "model": model, "host": host, "network": network,
            "connected": True, "status": "printing"}


# --- parsing -------------------------------------------------------------------------------------


def test_boundary_from_the_content_type():
    assert boundary_of(MJPEG) == b"frame"
    assert boundary_of('multipart/x-mixed-replace;boundary="--myboundary"') == b"myboundary"
    assert boundary_of("image/jpeg") is None and boundary_of("") is None


def test_a_part_with_a_length_is_taken_without_waiting_for_the_next_boundary():
    assert first_jpeg(part(jpeg(1)), b"frame") == jpeg(1)


def test_a_part_without_a_length_needs_the_next_boundary():
    data = part(jpeg(1), length=False)
    assert first_jpeg(data, b"frame") is None
    assert first_jpeg(data + b"--frame\r\n", b"frame") == jpeg(1)
    assert first_jpeg(data, b"frame", ended=True) == jpeg(1)


def test_a_frame_still_arriving_is_not_returned():
    data = part(jpeg(1))
    assert first_jpeg(data[:-20], b"frame") is None


def test_a_cut_off_first_frame_is_skipped():
    # Joining a stream in the middle of a frame: the first part is the tail of a picture.
    tail = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg(9)[25:] + b"\r\n"
    assert first_jpeg(tail + part(jpeg(2)), b"frame") == jpeg(2)
    # A part whose length header promises more than it holds is skipped too.
    short = b"--frame\r\nContent-Length: 500\r\n\r\n" + jpeg(9) + b"\r\n"
    assert first_jpeg(short + part(jpeg(3)), b"frame") == jpeg(3)


def test_a_delimiter_written_longer_than_the_boundary():
    # Some cameras put "--" into the boundary parameter and then write "----frame".
    data = part(jpeg(4), length=False, boundary=b"--frame") + b"----frame\r\n"
    assert first_jpeg(data, boundary_of("multipart/x-mixed-replace; boundary=--frame")) == jpeg(4)


def test_without_a_boundary_the_markers_decide():
    assert first_jpeg(b"junk" + jpeg(5) + b"more", None) == jpeg(5)
    assert first_jpeg(jpeg(5)[:-2], None) is None


# --- which camera ----------------------------------------------------------------------------------


def test_camera_address_by_model():
    assert camera_url(printer()) == CAM
    assert camera_url(printer("Elegoo Centauri Carbon")) == "http://192.168.1.50:3031/video"
    assert camera_url(printer(host="http://u:p@192.168.1.50:80/")) == CAM
    for p, words in ((printer(network="wan"), "cloud"), (printer(host=""), "no network address"),
                     (printer("Elegoo Neptune 4"), "Neptune")):
        with pytest.raises(CameraError) as e:
            camera_url(p)
        assert e.value.code == "unsupported" and words in e.value.message


# --- reading the stream ------------------------------------------------------------------------------


class Chunks(httpx.AsyncByteStream):
    """A stream that sends its chunks one by one, optionally pausing before each."""

    def __init__(self, chunks, pause=0.0):
        self.chunks, self.pause = chunks, pause

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.pause:
                await asyncio.sleep(self.pause)
            yield chunk


def stream(chunks, content_type=MJPEG, pause=0.0, status=200):
    return httpx.Response(status, headers={"content-type": content_type}, stream=Chunks(chunks, pause))


@respx.mock
async def test_reads_the_first_frame_across_chunks():
    data = part(jpeg(1)) + part(jpeg(2))
    respx.get(CAM).mock(return_value=stream([data[i:i + 7] for i in range(0, len(data), 7)]))
    assert await read_snapshot(CAM) == jpeg(1)


@respx.mock
async def test_a_single_jpeg_reply():
    respx.get(CAM).mock(return_value=stream([jpeg(3)], content_type="image/jpeg"))
    assert await read_snapshot(CAM) == jpeg(3)


@respx.mock
async def test_the_size_cap():
    respx.get(CAM).mock(return_value=stream([b"--frame\r\n\r\n" + b"\x00" * 600] * 5))
    with pytest.raises(CameraError) as e:
        await read_snapshot(CAM, max_bytes=1000)
    assert e.value.code == "too_large"


@respx.mock
async def test_the_time_limit():
    respx.get(CAM).mock(return_value=stream([b"--frame\r\n"] * 100, pause=0.05))
    with pytest.raises(CameraError) as e:
        await read_snapshot(CAM, timeout_s=0.2)
    assert e.value.code == "camera_timeout"


@respx.mock
async def test_unreachable_bad_status_and_no_picture():
    respx.get(CAM).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(CameraError) as e:
        await read_snapshot(CAM)
    assert e.value.code == "camera_unreachable" and "same network" in e.value.hint
    respx.get(CAM).mock(return_value=stream([b""], status=404))
    with pytest.raises(CameraError) as e:
        await read_snapshot(CAM)
    assert e.value.code == "camera_error" and "404" in e.value.message
    respx.get(CAM).mock(return_value=stream([b"--frame\r\n\r\nnot a picture\r\n"]))
    with pytest.raises(CameraError) as e:
        await read_snapshot(CAM)
    assert e.value.code == "camera_error"


# --- the tool -------------------------------------------------------------------------------------


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


def listing(*printers):
    respx.get(f"{F}/printers").mock(return_value=httpx.Response(
        200, json={"printers": list(printers), "selected_id": "p1"}))


@respx.mock
async def test_snapshot_returns_a_jpeg_image(monkeypatch):
    _env(monkeypatch)
    listing(printer())
    cam = respx.get(CAM).mock(return_value=stream([part(jpeg(7))]))
    out = await srv.get_camera_snapshot()
    assert isinstance(out, srv.Image) and out.data == jpeg(7) and out._mime_type == "image/jpeg"
    assert cam.called


@respx.mock
async def test_snapshot_of_a_cloud_printer_is_unsupported(monkeypatch):
    _env(monkeypatch)
    listing(printer(network="wan"))
    out = await srv.get_camera_snapshot()
    assert out["error"] == "unsupported" and "local network" in out["message"]


@respx.mock
async def test_snapshot_without_printers(monkeypatch):
    _env(monkeypatch)
    listing()
    out = await srv.get_camera_snapshot()
    assert out["error"] == "not_configured"
    respx.get(f"{F}/printers").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.get_camera_snapshot()
    assert "'printers'" in out["error"]


def test_limits_are_the_specified_ones():
    assert camera.TIMEOUT_S == 10.0 and camera.MAX_BYTES == 8 * 1024 * 1024
