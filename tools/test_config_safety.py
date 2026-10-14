r"""The user's config.json must be unreachable from anything that is not the app.

This exists because it went wrong: a test-suite run built Config(default_config()) objects whose
save() landed on the LIVE %APPDATA%\AjazzDock\config.json and replaced 10 pages, 12 folders and 196
bound keys with a fresh default. A config is the one thing here the user cannot re-derive, so there
are two independent layers now: the path is sandboxed unless the process declares itself the app,
and a save that would collapse a full config leaves an un-prunable rescue copy first.
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dock import config as C                                             # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (("  " + extra) if extra else ""))
    if not cond:
        fails.append(name)


def _live_paths():
    r"""Every location the user's real config can live at.

    This venv's Python is MSIX-packaged, so its READS of %APPDATA%\AjazzDock\config.json are
    redirected to a per-package shadow copy, while save()'s NEW .tmp file lands in the REAL folder
    and os.replace() then overwrites the real config. That split is exactly why the damage stayed
    invisible from inside Python. Sizes read here may therefore be the shadow's; the guarantee that
    actually holds is the assertion above that config_path() points into %TEMP% at all.
    """
    seen, out = set(), []
    cands = [os.path.join(C.config_dir(), "config.json"),
             os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "AjazzDock", "config.json")]
    for p in cands:
        for q in (os.path.abspath(p), os.path.realpath(p)):
            if q.lower() not in seen:
                seen.add(q.lower())
                out.append(q)
    return out


LIVE_ALL = _live_paths()
LIVE = LIVE_ALL[0]

# ---- 1. an ordinary import (a test, a tool) must never resolve to the live file -----------------
os.environ.pop("AJAZZDOCK_CONFIG", None)
check("this process is not marked as the app", C.live_config_enabled() is False)
scratch = C.config_path()          # never reuse this name below: os.remove() runs on it
check("config_path() is NOT the user's config",
      os.path.abspath(scratch) not in [q.lower() and q for q in LIVE_ALL], "got %s" % scratch)
check("it is a scratch file in the temp dir",
      os.path.abspath(scratch).startswith(os.path.abspath(tempfile.gettempdir())), scratch)

# the destructive move that caused the incident: saving a fresh default config
def _sizes():
    return {p: (os.path.getsize(p) if os.path.exists(p) else None) for p in LIVE_ALL}


before = _sizes()
C.Config(C.default_config()).save()
after = _sizes()
changed = [p for p in LIVE_ALL if before[p] != after[p]]
check("saving a default config touched NO live config (real or MSIX-shadow)", not changed,
      "checked %d path(s); changed: %s" % (len(LIVE_ALL), changed or "none"))
for live in LIVE_ALL:
    if before[live]:
        print("       | intact: %s (%d bytes)" % (live, before[live]))
if os.path.abspath(scratch).startswith(os.path.abspath(tempfile.gettempdir())):
    try:                               # tidy the scratch file — guarded, never a live path
        os.remove(scratch)
    except OSError:
        pass

# ---- 1b. ...and must not touch the user's restore points either ----------------------------------
# A scratch save also writes a history snapshot, and the 40-slot ring prunes the oldest - so a
# test that saved used to add junk to the REAL backups folder and delete a real restore point.
REAL_BACKUPS = os.path.join(C.config_dir(), "backups")
check("backups_dir() is NOT the user's restore-point folder",
      os.path.abspath(C.backups_dir()).lower() != os.path.abspath(REAL_BACKUPS).lower(),
      C.backups_dir())


def _listing(d):
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


real_before = _listing(REAL_BACKUPS)
C.Config._last_snapshot_t = None                     # force a history snapshot on this save
C.Config(C.default_config()).save()
from dock import backups as _B                       # the explicit snapshot + prune path too
_B.snapshot("scratchtest")
real_after = _listing(REAL_BACKUPS)
check("a scratch save + snapshot left the real restore points untouched",
      real_before == real_after,
      "%d files before, %d after%s" % (len(real_before), len(real_after),
      "" if real_before == real_after else "; new: %s gone: %s" % (
          sorted(set(real_after) - set(real_before)), sorted(set(real_before) - set(real_after)))))
try:
    shutil.rmtree(os.path.dirname(C.backups_dir()), ignore_errors=True)
    if os.path.exists(scratch):
        os.remove(scratch)
except OSError:
    pass

# ---- 2. the app still gets the real path --------------------------------------------------------
C.use_live_config()
check("use_live_config() restores the real path",
      os.path.abspath(C.config_path()) == os.path.abspath(LIVE))
C._LIVE_OK = False                                       # back to sandbox for the rest of the run
check("the frozen exe is the app by construction",
      "getattr(sys, \"frozen\", False)" in open(C.__file__, encoding="utf-8").read())
check("run.py opts in before starting the UI",
      "use_live_config()" in open(os.path.join(os.path.dirname(C.__file__), "..", "run.py"),
                                  encoding="utf-8").read())

# ---- 3. a wipe leaves an un-prunable rescue copy -------------------------------------------------
tmp = tempfile.mkdtemp(prefix="hexpad-cfgtest-")
os.environ["AJAZZDOCK_CONFIG"] = os.path.join(tmp, "config.json")
old_dir, C.config_dir = C.config_dir, lambda: tmp
try:
    full = C.default_config()
    prof = full["profiles"][0]
    prof["pages"] = [{"name": "p%d" % i, "items": {("key%d" % k): {"label": "x"} for k in range(1, 7)}}
                     for i in range(8)]
    prof["folders"] = {("f%d" % i): {"name": "F", "items": {"key1": {"label": "a"}}} for i in range(9)}
    check("a real config weighs far more than a default",
          C.config_weight(full) > 50 and C.config_weight(C.default_config()) < 24,
          "full=%d default=%d" % (C.config_weight(full), C.config_weight(C.default_config())))

    C.Config(full).save()                                # the user's config exists
    C.Config._last_snapshot_t = None
    C.Config(C.default_config()).save()                  # ...and something blanks it

    bdir = C.backups_dir()
    rescues = [f for f in os.listdir(bdir) if f.startswith("rescue-")]
    check("the wipe left a rescue copy", len(rescues) == 1, str(os.listdir(bdir)))
    if rescues:
        saved = json.load(open(os.path.join(bdir, rescues[0]), encoding="utf-8-sig"))
        check("the rescue holds the FULL config, not the blank one",
              C.config_weight(saved) == C.config_weight(full),
              "weight %d" % C.config_weight(saved))

    # 40 further blank saves must not be able to prune it away
    for i in range(45):
        C.Config._last_snapshot_t = None
        C.Config(C.default_config()).save()
    still = [f for f in os.listdir(bdir) if f.startswith("rescue-")]
    check("sustained blank saves cannot prune the rescue", len(still) >= 1,
          "%d rescue / %d rolling" % (len(still),
                                      len([f for f in os.listdir(bdir) if f.startswith("config-")])))
    check("the rolling history is still capped",
          len([f for f in os.listdir(bdir) if f.startswith("config-")]) <= 41)

    # a normal full save must NOT spam rescue copies
    n0 = len([f for f in os.listdir(bdir) if f.startswith("rescue-")])
    for i in range(3):
        C.Config._last_snapshot_t = None
        C.Config(full).save()
    check("saving a real config writes no rescue",
          len([f for f in os.listdir(bdir) if f.startswith("rescue-")]) == n0)
finally:
    C.config_dir = old_dir
    os.environ.pop("AJAZZDOCK_CONFIG", None)
    shutil.rmtree(tmp, ignore_errors=True)

print("\nRESULT: " + ("ALL PASS" if not fails else "%d FAIL: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
