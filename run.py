"""Launch Hexpad (tray app + config UI).

For silent background launch use start-ajazzdock.ps1 (pythonw, no console).
"""
import sys

_CRASH_LOG_FILE = None            # kept alive so faulthandler's fd stays open for the process life


def _enable_faulthandler() -> None:
    """Dump the Python stack of every thread on a NATIVE crash (access violation / segfault) to
    %APPDATA%\\AjazzDock\\crash.log, so a silent crash leaves a diagnosable trace instead of vanishing."""
    global _CRASH_LOG_FILE
    try:
        import os
        import faulthandler
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "AjazzDock")
        os.makedirs(base, exist_ok=True)
        _CRASH_LOG_FILE = open(os.path.join(base, "crash.log"), "a", buffering=1,
                               encoding="utf-8", errors="replace")
        faulthandler.enable(file=_CRASH_LOG_FILE, all_threads=True)
    except Exception:
        pass


if __name__ == "__main__":
    _enable_faulthandler()
    # Isolated sensor worker: `--lhm-worker <out_path> <parent_pid>` runs ONLY the
    # LibreHardwareMonitor/.NET sweep in this child process. If that native code crashes it takes
    # down the child (logged by faulthandler), not the app. Never touches Qt / the dock.
    if len(sys.argv) >= 2 and sys.argv[1] == "--lhm-worker":
        from dock import live
        out = sys.argv[2] if len(sys.argv) > 2 else ""
        ppid = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 0
        live.lhm_worker_main(out, ppid)
        sys.exit(0)

    from dock import config as _config
    _config.use_live_config()     # this process IS the app -> it may read/write the real config
    from dock.gui import main
    main()
