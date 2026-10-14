"""Build paho_check.cpp against the slicer's own Paho MQTT C++ static libraries and run it.

Usage: py -3 -I tools\\remote-api\\fakecc2\\paho_check\\build_and_run.py [--mqtt 127.0.0.1:1883] [--sn FAKECC2SN0001]
                                                           [--access-code FAKE42] [--out <dir>]

Needs a fake CC2 running (py -3 -I tools\\remote-api\\fakecc2\\fakecc2.py) and the slicer's deps build
(deps\\build\\ElegooSlicer_dep). The executable goes to --out (default: a temp dir);
nothing is written into the repositories. Optional: the Python self-test already covers the
protocol; this proves the broker also satisfies the real Paho client the SDK uses.
"""
import argparse
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", "..", "..", ".."))
DEPS = os.path.join(REPO, "deps", "build", "ElegooSlicer_dep", "usr", "local")
VCVARS = r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mqtt", default="127.0.0.1:1883")
    parser.add_argument("--sn", default="FAKECC2SN0001")
    parser.add_argument("--access-code", default="FAKE42")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    out = args.out or tempfile.mkdtemp(prefix="fakecc2_paho_")
    os.makedirs(out, exist_ok=True)
    exe = os.path.join(out, "paho_check.exe")
    source = os.path.join(HERE, "paho_check.cpp")
    # Same CRT (/MD) and language level as the slicer build; Paho C needs ws2_32 + rpcrt4 on Windows.
    cmd = os.path.join(out, "build.cmd")
    with open(cmd, "w") as f:
        f.write("@echo off\r\nset VSCMD_SKIP_SENDTELEMETRY=1\r\n"
                f'call "{VCVARS}" -vcvars_ver=14.44 >nul\r\n'
                f'cl /nologo /EHsc /MD /O1 /std:c++17 /DNOMINMAX /I"{DEPS}\\include" "{source}" '
                f'/Fe"{exe}" /Fo"{out}\\\\" /link /LIBPATH:"{DEPS}\\lib" paho-mqttpp3-static.lib '
                f"paho-mqtt3a-static.lib ws2_32.lib rpcrt4.lib crypt32.lib advapi32.lib\r\n")
    build = subprocess.run(["cmd.exe", "/c", cmd], capture_output=True, text=True, errors="replace", cwd=out)
    if build.returncode != 0 or not os.path.exists(exe):
        print(build.stdout[-4000:], build.stderr[-2000:])
        print("BUILD FAILED (exit %d)" % build.returncode)
        return 1
    print("built", exe)
    run = subprocess.run([exe, args.mqtt, args.sn, args.access_code], cwd=out, timeout=120)
    return run.returncode


if __name__ == "__main__":
    sys.exit(main())
