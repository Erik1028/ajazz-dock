"""Configuration model for the AKP03 controller.

The config is plain JSON so the UI can read/write it directly. A *binding* is keyed
by the device input id (matching dock.device.Event.input_id):

    LCD keys  : "key1".."key6"   (these also carry a visual face: label/icon/color)
    buttons   : "btn7".."btn9"
    encoders  : "enc0".."enc2"    (push)  and  "enc0+"/"enc0-" .. (twist cw/ccw)

A binding looks like:
    { "label": "Chrome", "icon": "C:/..png" or "🎵", "color": "#1e6fd0",
      "text_color": "#ffffff", "action": { "type": "...", ... } }

Faces (label/icon/color) only render on key1..key6; other ids use just "action".

Action types (see dock.actions): open, hotkey, text, media, volume, mic, page,
profile, brightness, macro.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import sys
import threading
import time
from typing import Any, Dict, List, Optional

_SAVE_LOCK = threading.Lock()   # serialize save() across the device-loop + Qt threads (shared tmp/replace)

APP_NAME = "AjazzDock"
LCD_KEYS = ["key1", "key2", "key3", "key4", "key5", "key6"]
BUTTONS = ["btn7", "btn8", "btn9"]
ENCODERS = ["enc0", "enc1", "enc2"]

# The firmware anchors each key image to the TOP-RIGHT, leaving a dark gap on the left/bottom.
# This paste-offset (negative x = left, positive y = down — measured on-device) nudges content
# down-left so it sits centred. Applied as the default + once to never-calibrated configs.
_DEFAULT_SHIFT = (-6, 8)


def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


# Only the real app may touch the user's config.json. Tests and one-off tools import dock.* and
# build Config(default_config()) objects whose save() would otherwise land on the live file and
# replace every page, folder and binding the user owns with a fresh default — that has happened,
# and a config is the one thing in this app the user cannot re-derive. The frozen exe is always
# the app; running from source, run.py opts in explicitly. Everything else gets a scratch file.
_LIVE_OK = bool(getattr(sys, "frozen", False))


def use_live_config() -> None:
    """Called by run.py: this process IS the app, so it may read/write the real config."""
    global _LIVE_OK
    _LIVE_OK = True


def live_config_enabled() -> bool:
    return _LIVE_OK


def config_path() -> str:
    override = os.environ.get("AJAZZDOCK_CONFIG")      # explicit opt-in for dev/testing
    if override:
        return override
    if _LIVE_OK:
        return os.path.join(config_dir(), "config.json")
    # Not the app -> a per-process scratch config. Reading it finds nothing (so tests start from
    # defaults, as they expect) and writing it cannot destroy anything.
    return os.path.join(tempfile.gettempdir(), "hexpad-scratch-config-%d.json" % os.getpid())


def _discord_token_path() -> str:
    return os.path.join(config_dir(), "discord_token.json")


def load_discord_token():
    """The Discord OAuth token lives in its OWN file (not the main config) so the RPC thread can
    persist a rotated refresh token with an atomic write, never touching config.json."""
    try:
        with open(_discord_token_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d.get("token", ""), d.get("refresh", "")
    except Exception:
        return "", ""


def save_discord_token(token: str, refresh: str) -> None:
    try:
        p = _discord_token_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"token": token or "", "refresh": refresh or ""}, f)
        os.replace(tmp, p)            # atomic; safe from any thread
    except Exception:
        pass


def backups_dir() -> str:
    """The restore-point folder. Only the app may use the real one: the history snapshots AND the
    40-slot prune (backups._prune / Config._write_history) both live here, so a test or script
    that saved a config used to add a junk snapshot to Erik's real restore points and, through the
    prune, delete his oldest real one. By 2026-10-11 24 of his 40 slots were scratch junk and a
    scratch run had written a config modified in memory there. Same rule as config_path()."""
    override = os.environ.get("AJAZZDOCK_CONFIG")
    if override:                                   # explicit dev/test config -> backups beside it
        base = os.path.dirname(os.path.abspath(override))
    elif _LIVE_OK:
        base = config_dir()
    else:                                          # not the app -> a throwaway per-process folder
        base = os.path.join(tempfile.gettempdir(), "hexpad-scratch-%d" % os.getpid())
    d = os.path.join(base, "backups")
    os.makedirs(d, exist_ok=True)
    return d


# A default config weighs 14 (2 pages + 12 items); a config with real work in it weighs hundreds.
# Anything under this is treated as "blank" for the purposes of the rescue snapshot.
_WIPE_FLOOR = 24


def config_weight(data: Dict[str, Any]) -> int:
    """How much user work a config holds: bound keys + pages + folders, across profiles/folders."""
    n = 0
    for pr in (data.get("profiles") or []):
        pages = pr.get("pages") or []
        n += len(pages)
        for pg in pages:
            n += len(pg.get("items") or {})
        folders = pr.get("folders") or {}
        n += len(folders)
        for f in folders.values():
            for pg in (f.get("pages") or [{"items": f.get("items") or {}}]):
                n += len(pg.get("items") or {})
    return n


def default_config() -> Dict[str, Any]:
    """A useful first-run config that demonstrates every feature, lean by default."""
    return {
        "version": 1,
        "brightness": 70,
        "show_labels": True,
        "press_fx": True,
        "press_anim": "bounce",
        "page_fx": True,
        "folder_anim": "zoom",
        # What exits a folder: a round button (btn9) keeps all 6 LCD keys free for content;
    # "key6" puts the built-in Back tile on the 6th key. MUST match _migrate's default,
    # or a fresh install behaves differently before and after its first restart.
    "folder_back": "btn9",
    "live_style": "gauge",
        "encoder_accel": True,                      # fast knob spin -> bigger steps
        # Ambient/idle screen: after `delay` s with no dock input, dim to `dim`% and show a
        # grid-spanning clock; any key/knob press wakes it (and is swallowed, not fired).
        "idle": {"enabled": True, "delay": 120, "dim": 25},
        # App-aware auto-switching: when on, the foreground app picks a profile/page.
        # Each rule: {"app": "obs64.exe", "profile": <name|None>, "page": <index|None>}.
        "auto_switch": False,
        "app_rules": [],
        # TP-Link account for direct Tapo bulb control (stored locally; auto-imported from Lumos).
        "tapo": {"email": "", "password": ""},
        # Discord application for voice control over RPC (the OAuth token lives in a separate file).
        "discord": {"client_id": "", "client_secret": ""},
        # Per-key image geometry (the firmware anchors images top-right with a fixed margin;
        # these are tuned in the in-app display calibrator). w/h = tile px per key (the
        # physical cell is taller than wide, so width and height are independent).
        "display": {"w": 88, "h": 88, "dx": _DEFAULT_SHIFT[0], "dy": _DEFAULT_SHIFT[1],
                    "inset": 2, "cal_v": 1},
        "active_profile": "Default",
        "profiles": [
            {
                "name": "Default",
                # Encoders + round buttons are GLOBAL (same on every page of this profile).
                "globals": {
                    "enc0+": {"action": {"type": "volume", "volume": "up"}},
                    "enc0-": {"action": {"type": "volume", "volume": "down"}},
                    "enc0": {"action": {"type": "volume", "volume": "mute"}},
                    "btn7": {"action": {"type": "page", "page": "prev"}},
                    "btn8": {"action": {"type": "page", "page": "next"}},
                    "btn9": {"action": {"type": "mic", "mic": "toggle"}},
                },
                "pages": [
                    {
                        "name": "Home",
                        "items": {
                            "key1": {"label": "Notepad", "icon": "📝", "color": "#2d6cdf",
                                     "action": {"type": "open", "target": "notepad.exe"}},
                            "key2": {"label": "Browser", "icon": "🌐", "color": "#1aa179",
                                     "action": {"type": "open", "target": "https://www.google.com"}},
                            "key3": {"label": "Files", "icon": "📁", "color": "#c8881f",
                                     "action": {"type": "hotkey", "keys": "win+e"}},
                            "key4": {"label": "Play", "icon": "⏯️", "color": "#7a3cc4",
                                     "action": {"type": "media", "media": "play_pause"}},
                            "key5": {"label": "Mic", "icon": "🎙️", "color": "#c0392b",
                                     "action": {"type": "mic", "mic": "toggle"}},
                            "key6": {"label": "Page →", "icon": "➡️", "color": "#444b55",
                                     "action": {"type": "page", "page": "next"}},
                        },
                    },
                    {
                        "name": "Media",
                        "items": {
                            "key1": {"label": "Prev", "icon": "⏮️", "color": "#34495e",
                                     "action": {"type": "media", "media": "prev"}},
                            "key2": {"label": "Play", "icon": "⏯️", "color": "#7a3cc4",
                                     "action": {"type": "media", "media": "play_pause"}},
                            "key3": {"label": "Next", "icon": "⏭️", "color": "#34495e",
                                     "action": {"type": "media", "media": "next"}},
                            "key4": {"label": "Vol -", "icon": "🔉", "color": "#27632a",
                                     "action": {"type": "volume", "volume": "down"}},
                            "key5": {"label": "Vol +", "icon": "🔊", "color": "#1aa179",
                                     "action": {"type": "volume", "volume": "up"}},
                            "key6": {"label": "Home", "icon": "🏠", "color": "#444b55",
                                     "action": {"type": "page", "page": "prev"}},
                        },
                    },
                ],
            }
        ],
    }


class Config:
    """Thin wrapper around the config dict with convenient accessors."""

    def __init__(self, data: Dict[str, Any]):
        self.data = data

    # ---- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        path = path or config_path()
        if os.path.exists(path):
            try:
                # utf-8-sig tolerates a leading BOM (some editors / PowerShell add one)
                # which plain json.load would choke on.
                with open(path, "r", encoding="utf-8-sig") as f:
                    return cls(_migrate(json.load(f)))
            except (json.JSONDecodeError, OSError, ValueError, TypeError, AttributeError, KeyError):
                # NEVER silently overwrite a config we can't parse — set it aside so the
                # user's bindings can be recovered instead of being clobbered by defaults.
                for suffix in (".corrupt", ".corrupt1", ".corrupt2"):
                    if not os.path.exists(path + suffix):
                        try:
                            os.replace(path, path + suffix)
                        except OSError:
                            pass
                        break
        cfg = cls(default_config())
        cfg.save(path)
        return cfg

    def save(self, path: Optional[str] = None) -> None:
        path = path or config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Serialize the WHOLE save under one process-wide lock: the device-loop thread (encoder
        # brightness/volume save) and the Qt thread (editor autosave) must never do concurrent file
        # ops on `path` — on Windows a .bak read racing an os.replace fails with PermissionError.
        with _SAVE_LOCK:
            try:                                          # rolling backup of the last good config
                if os.path.exists(path):
                    shutil.copyfile(path, path + ".bak")
                    self._rescue_if_wiping(path)
            except OSError:
                pass
            tmp = f"{path}.{os.getpid()}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, ensure_ascii=False)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):                    # never leave a stray tmp on a failed replace
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            try:                                          # timestamped history for recoverability
                self._write_history(path)
            except OSError:
                pass

    def _rescue_if_wiping(self, path: str) -> None:
        """About to write a config that looks EMPTY over one that is full? Keep the old one first.

        The rolling `config-*.json` history is a 40-slot ring, so a process that repeatedly saves a
        blank config can flush every good snapshot out of it. A `rescue-*.json` copy is never
        pruned and never overwritten, so the last good layout survives even a sustained wipe.
        Cheap by construction: the existing file is only parsed when what we are about to write is
        already suspiciously small, which never happens on a normal save."""
        if config_weight(self.data) >= _WIPE_FLOOR:
            return                                    # writing a real config -> nothing at risk
        try:
            with open(path, encoding="utf-8-sig") as f:
                old = json.load(f)
        except (OSError, ValueError):
            return
        if config_weight(old) < max(_WIPE_FLOOR, config_weight(self.data) * 2):
            return                                    # the old one was small too -> not a wipe
        d = backups_dir()
        keep = [f for f in os.listdir(d) if f.startswith("rescue-") and f.endswith(".json")]
        if len(keep) >= 30:
            return                    # already holding plenty; the OLDEST are the valuable ones
        dst = os.path.join(d, "rescue-%s.json"
                           % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        if not os.path.exists(dst):
            shutil.copyfile(path, dst)

    _last_snapshot_t = None      # in-memory throttle: skip the backups dir scan on the common path

    @classmethod
    def _write_history(cls, path: str) -> None:
        now = time.time()
        # Common path (a save within 5 min of the last snapshot): a cheap timestamp compare —
        # no os.makedirs / os.listdir / sort. Only the once-per-5-min path touches the disk.
        if cls._last_snapshot_t is not None and now - cls._last_snapshot_t < 300:
            return
        d = backups_dir()
        snaps = sorted(f for f in os.listdir(d)
                       if f.startswith("config-") and f.endswith(".json"))
        if snaps:
            newest = os.path.getmtime(os.path.join(d, snaps[-1]))
            if now - newest < 300:                  # at most one snapshot / 5 min
                cls._last_snapshot_t = newest
                return
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copyfile(path, os.path.join(d, f"config-{stamp}.json"))
        cls._last_snapshot_t = now
        snaps = sorted(f for f in os.listdir(d)
                       if f.startswith("config-") and f.endswith(".json"))
        for old in snaps[:-40]:                      # keep the most recent 40
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass

    # ---- accessors ---------------------------------------------------------
    @property
    def brightness(self) -> int:
        return int(self.data.get("brightness", 70))

    @brightness.setter
    def brightness(self, v: int) -> None:
        self.data["brightness"] = max(0, min(100, int(v)))

    def display(self) -> Dict[str, Any]:
        """Per-key image geometry calibration: {w, h, dx, dy} (old configs may use `size`)."""
        d = self.data.setdefault("display", {})
        base = int(d.get("size", 88))            # back-compat: old single-axis "size"
        d.setdefault("w", base)
        d.setdefault("h", base)
        d.setdefault("dx", 0)
        d.setdefault("dy", 0)
        d.setdefault("inset", 2)                  # black frame px (0 = content reaches the edge)
        return d

    @property
    def profiles(self) -> List[Dict[str, Any]]:
        return self.data.setdefault("profiles", [])

    def profile_names(self) -> List[str]:
        return [p.get("name", f"Profile {i}") for i, p in enumerate(self.profiles)]

    def active_profile(self) -> Dict[str, Any]:
        name = self.data.get("active_profile")
        for p in self.profiles:
            if p.get("name") == name:
                return p
        # fall back to first
        if self.profiles:
            self.data["active_profile"] = self.profiles[0].get("name")
            return self.profiles[0]
        # empty -> seed a default
        self.data.update(default_config())
        return self.profiles[0]

    def set_active_profile(self, name: str) -> bool:
        if name in self.profile_names():
            self.data["active_profile"] = name
            return True
        return False

    def pages(self, profile: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        profile = profile or self.active_profile()
        return profile.setdefault("pages", [{"name": "Home", "items": {}}])

    def page(self, index: int, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        pages = self.pages(profile)
        if not pages:
            pages.append({"name": "Home", "items": {}})
        return pages[index % len(pages)]

    def globals_of(self, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Profile-wide bindings (encoders + round buttons), shared across all pages."""
        profile = profile or self.active_profile()
        return profile.setdefault("globals", {})

    def folders_of(self, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Sub-pages opened by a key's 'folder' action: {folder_id: {name, items}}."""
        profile = profile or self.active_profile()
        return profile.setdefault("folders", {})

    def folder(self, fid: str, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        f = self.folders_of(profile).setdefault(fid, {"name": "Folder", "pages": [{"items": {}}]})
        return self._norm_folder(f)

    @staticmethod
    def _norm_folder(f: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure a folder is multi-page: migrate a legacy single-screen `items` into pages[0],
        and guarantee at least one page (each with an `items` dict). In-place + idempotent."""
        if "pages" not in f:
            f["pages"] = [{"items": f.pop("items", {}) or {}}]
        if not f["pages"]:
            f["pages"] = [{"items": {}}]
        for pg in f["pages"]:
            pg.setdefault("items", {})
        f.pop("items", None)                        # legacy field fully superseded by pages
        return f

    def folder_pages(self, fid: str, profile: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return self.folder(fid, profile)["pages"]

    def folder_items(self, fid: str, index: int = 0,
                     profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        pages = self.folder_pages(fid, profile)
        return pages[index % len(pages)].setdefault("items", {})


def _migrate(data: Dict[str, Any]) -> Dict[str, Any]:
    """Forward-compatible defaults for older/partial config files."""
    if not isinstance(data, dict):                       # parseable JSON but not our shape (null/list/
        raise ValueError("config root is not an object")  # str/number) -> quarantine, don't crash boot
    data.setdefault("version", 1)
    data.setdefault("brightness", 70)
    data.setdefault("show_labels", True)
    data.setdefault("press_fx", True)
    data.setdefault("press_anim", "bounce")
    data.setdefault("page_fx", True)
    data.setdefault("folder_anim", "zoom")
    # What exits a folder: a round button (default btn9) keeps all 6 LCD keys free for content; set to
    # "key6" to bring back the built-in Back tile on the 6th key. (btn7/btn8 still flip folder pages.)
    data.setdefault("folder_back", "btn9")
    data.setdefault("live_style", "gauge")
    data.setdefault("auto_icons", True)          # crisp Fluent icon for iconless action keys
    data.setdefault("encoder_accel", True)
    idle = data.setdefault("idle", {})
    idle.setdefault("enabled", True)
    idle.setdefault("delay", 120)
    idle.setdefault("dim", 25)
    idle.setdefault("style", "classic")          # ambient design (Settings ▸ Display ▸ Idle)
    idle.setdefault("playing", True)             # dynamic: show the cover while music plays
    idle.setdefault("weather", False)            # dynamic: rotate the clock with a weather screen
    data.setdefault("auto_switch", False)
    data.setdefault("app_rules", [])
    data.setdefault("tapo", {"email": "", "password": ""})
    data.setdefault("discord", {"client_id": "", "client_secret": ""})
    disp = data.setdefault("display", {})
    base = int(disp.get("size", 88))             # old configs only had a single "size"
    disp.setdefault("w", base)
    disp.setdefault("h", base)
    disp.setdefault("dx", 0)
    disp.setdefault("dy", 0)
    disp.setdefault("inset", 2)
    # One-time: nudge content down-left to close the firmware's top-right "black gap". Only for
    # never-calibrated configs (dx/dy still 0); the cal_v marker stops it re-applying, so a user
    # who later zeroes the nudge by hand keeps their choice.
    if not disp.get("cal_v"):
        if int(disp.get("dx", 0)) == 0 and int(disp.get("dy", 0)) == 0:
            disp["dx"], disp["dy"] = _DEFAULT_SHIFT
        disp["cal_v"] = 1
    data.setdefault("profiles", default_config()["profiles"])
    if "active_profile" not in data and data["profiles"]:
        data["active_profile"] = data["profiles"][0].get("name", "Default")
    for p in data["profiles"]:
        p.setdefault("name", "Profile")
        g = p.setdefault("globals", {})
        for _f in p.setdefault("folders", {}).values():
            Config._norm_folder(_f)                  # legacy folder.items -> folder.pages[0].items
        p.setdefault("pages", [{"name": "Home", "items": {}}])
        for pg in p["pages"]:
            pg.setdefault("name", "Page")
            items = pg.setdefault("items", {})
            # Round buttons stay profile-global -> lift any out of per-page items (first wins).
            # Encoders are now PER-PAGE: they live in page items; globals[enc*] is the shared
            # default a page falls back to until it overrides the knob.
            for sid in [s for s in items if s.startswith("btn")]:
                g.setdefault(sid, items.pop(sid))
    return data
