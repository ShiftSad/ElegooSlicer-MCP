"""Elegoo printers, read through ElegooSlicer MCP's /printers routes.

ElegooSlicer talks to its printers itself (its Device tab: MQTT on the local network, or Elegoo's
cloud), so this backend never contacts a printer on its own: it asks the slicer, which answers
from its printer cache. get_printer_status, wait_for_printer, list_print_history and
check_printer_match use it when the slicer lists "printers" in its capabilities and the active
printer profile connects through ElegooSlicer (host_type elegoolink) or has no address.

What ElegooSlicer keeps about a printer is less than Klipper reports: the state, the job's file,
progress and times, and the printer's exception codes. It keeps no temperatures and no layer
numbers, so this backend reports none and refuses the waits that would need them. Read-only."""
from __future__ import annotations
import datetime
from dataclasses import replace
from urllib.parse import urlsplit

from ..errors import ApiError, NotFound, NotReachable, Unauthorized, ConfigError
from .errors import PrinterError
from .match import summarize
from .problems import problem
from .status import headline, _f, _whole

# ElegooSlicer's printer status (GET /printers "status") -> the protocol-neutral state that
# get_printer_status reports for every kind of printer (status.py). Every status not listed is
# "busy": the printer is doing something other than printing, often a step of a print (homing,
# auto leveling, loading filament from the CANVAS between colours, receiving the file).
STATE = {
    "offline": "offline", "idle": "idle", "printing": "printing", "paused": "paused",
    "pausing": "paused", "canceled": "cancelled", "print_completed": "finished",
    "preheating": "heating", "error": "error", "emergency_stop": "error",
    # Connection problems: ElegooSlicer reaches something, but not the printer it added.
    "id_not_match": "offline", "auth_error": "offline",
}

# Statuses that are problems in themselves, with what to tell the user.
STATUS_PROBLEMS = {
    "offline": "ElegooSlicer isn't connected to the printer: is it switched on and on the same "
               "network? ElegooSlicer's Device tab shows the connection.",
    "error": "The printer reports an error; its screen shows what it is.",
    "emergency_stop": "The printer was emergency-stopped.",
    "id_not_match": "The printer at this address isn't the one ElegooSlicer added (its id doesn't "
                    "match). Add it again in ElegooSlicer's Device tab.",
    "auth_error": "The printer refused ElegooSlicer's access code. Enter it again in "
                  "ElegooSlicer's Device tab (it is shown on the printer's screen).",
}

HISTORY_LAN_REASON = ("ElegooSlicer gets an Elegoo printer's print history from Elegoo's cloud only, "
                      "so a printer added on the local network has none to show here. The "
                      "printer's own screen lists its past prints.")


def printer_error(e: ApiError) -> PrinterError:
    """A failed /printers request as the printer tools report problems. The transient ones (the
    printer or the slicer's printer layer did not answer in time) are not_reachable, so
    wait_for_printer rides them out like a network blip."""
    token = e.body.get("error") if isinstance(e.body, dict) else None
    if isinstance(e, NotFound) and e.route_missing:
        return PrinterError("unsupported_connection",
                            "This ElegooSlicer build can't report its printers.",
                            hint="Use an ElegooSlicer MCP build that lists 'printers' in get_status "
                                 "capabilities.")
    if token == "printer_not_found":
        return PrinterError("not_configured", "ElegooSlicer no longer has this printer.",
                            hint="list_printers shows the printers in ElegooSlicer's Device tab.")
    if token in ("printer_timeout", "printers_unavailable", "printer_error", "printer_offline"):
        return PrinterError("not_reachable", f"ElegooSlicer couldn't reach the printer ({token}).",
                            hint="Check that the printer is on and connected in ElegooSlicer's Device "
                                 "tab, then try again in a few seconds.")
    if isinstance(e, (NotReachable, Unauthorized, ConfigError)):
        return PrinterError("orca_unreachable", "Couldn't read the printer through ElegooSlicer.",
                            hint="Start ElegooSlicer MCP with the Remote API enabled, and check "
                                 "ORCA_API_TOKEN.", detail=str(e))
    return PrinterError("protocol_error", f"ElegooSlicer answered '{token or e}' about the printer.")


def pick_printer(listing: dict, wanted: str | None = None) -> dict:
    """The printer a tool acts on, from GET /printers: the one named by id or by name (any case),
    else the one ElegooSlicer's Print dialog would preselect (selected_id)."""
    printers = [p for p in (listing or {}).get("printers") or [] if isinstance(p, dict)]
    if not printers:
        raise PrinterError("not_configured", "ElegooSlicer has no printers.",
                           hint="Add the printer in ElegooSlicer's Device tab (by its IP address and "
                                "the access code shown on its screen).")
    if wanted is None:
        selected = listing.get("selected_id")
        return next((p for p in printers if p.get("id") == selected), printers[0])
    for p in printers:
        if p.get("id") == wanted:
            return p
    named = [p for p in printers if str(p.get("name") or "").casefold() == str(wanted).casefold()]
    if len(named) == 1:
        return named[0]
    names = [f"{p.get('name')} ({p.get('id')})" for p in printers]
    if named:
        raise PrinterError("not_configured", f"Several printers are called '{wanted}'.",
                           hint="Pass the printer's id instead: " + ", ".join(names))
    raise PrinterError("not_configured", f"ElegooSlicer has no printer '{wanted}'.",
                       hint="ElegooSlicer's printers: " + ", ".join(names))


def host_url(host) -> str:
    """http://<host> for a printer's host field, without any user name, password, port or path
    (the slicer already cuts credentials off; this cuts again). '' when there is no host."""
    text = str(host or "").strip()
    if not text:
        return ""
    try:
        name = urlsplit(text if "://" in text else "http://" + text).hostname
    except ValueError:
        return ""
    if not name:
        return ""
    return f"http://[{name}]" if ":" in name else f"http://{name}"


class ElegooClient:
    """The slicer client, bound to one printer: what the printer tools talk to for an Elegoo
    printer in place of a Moonraker or OctoPrint client. Closing it closes the slicer client."""

    def __init__(self, fork, printer: dict):
        self._fork = fork
        self.printer = printer  # the GET /printers entry this client was opened for
        self.printer_id = str(printer.get("id"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._fork.__aexit__(None, None, None)

    async def status(self) -> dict:
        try:
            return await self._fork.get_printer_status(self.printer_id)
        except ApiError as e:
            raise printer_error(e) from e

    async def tasks(self, size: int) -> dict | None:
        """One page of the printer's print history, or None when ElegooSlicer has none for it
        (a LAN printer: 501 unsupported_on_lan)."""
        try:
            return await self._fork.get_printer_tasks(self.printer_id, 1, size)
        except ApiError as e:
            if isinstance(e.body, dict) and e.body.get("error") == "unsupported_on_lan":
                return None
            raise printer_error(e) from e

    async def config(self, keys: list[str]) -> dict:
        try:
            return await self._fork.get_config(keys)
        except ApiError as e:
            raise printer_error(e) from e


async def open_printer(target, fork_factory):
    """Return (target filled in with the printer's id, name and address, an open ElegooClient)
    for the printer ElegooSlicer's Print dialog would preselect. The caller closes the client; on
    any failure here the slicer client is closed."""
    try:
        fork = fork_factory()
    except ApiError as e:
        raise printer_error(e) from e
    try:
        listing = await fork.get_printers()
        if not listing.get("printers") and target.host_type != "elegoolink":
            # A profile with no address, on a slicer with no Elegoo printer either.
            raise PrinterError("not_configured",
                               f"The printer profile '{target.profile}' has no connection set up, "
                               "and ElegooSlicer has no printers.",
                               hint="Add an Elegoo printer in ElegooSlicer's Device tab. For a "
                                    "Klipper or OctoPrint printer, enter its address in the printer "
                                    "profile's connection settings, or set ORCA_PRINTER_URL in this "
                                    "MCP server's settings.")
        printer = pick_printer(listing)
    except ApiError as e:
        await fork.__aexit__(None, None, None)
        raise printer_error(e) from e
    except BaseException:
        await fork.__aexit__(None, None, None)
        raise
    found = replace(target, url=host_url(printer.get("host")), printer_id=str(printer.get("id")),
                    name=printer.get("name"))
    return found, ElegooClient(fork, printer)


def _job(task, state: str) -> dict | None:
    """The job block of the neutral status. ElegooSlicer keeps the current task only while the
    printer prints, pauses or is paused, so a finished or cancelled job has no file name left."""
    if isinstance(task, dict):
        remaining = _f(task.get("estimated_s"), 0)  # the printer's own "time remaining"
        return {"file": task.get("file"), "state": "paused" if state == "paused" else "printing",
                "progress_percent": _whole(task.get("progress")),
                "layer": {"current": None, "total": None},
                "elapsed_s": _f(task.get("current_s"), 0),
                "remaining_s": None if remaining is None else int(remaining),
                "remaining_basis": None if remaining is None else "printer_estimate",
                "filament_used_mm": None, "first_layer_height": None}
    ended = {"finished": "complete", "cancelled": "cancelled"}.get(state)
    if ended is None:
        return None
    return {"file": None, "state": ended, "progress_percent": None,
            "layer": {"current": None, "total": None}, "elapsed_s": None, "remaining_s": None,
            "remaining_basis": None, "filament_used_mm": None, "first_layer_height": None}


def elegoo_snapshot(printer: dict, target_public: dict) -> dict:
    """GET /printers/{id}/status in the shape get_printer_status reports for every printer."""
    raw = str(printer.get("status") or "unknown")
    if not printer.get("connected") and raw not in ("id_not_match", "auth_error"):
        raw = "offline"
    state = STATE.get(raw, "busy")
    problems = []
    if raw in STATUS_PROBLEMS:
        problems.append(problem("fatal", "printer", STATUS_PROBLEMS[raw]))
    for ex in printer.get("exceptions") or []:
        code = ex.get("code") if isinstance(ex, dict) else ex
        if code not in (None, ""):
            # ElegooSlicer keeps only the code, so its meaning is not known here: pass it on.
            problems.append(problem("warning", "printer",
                                    f"The printer reports exception code {code} (its screen "
                                    "explains it)."))
    body = {
        "printer": target_public, "connected": state != "offline", "state": state,
        "activity": raw.replace("_", " ") if state == "busy" else None,
        "temps": {"nozzle": None, "bed": None},
        "job": _job(printer.get("task"), state),
        "problems": problems,
        "elegoo": {"status": raw, "status_code": printer.get("status_code"),
                   "model": printer.get("model"), "firmware": printer.get("firmware"),
                   "network": printer.get("network"), "age_s": printer.get("age_s")},
    }
    return {"headline": headline(body), **body}


def _when(ts) -> str | None:
    t = _f(ts, 0)
    if not t or t <= 0:
        return None
    try:
        return (datetime.datetime.fromtimestamp(t, datetime.timezone.utc).astimezone()
                .isoformat(timespec="minutes"))
    except (OverflowError, OSError, ValueError):
        return None


async def print_history(target, client: ElegooClient, limit: int) -> dict:
    limit = max(1, min(int(limit), 50))
    reply = await client.tasks(limit)
    if reply is None:
        return {"supported": False, "reason": HISTORY_LAN_REASON, "printer": target.public()}
    jobs = []
    for item in reply.get("items") or []:
        if not isinstance(item, dict):
            continue
        begin, end = _f(item.get("begin_ts"), 0), _f(item.get("end_ts"), 0)
        jobs.append({"name": item.get("name"), "started_at": _when(begin), "ended_at": _when(end),
                     "duration_s": int(end - begin) if begin and end and end >= begin else None,
                     # Elegoo's cloud task status; ElegooSlicer does not say what each value means.
                     "status_code": item.get("status_code")})
    return {"printer": target.public(), "jobs": jobs, "total": reply.get("total"),
            "note": "From Elegoo's cloud. These jobs are not copied into the outcome store: the "
                    "cloud's status codes don't say whether a print succeeded."}


def _truthy(v) -> bool | None:
    """A config flag as GET /config reports it ("1"/"0", or a JSON bool), else None."""
    if isinstance(v, bool):
        return v
    if v in ("1", 1, "true"):
        return True
    if v in ("0", 0, "false"):
        return False
    return None


def _check(name, status, profile, printer, why) -> dict:
    return {"check": name, "status": status, "profile": profile, "printer": printer, "why": why}


def check_model(cfg: dict, printer: dict) -> dict:
    mine, theirs = cfg.get("printer_model"), printer.get("model")
    if not mine or not theirs:
        return _check("model", "unknown", mine, theirs, "Not enough data to compare.")
    if str(mine).strip().casefold() == str(theirs).strip().casefold():
        return _check("model", "ok", mine, theirs, "The profile is made for this printer model.")
    return _check("model", "warn", mine, theirs,
                  f"The profile is made for a {mine}, but the printer is a {theirs}: its bed size, "
                  "start G-code and filament changes may not fit. Select a profile for this model.")


def check_canvas(cfg: dict, printer: dict) -> dict:
    wants = _truthy(cfg.get("support_mms"))
    has = (printer.get("supports") or {}).get("canvas")
    if wants is None or not isinstance(has, bool):
        return _check("canvas", "unknown", wants, has, "Not enough data to compare.")
    if wants and not has:
        return _check("canvas", "warn", wants, has,
                      "The profile feeds filament through a CANVAS multi-material unit, but the "
                      "printer reports none.")
    if has and not wants:
        return _check("canvas", "warn", wants, has,
                      "The printer has a CANVAS, but this profile doesn't use it, so a print uses "
                      "one filament only.")
    return _check("canvas", "ok", wants, has,
                  "The profile and the printer agree on the CANVAS multi-material unit.")


async def check_match(target, client: ElegooClient) -> dict:
    cfg = await client.config(["printer_model", "support_mms"])
    out = summarize([check_model(cfg, client.printer), check_canvas(cfg, client.printer)])
    out["note"] = ("Elegoo printers don't report their nozzle, bed size, limits or temperatures to "
                   "ElegooSlicer, so only the model and the CANVAS are compared.")
    out["printer"] = target.public()
    return out
