"""Regressions for the bug-hunt fixes: malformed configs are quarantined (not a boot-crash),
concurrent Config.save() from many threads never collides/corrupts, and a hand-edited non-dict
`action` doesn't abort the render path."""
import os, sys, json, threading, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dock.config import Config, default_config
from dock.images import render_face, resolve_swatch, resolve_auto_icon

fails = []
def ck(n, cond):
    print(("  ok " if cond else "FAIL ") + n)
    if not cond: fails.append(n)

tmp = tempfile.mkdtemp(prefix="hexpad_test_")
try:
    # ---- #4: parseable-but-malformed config must recover to defaults, not crash the boot ----
    for i, bad in enumerate(["null", "[]", '"x"', "5", '{"profiles": null}', '{"profiles": 3}']):
        p = os.path.join(tmp, f"bad{i}.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(bad)
        try:
            cfg = Config.load(p)
            ok = isinstance(cfg.data, dict) and "profiles" in cfg.data and isinstance(cfg.data["profiles"], list)
            quarantined = any(fn.startswith(f"bad{i}.json.corrupt") for fn in os.listdir(tmp))
            ck(f"malformed config {bad!r:>18} -> defaults + quarantined", ok and quarantined)
        except Exception as e:
            ck(f"malformed config {bad!r:>18} -> defaults + quarantined", False); print("     err:", repr(e))

    # ---- #5: concurrent saves from many threads -> no error, valid file, no leftover tmp ----
    p = os.path.join(tmp, "concurrent.json")
    c = Config(default_config())
    errs = []
    def saver():
        try:
            for _ in range(25):
                c.save(p)
        except Exception as e:
            errs.append(repr(e))
    threads = [threading.Thread(target=saver) for _ in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()
    ck("concurrent saves raised nothing", not errs or print("     errs:", errs[:3]))
    loaded = Config.load(p)
    ck("concurrent saves left a valid config file", isinstance(loaded.data, dict) and "profiles" in loaded.data)
    leftover = [f for f in os.listdir(tmp) if f.startswith("concurrent.json.") and f.endswith(".tmp")]
    ck("concurrent saves left no stray .tmp file", not leftover)

    # ---- #6: a non-dict action must not raise in the render path ----
    for bad_action in ("none", 5, [], None, "hotkey"):
        item = {"action": bad_action, "label": "x", "swatch_size": 70}
        try:
            resolve_swatch(item); resolve_auto_icon(item)
            render_face(item, (60, 60))
            ck(f"non-dict action {bad_action!r:>8} renders (no crash)", True)
        except Exception as e:
            ck(f"non-dict action {bad_action!r:>8} renders (no crash)", False); print("     err:", repr(e))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)
