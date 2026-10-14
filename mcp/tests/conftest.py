import lzma
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def gcode_fixture():
    """Return the decompressed text of tests/fixtures/<name>.gcode.xz (cached per session)."""
    cache: dict[str, str] = {}

    def load(name: str) -> str:
        if name not in cache:
            with lzma.open(FIXTURES / f"{name}.gcode.xz", "rt", encoding="utf-8", errors="replace") as fh:
                cache[name] = fh.read()
        return cache[name]
    return load


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path_factory):
    """No test may write into the real home folder (~/.orcaslicer-mcp/gcode, notes, ...): every
    test starts with Path.home() pointing at an empty temp folder. POSIX reads HOME, Windows reads
    USERPROFILE (HOME alone is ignored there, which once let save_gcode tests write into the real
    home), so both are set."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture(autouse=True)
def _isolate_outcome_store(monkeypatch, tmp_path_factory):
    """No test may touch a real outcome store: both fallback locations point at temp dirs.
    Tests that want a store set PRINT_OUTCOMES_DIR themselves."""
    from orcaslicer_mcp import outcomes
    base = tmp_path_factory.mktemp("outcomes")
    monkeypatch.setattr(outcomes, "LEGACY_DIR", base / "legacy-absent")
    monkeypatch.setattr(outcomes, "DEFAULT_DIR", base / "default")
    monkeypatch.delenv("PRINT_OUTCOMES_DIR", raising=False)


@pytest.fixture(autouse=True)
def _isolate_printer_state(monkeypatch, tmp_path_factory):
    """The printer tools remember the last printer on disk and cache probes in memory.
    No test may read or write the real file or inherit another test's cache or settings."""
    import importlib
    from orcaslicer_mcp.printer import target
    monkeypatch.setattr(target, "REMEMBERED_PATH", tmp_path_factory.mktemp("printer") / "printer.json")
    for name in ("ORCA_PRINTER_URL", "ORCA_PRINTER_API_KEY", "ORCA_PRINTER_ID"):
        monkeypatch.delenv(name, raising=False)
    try:
        importlib.import_module("orcaslicer_mcp.printer.target")._PROBE_CACHE.clear()
    except (ImportError, AttributeError):
        pass
