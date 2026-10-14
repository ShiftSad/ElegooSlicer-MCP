"""Register the fake CC2 in an ElegooSlicer TEST data dir, the way the app persists a LAN printer.

Usage: py -3 -I tools\\remote-api\\fakecc2\\add_fake_printer.py <datadir> [options]
  --http-port N      the fake's HTTP port when it is not 80 (host becomes "127.0.0.1:N")
  --access-code X    default FAKE42 (must match the fake's --access-code)
  --sn X             default FAKECC2SN0001 (must match the fake's --sn)
  --name X           printer name shown in the slicer (default "Fake CC2")
  --no-serial        leave serialNumber/mainboardId empty, so the SDK asks GET /system/info on
                     every connect (the path of a printer typed in by hand before its first connect)
  --remove           only remove the fake's entry
  --force            write even if elegoo-slicer.exe is running

Run it while ElegooSlicer is CLOSED: the app keeps the printer list in memory and rewrites
<datadir>/user/printer_list.json (PrinterCache::savePrinterList), which would undo this.
Never point it at %APPDATA%\\ElegooSlicer (the real profile): the script refuses.

File format (src/slic3r/Utils/Elegoo/PrinterCache.cpp + src/libslic3r/PrinterNetworkInfo.cpp):
a JSON object printerId -> PrinterNetworkInfo as convertPrinterNetworkInfoToJson writes it, minus
the runtime fields savePrinterList drops (connectStatus, printerStatus, exceptions,
deviceAssistantStatus, printTask). nlohmann::json writes keys sorted, 4-space indent.
The access code is stored there in plain text, as the app itself does: UserDataStorage (DPAPI)
only encrypts the cloud login (<datadir>/user/user_info, UserNetworkManager.cpp:1050-1133).
selftest.py checks the keys and JSON types against those sources.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

FIRMWARE = "02.01.00.00"


def is_under(path, parent):
    try:
        return os.path.commonpath([os.path.abspath(path).lower(), os.path.abspath(parent).lower()]) == \
            os.path.abspath(parent).lower()
    except ValueError:  # different drives
        return False


def slicer_running():
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq elegoo-slicer.exe", "/NH"],
                             capture_output=True, text=True, errors="replace", timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "elegoo-slicer.exe" in out.lower()


def fake_entry(printer_id, host, args):
    """One printer as convertPrinterNetworkInfoToJson writes it (enum values from PrinterNetworkInfo.hpp)."""
    now = int(time.time())
    serial = "" if args.no_serial else args.sn
    return {
        "printerId": printer_id,
        "printerName": args.name,
        "host": host,
        "port": 0,
        "vendor": "Elegoo",
        "printerModel": "Elegoo Centauri Carbon 2",  # getPrinterType() -> ELEGOO_FDM_CC2
        "protocolVersion": "",
        "firmwareVersion": FIRMWARE,
        "hostType": "ElegooLink",                   # NetworkFactory::createPrinterNetwork -> ElegooPrinterNetwork
        "mainboardId": serial,                      # the SDK reports mainboardId = serial number
        "serialNumber": serial,
        "username": "",
        "password": "",
        "authMode": 2,                              # PRINTER_AUTH_MODE_ACCESS_CODE
        "token": "",
        "accessCode": args.access_code,
        "pinCode": "",
        "webUrl": "",
        "networkType": 0,                           # NETWORK_TYPE_LAN
        "extraInfo": {},
        "isPhysicalPrinter": False,
        "addTime": now,
        "modifyTime": now,
        "lastActiveTime": now,
        # What the SDK reports for a CC2 (handlePrinterAttributes); refreshed on every connect.
        "printCapabilities": {
            "supportsAutoBedLeveling": True,
            "supportsTimeLapse": True,
            "supportsHeatedBedSwitching": True,
            "supportsFilamentMapping": True,
            "supportsAutoRefill": True,
        },
        "systemCapabilities": {
            "supportsMultiFilament": True,
            "canGetDiskInfo": True,
            "canSetPrinterName": True,
        },
        "isAdded": False,
    }


def main():
    parser = argparse.ArgumentParser(description="Add the fake CC2 to a TEST ElegooSlicer data dir.")
    parser.add_argument("datadir")
    parser.add_argument("--http-port", type=int, default=80)
    parser.add_argument("--access-code", default="FAKE42")
    parser.add_argument("--sn", default="FAKECC2SN0001")
    parser.add_argument("--name", default="Fake CC2")
    parser.add_argument("--no-serial", action="store_true")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    datadir = os.path.abspath(args.datadir)
    appdata = os.environ.get("APPDATA")
    if (appdata and is_under(datadir, appdata)) or "appdata" in datadir.lower():
        sys.exit("refusing: %s is under AppData (the real ElegooSlicer profile). Use a test --datadir." % datadir)
    if not os.path.isdir(datadir):
        sys.exit("no such data dir: %s (start the app once with --datadir to create it)" % datadir)
    if slicer_running() and not args.force:
        sys.exit("elegoo-slicer.exe is running: close it first (it rewrites printer_list.json), or pass --force")

    user_dir = os.path.join(datadir, "user")
    os.makedirs(user_dir, exist_ok=True)
    list_path = os.path.join(user_dir, "printer_list.json")
    printers = {}
    if os.path.exists(list_path):
        try:
            with open(list_path, encoding="utf-8") as f:
                text = f.read()
            printers = json.loads(text) if text.strip() else {}
        except ValueError as error:
            sys.exit("%s is not valid JSON (%s); not touching it" % (list_path, error))
        if not isinstance(printers, dict):
            sys.exit("%s is not a JSON object; not touching it" % list_path)
        backup = list_path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(list_path, backup)
        print("backup:", backup)

    host = "127.0.0.1" if args.http_port == 80 else "127.0.0.1:%d" % args.http_port
    printer_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "fakecc2:" + args.sn))  # stable across runs
    # PrinterManager::addPrinter treats the same host, serial, mainboard id or id as the same printer.
    stale = [pid for pid, p in printers.items()
             if pid == printer_id or (isinstance(p, dict) and (
                 p.get("host") == host or p.get("serialNumber") == args.sn or p.get("mainboardId") == args.sn))]
    for pid in stale:
        print("removed existing entry %s (%s)" % (pid, printers[pid].get("printerName") if isinstance(printers[pid], dict) else "?"))
        del printers[pid]
    if not args.remove:
        printers[printer_id] = fake_entry(printer_id, host, args)

    tmp_path = list_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(printers, indent=4, sort_keys=True, ensure_ascii=False))
    os.replace(tmp_path, list_path)

    if args.remove:
        print("fake CC2 removed from", list_path)
        return
    print("fake CC2 registered in %s" % list_path)
    print("  printerId %s, host %s, serial %s, access code %s" % (
        printer_id, host, "(empty: fetched via /system/info)" if args.no_serial else args.sn, args.access_code))
    print("next: start the fake (py -3 -I tools\\remote-api\\fakecc2\\fakecc2.py%s), then the app with --datadir %s" % (
        "" if args.http_port == 80 else " --http-port %d" % args.http_port, datadir))


if __name__ == "__main__":
    main()
