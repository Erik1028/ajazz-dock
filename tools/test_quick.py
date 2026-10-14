"""Verify the 'quick' action type routes to the right implementation — with all the
real side effects (emptying the bin, sending keys, launching) stubbed out."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dock.actions as A                                  # noqa: E402

fails = []
rec = []


def check(name, cond):
    print(("  ok " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# stub every real side effect. `keyboard` is imported LAZILY inside _kb_send (so the module can
# load without it), so the stub goes on the wrapper, not on a module-level `keyboard` attribute.
A._kb_send = lambda k: rec.append(("send", k))
A.subprocess.Popen = lambda args, **kw: rec.append(("popen", list(args)))
A.os.startfile = lambda t: rec.append(("startfile", t))
A.ActionEngine._empty_recycle_bin = staticmethod(lambda: rec.append(("recycle_empty",)))
A.ActionEngine._clear_clipboard = staticmethod(lambda: rec.append(("clip_clear",)))

eng = A.ActionEngine()
eng._system = lambda what: rec.append(("system", what))


def run(op):
    """Dispatch directly. execute() now routes blocking keyboard/system actions to a worker
    thread (see _INLINE), so going through it would race this test instead of testing routing."""
    rec.clear()
    eng._quick(op)
    return list(rec)


check("recycle_empty -> SHEmptyRecycleBin", run("recycle_empty") == [("recycle_empty",)])
check("recycle_open -> explorer shell:RecycleBinFolder",
      run("recycle_open") == [("popen", ["explorer.exe", "shell:RecycleBinFolder"])])
check("clipboard_clear -> EmptyClipboard", run("clipboard_clear") == [("clip_clear",)])
check("settings -> ms-settings:", run("settings") == [("startfile", "ms-settings:")])
check("lock -> system lock", run("lock") == [("system", "lock")])
check("show_desktop -> win+d", run("show_desktop") == [("send", "windows+d")])
check("task_manager -> ctrl+shift+esc", run("task_manager") == [("send", "ctrl+shift+esc")])
check("snip -> win+shift+s", run("snip") == [("send", "windows+shift+s")])
check("project -> win+p", run("project") == [("send", "windows+p")])
check("unknown op is a no-op (no crash)", run("does_not_exist") == [])

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)
