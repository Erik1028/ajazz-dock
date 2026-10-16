"""Recover the AjazzDock config from the MSIX-sandbox copy (the richest surviving one).

Backs up the current real config, then writes the migrated sandbox config to the real
%APPDATA% path WITHOUT a BOM. Run with the NON-MSIX Python (Programs\\Python\\Python312),
which sees the real %APPDATA% (the venv MSIX python would hit the sandbox instead).
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dock.config import _migrate  # noqa: E402

real = os.path.join(os.environ["APPDATA"], "AjazzDock", "config.json")
sandbox = os.path.join(os.environ["LOCALAPPDATA"], "Packages", "PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0",
                       "LocalCache", "Roaming", "AjazzDock", "config.json")

if not os.path.exists(sandbox):
    print("SANDBOX MISSING:", sandbox)
    sys.exit(1)

# Preserve whatever is currently there (the reset-to-default file) before we replace it.
if os.path.exists(real):
    shutil.copyfile(real, real + ".pre-restore-bak")
    print("backed up current ->", real + ".pre-restore-bak")

with open(sandbox, "r", encoding="utf-8-sig") as f:
    d = json.load(f)

# Carry over the newer top-level flags the sandbox copy predates.
d.setdefault("press_fx", True)
d.setdefault("press_anim", "jump")
d = _migrate(d)

tmp = real + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:          # plain utf-8, NO BOM
    json.dump(d, f, indent=2, ensure_ascii=False)
os.replace(tmp, real)

prof = d["profiles"][0]
print("restored ->", real)
print("brightness:", d.get("brightness"), " pages:", len(prof["pages"]))
print("globals:", list(prof.get("globals", {}).keys()))
print("Home key1:", prof["pages"][0]["items"].get("key1"))
