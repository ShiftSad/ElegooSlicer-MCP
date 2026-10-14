"""Build sdk_check.cpp against the slicer's own elegoo-link SDK (static) and run it against a fake CC2.

Usage: py -3 -I tools\\remote-api\\fakecc2\\sdk_check\\build_and_run.py [--host 127.0.0.1] [--sn FAKECC2SN0001]
                                                         [--access-code FAKE42] [--control-port 18080]
                                                         [--out <dir>]

Needs a fake CC2 running on the default ports (py -3 -I tools\\remote-api\\fakecc2\\fakecc2.py; give it
--print-seconds 20 or more so the pause/stop steps happen mid-print) and the slicer's deps build
(deps\\build). The executable, the test G-code and the SDK log (elegoolink.log) go
to --out (default: a temp dir); nothing is written into the repositories.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", "..", "..", ".."))
DEPS_BUILD = os.path.join(REPO, "deps", "build")
DEPS = os.path.join(DEPS_BUILD, r"ElegooSlicer_dep\usr\local")
AGORA = os.path.join(DEPS_BUILD, r"dep_elegoolink-prefix\src\dep_elegoolink\thirdparty\agora\windows\lib\x86_64")
VCVARS = r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"

# elegoolink's exported link interface (lib\cmake\elegoolink\elegoolink_targets.cmake) and the
# Windows libraries its dependencies (Paho, ixwebsocket, curl, OpenSSL) need.
LIBS = ("elegoolink.lib paho-mqttpp3-static.lib paho-mqtt3as-static.lib ixwebsocket.lib libcurl.lib "
        "libssl.lib libcrypto.lib zlib.lib agora_rtm_sdk.dll.lib "
        "ws2_32.lib wsock32.lib crypt32.lib rpcrt4.lib shlwapi.lib bcrypt.lib advapi32.lib user32.lib "
        "iphlpapi.lib wldap32.lib normaliz.lib secur32.lib ole32.lib")


def write_gcode(path):
    """~2.5 MB of G-code with the header lines the fake reads (so the upload takes 3 chunks)."""
    with open(path, "w", newline="\n") as f:
        f.write("; total layer number: 30\n; estimated printing time (normal mode) = 25m 0s\n")
        for i in range(90000):
            f.write("G1 X%.3f Y%.3f E%.5f F6000\n" % (100 + (i % 500) * 0.1, 100 + (i % 300) * 0.1, i * 0.003))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--sn", default="FAKECC2SN0001")
    parser.add_argument("--access-code", default="FAKE42")
    parser.add_argument("--control-port", type=int, default=18080, help="the fake's control port (offline/online step)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="fakecc2_sdk_"))
    os.makedirs(out, exist_ok=True)
    exe = os.path.join(out, "sdk_check.exe")
    source = os.path.join(HERE, "sdk_check.cpp")
    cmd = os.path.join(out, "build.cmd")
    with open(cmd, "w") as f:
        f.write("@echo off\r\nset VSCMD_SKIP_SENDTELEMETRY=1\r\n"
                f'call "{VCVARS}" -vcvars_ver=14.44 >nul\r\n'
                f"cl /nologo /EHsc /MD /O1 /std:c++17 /DNOMINMAX /DWIN32_LEAN_AND_MEAN /DELEGOO_LINK_STATIC "
                f'/DCURL_STATICLIB /I"{DEPS}\\include" "{source}" /Fe"{exe}" /Fo"{out}\\\\" '
                f'/link /LIBPATH:"{DEPS}\\lib" /LIBPATH:"{AGORA}" {LIBS}\r\n')
    build = subprocess.run(["cmd.exe", "/c", cmd], capture_output=True, text=True, errors="replace", cwd=out)
    if build.returncode != 0 or not os.path.exists(exe):
        errors = [l for l in build.stdout.splitlines() if "error" in l.lower()]
        print("\n".join(errors[:60]) or build.stdout[-4000:])
        print("BUILD FAILED (exit %d)" % build.returncode)
        return 1
    print("built", exe, flush=True)
    for dll in ("agora_rtm_sdk.dll", "libaosl.dll"):  # imported by the static SDK (cloud RTM)
        shutil.copy2(os.path.join(AGORA, dll), out)
    gcode = os.path.join(out, "sdk_check.gcode")
    write_gcode(gcode)
    run = subprocess.run([exe, args.host, args.sn, args.access_code, gcode, out, str(args.control_port)],
                         cwd=out, timeout=300)
    print("SDK log:", os.path.join(out, "elegoolink.log"))
    return run.returncode


if __name__ == "__main__":
    sys.exit(main())
