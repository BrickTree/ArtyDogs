import sys
import traceback
from pathlib import Path

# From source this is the project folder; in a packaged build, the folder holding the .exe.
HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    if "--selftest" in sys.argv:
        from arty.selftest import run

        sys.exit(run())
    from arty.app import main

    main()
except SystemExit:
    raise
except Exception:
    # pythonw has no console, so leave the error somewhere visible.
    (HERE / "debug").mkdir(exist_ok=True)
    (HERE / "debug" / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, traceback.format_exc()[-1500:], "WARDOGS Arty crashed", 0x10)
    except Exception:
        pass
    raise
