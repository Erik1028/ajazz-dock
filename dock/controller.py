"""DockController: bind device input events to actions and render pages."""
from __future__ import annotations

import collections
import json
import math
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .actions import ActionEngine
from .config import LCD_KEYS, Config
from .device import AKP03, Event
import os

from . import live
from . import images as _images
from .apppoller import ForegroundPoller
from .images import (render_face, press_frames, page_swipe_frames, folder_face,
                     folder_transition_frames, expand_gen, live_face, panel_frames, timer_face,
                     volume_hud_tiles, value_hud_tiles, ambient_clock_tiles, media_overflows,
                     ambient_weather_tiles, ambient_now_playing_tiles, now_playing_sig,
                     AMBIENT_ANIMATED, AMBIENT_STYLE_ORDER)

_AMBIENT_WX_CYCLE = 14.0      # dynamic idle: seconds showing the clock, then the weather, repeat

# Face shown on the last key while a folder sub-page is open.
_BACK_FACE = {"label": "Back", "icon": "⬅️", "color": "#222a33"}

# Lighter JPEG for in-motion animation frames (smaller -> faster transfer -> higher fps).
# The final settle frame is re-rendered at full quality.
_ANIM_Q = 72
_ANIM_SS = 2
_MARQUEE_SPEED = 70        # now-playing title scroll speed (supersampled px/sec) — slow + readable
_MARQUEE_DT = 0.05         # ~20 fps refresh while a title scrolls
# Encoder acceleration: turning a knob fast multiplies its step (so volume/brightness/scrub fly),
# turning slowly stays 1:1 (fine control). A pause longer than the window resets to 1.
_ENC_ACCEL_WINDOW = 0.09   # s; multiplier ~= window / gap-between-turns
_ENC_ACCEL_MAX = 6         # cap so a flick can't overshoot wildly
_ENC_ACCEL_DEFAULT = {"volume": 1, "brightness": 10, "monitor": 5, "appvolume": 5}
# Multi-gesture keys: tap fires instantly on press UNLESS a double/hold is configured, in which
# case we wait to disambiguate. A simple tap-only key keeps zero latency.
_HOLD_T = 0.5              # s a key must be held to fire its 'hold' action
_DOUBLE_T = 0.28          # s window to catch a second tap (double-tap)
# Mirror sliders (Orbit's VR panel): the turn kinds Hexpad can both read and set absolutely.
_ABS_KINDS = ("brightness", "volume", "monitor")
_JOG_MAX = 10              # detents one jog message may fire
_BRIGHT_SAVE_IDLE = 1.0    # s a slider-set dock brightness rests before it is saved (no 'final' seen)


def _log_error(where: str, exc: BaseException) -> None:
    """Append a traceback to %APPDATA%/AjazzDock/error.log (best-effort, never raises)."""
    try:
        import datetime
        import traceback
        from .config import config_dir
        with open(f"{config_dir()}\\error.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {where}\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


def _log_debug(msg: str) -> None:
    """Append a one-line trace to %APPDATA%/AjazzDock/calib.log (best-effort)."""
    try:
        import datetime
        from .config import config_dir
        with open(f"{config_dir()}\\calib.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%H:%M:%S}] {msg}\n")
    except Exception:
        pass


def remote_event(input_id: str, down: Optional[bool], turn: int) -> Optional[Event]:
    """'key3' + down -> Event('key', 2, ...), 'btn7' -> button 6, 'enc1' + turn -> encoder_turn 1.
    Marked remote: a remote turn is never accelerated (see _handle_event)."""
    try:
        if input_id.startswith("key") and down is not None:
            i = int(input_id[3:]) - 1
            if 0 <= i < len(LCD_KEYS):
                return Event("key", i, bool(down), remote=True)
        elif input_id.startswith("btn") and down is not None:
            i = int(input_id[3:]) - 1
            if 6 <= i <= 8:
                return Event("button", i, bool(down), remote=True)
        elif input_id.startswith("enc"):
            i = int(input_id[3:])
            if 0 <= i <= 2:
                if turn:
                    return Event("encoder_turn", i, delta=1 if turn > 0 else -1, remote=True)
                if down is not None:
                    return Event("encoder_push", i, bool(down), remote=True)
    except (ValueError, TypeError):
        pass
    return None


class DockController:
    def __init__(self, config: Optional[Config] = None,
                 on_status: Optional[Callable[[], None]] = None) -> None:
        self.config = config or Config.load()
        self.dock = AKP03()
        self.engine = ActionEngine(self)
        self.on_status = on_status            # called when connection/page/profile changes

        self.page_index = 0
        self._folder: Optional[str] = None    # id of the open folder sub-page, or None
        self._folder_page = 0                 # current page index WITHIN the open folder
        self._toggle_idx: Dict[str, int] = {}  # toggle-action keys -> current state index (in-memory)
        self._timers: Dict[str, dict] = {}     # timer-action keys -> countdown/stopwatch state (in-memory)
        self._last_key_index = 0              # last LCD key pressed (for the 'expand' folder anim)
        self._folder_src = 0                  # key the open folder grew from
        self._display_on = True               # dock screen on/off (long-press the middle button)
        self._pending_display = None          # tray/gesture toggle -> applied on the loop thread (HID writes)
        self._press = {}                      # input_id -> held-press state (gesture detection)
        self._tap_pending = {}                # input_id -> a tap awaiting a possible double-tap
        self.connected = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # cross-thread requests, all serviced inside the event loop thread:
        self._render_requested = True
        self._reload_requested = False
        self._pending_brightness: Optional[int] = None
        self._anim = None                     # dict: index, page, frames, start, dur, last
        self._page_anim = None                # dict: frames(list of 6-face lists), start, dur, last
        self._pending_page = None             # (direction, new_index) requested from any thread
        self._pending_switch = None           # (profile|None, page|None) from the app poller
        self._last_manual_nav = 0.0           # time of the last hand-driven page change
        self._poller = ForegroundPoller(self)  # app-aware auto-switching
        self._calib = None                    # (w, h, dx, dy) live display-calibration preview
        self._calib_dirty = False
        self._calib_active = False            # True only while the calibration dialog is open
        self._panel = None                    # animated full-panel wallpaper state (or None)
        self._volume_hud = None               # transient knob-HUD session (dict, see show_value_hud) or None
        self._hud_fx = None                   # hud_fx.KnobHud (lazy: numpy loads on the FIRST HUD only)
        self._hud_lock = threading.Lock()     # show_value_hud runs on action worker threads
        self._hud_keys = None                 # loop-owned: the HUD tiles on the keys right now, or None
        self._hud_shown = -1e9                # last time a HUD frame was on the keys (the 'fresh' gate)
        self._hud_clock = time.time           # the HUD session clock (a test drives a fake one)
        self._last_input = time.time()        # last dock input (for the idle/ambient timer)
        self._ambient = None                  # ambient/idle screen state (or None when awake)
        # Orbit's VR panel (dock/mirror.py): its presses are queued here and handled on the loop
        # thread like the dock's own (HID writes are single-threaded); a wake request leaves the idle
        # screen without firing anything; on_input echoes the dock's PHYSICAL inputs (loop thread).
        self._remote = collections.deque()
        self._wake_requested = False
        self.mirror_clients = 0               # connected panels -> the loop polls faster while > 0
        self.on_input: Optional[Callable[[Event], None]] = None
        # Its knob sliders: encoder index -> (kind, value 0..1, final), latest-wins, applied on the
        # loop (_apply_set). A slider-set dock brightness is saved on 'final' (or after 1 s of rest).
        self._remote_set: Dict[int, tuple] = {}
        self._set_lock = threading.Lock()
        self._bright_save_at = 0.0
        # Sharp faces: a client asked for 2x copies -> requested from any thread, applied on the loop
        # (images' flag is thread-local to the loop; the tap encodes there).
        self._mirror_hi = False
        self._mirror_hi_req = False
        self._retries = 0                     # consecutive failed reconnect scans (backoff)
        self._has_live = False                # current page has at least one live/dynamic key
        self._live_last = 0.0                 # last live-key refresh timestamp
        self._live_sig = {}                   # per-key last-pushed live signature (skip no-op redraws)
        self._marquee = {}                    # key index -> {val, start} for a scrolling media title
        self._marquee_last = 0.0
        self._enc_last = {}                   # encoder index -> last turn time (for acceleration)
        live.prime()                          # warm up the non-blocking CPU sampler
        try:                                  # let the bulb-state live key reach the Tapo bulb
            from .tapo import tapo_creds
            _e, _p = tapo_creds(self.config)
            live.set_tapo_creds(_e, _p)
        except Exception:
            pass
        live.set_weather_units(self.config.data.get("weather_units", "c"))   # °C / °F
        live.set_weather_location(self.config.data.get("weather", ""))   # city / "lat,lon" / "" = IP
        try:                                  # wire the Discord app + its saved OAuth token
            from . import discord
            from .config import load_discord_token, save_discord_token
            dc = self.config.data.get("discord", {})
            tok, ref = load_discord_token()
            discord.configure(dc.get("client_id", ""), dc.get("client_secret", ""),
                              token=tok, refresh=ref, on_token=save_discord_token)
        except Exception:
            pass
        try:                                  # wire OBS so its live-status keys can poll the WebSocket
            from . import obs
            o = self.config.data.get("obs", {})
            obs.configure(o.get("host"), o.get("port"), o.get("password"))
        except Exception:
            pass
        self._apply_geometry()                # push config display-calibration into the render modules

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="dock-loop", daemon=True)
        self._thread.start()
        self._poller.start()
        threading.Thread(target=self._monitors_startup, name="monitor-restore", daemon=True).start()

    def _monitors_startup(self) -> None:
        self._restore_monitors()
        try:                                  # a monitors off/on key exists -> read the DDC caps now,
            if '"monpower"' in json.dumps(self.config.data):   # so its first press is quick
                from . import monitors
                monitors.warm_up()
        except Exception:
            pass

    @staticmethod
    def _restore_monitors() -> None:
        """Monitors left switched off by the 'monpower' key (app quit / crash / reboot) -> back on."""
        try:
            from . import monitors
            done = monitors.restore_if_left_off()
            if done:
                print(f"[monpower] restored: {done}")
        except Exception as e:
            print(f"[monpower] restore failed: {e}")

    def stop(self) -> None:
        self._running = False
        self._poller.stop()
        # release UDP 9001 for other OSC apps — but only if the VRChat bridge was ever used
        _vrc = sys.modules.get(__package__ + ".vrcosc")
        if _vrc is not None:
            try:
                _vrc.stop()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        _mon = sys.modules.get(__package__ + ".monitors")
        if _mon is not None:                        # never quit leaving the PC monitors dark, nor
            try:                                    # with a DDC call still in flight
                done = _mon.shutdown()
                if done:
                    print(f"[monpower] on quit: {done}")
            except Exception as e:
                print(f"[monpower] quit restore failed: {e}")
        try:
            if self.connected:
                self.dock.clear_all()
                self.dock.flush()
        except Exception:
            pass
        self.dock.close()

    # ---- display power (long-press the middle button) ----------------------
    @staticmethod
    def _nonempty_action(a) -> bool:
        return isinstance(a, dict) and (a.get("type") or "none") not in ("none", "", None)

    @staticmethod
    def _gesture_action(binding, slot):
        """The action for a gesture slot: 'tap' -> action, 'double' -> action_double, 'hold' -> action_hold."""
        if not binding:
            return None
        return binding.get({"tap": "action", "double": "action_double", "hold": "action_hold"}[slot])

    def _has_gestures(self, input_id: str, binding) -> bool:
        """True if this input needs press/release disambiguation (a double or hold is configured —
        or it's the middle button, which always has an implicit hold = toggle the screen)."""
        if input_id == "btn8":
            return True
        return (self._nonempty_action(self._gesture_action(binding, "double"))
                or self._nonempty_action(self._gesture_action(binding, "hold")))

    def _check_gestures(self) -> None:
        """Loop housekeeping: fire a hold once a key is held past _HOLD_T, and commit a pending tap
        once its double-tap window has elapsed with no second press."""
        now = time.time()
        for iid, pr in list(self._press.items()):
            if pr["hold_fired"] or now - pr["t"] < _HOLD_T:
                continue
            hold = self._gesture_action(pr["binding"], "hold")
            if self._nonempty_action(hold):
                pr["hold_fired"] = True
                self._fire_action(hold, pr["index"], pr["kind"], animate=False)
            elif iid == "btn8":                            # default middle-button hold = screen toggle
                pr["hold_fired"] = True
                self.toggle_display()
        for iid, pend in list(self._tap_pending.items()):
            if now - pend["t"] >= _DOUBLE_T:               # no second tap arrived -> it was a single
                self._tap_pending.pop(iid, None)
                self._fire_gesture(pend["kind"], pend["index"], pend["binding"], "tap")

    def _anim_ok(self, kind: str, index: int) -> bool:
        return (kind == "key" and self.connected and self.config.data.get("press_fx", True)
                and not self._panel and index >= 0 and not self._opens_folder(index))

    def _fire_action(self, action, index: int, kind: str, animate: bool = True) -> None:
        """Run an action (if non-empty), then play the key's press bounce. action FIRST = snappy.
        `animate=False` for deferred gesture fires (the physical press already bounced)."""
        do_anim = animate and self._anim_ok(kind, index)
        if isinstance(action, dict) and action.get("type") == "toggle":
            self._fire_toggle(action, index, kind)
        elif isinstance(action, dict) and action.get("type") == "timer":
            self._fire_timer(action, index, kind)
        elif self._nonempty_action(action):
            self.engine.execute(action)
        if do_anim:
            self._animate_key(index)

    # ---- toggle action (one key alternating between N states) ---------------
    def _toggle_key(self, index: int, kind: str) -> str:
        """A stable per-binding id so each toggle key remembers its own state (in-memory)."""
        prof = self.config.data.get("active_profile")
        if kind == "key":
            kid = LCD_KEYS[index] if 0 <= index < len(LCD_KEYS) else f"k{index}"
            scope = f"f:{self._folder}:{self._folder_page}" if self._folder is not None else f"p:{self.page_index}"
            return f"{prof}/{scope}/{kid}"
        return f"{prof}/{kind}:{index}"

    def _toggle_states(self, action):
        return [s for s in (action.get("states") or []) if isinstance(s, dict)]

    def _toggle_state_index(self, action, index: int, kind: str) -> int:
        states = self._toggle_states(action)
        if not states:
            return 0
        return self._toggle_idx.get(self._toggle_key(index, kind), 0) % len(states)

    def _fire_toggle(self, action, index: int, kind: str) -> None:
        states = self._toggle_states(action)
        if not states:
            return
        tkey = self._toggle_key(index, kind)
        i = self._toggle_idx.get(tkey, 0) % len(states)
        sub = states[i].get("action") or {}
        if self._nonempty_action(sub):
            self.engine.execute(sub)
        self._toggle_idx[tkey] = (i + 1) % len(states)   # advance, remember
        if kind == "key" and 0 <= index < len(LCD_KEYS):  # repaint to show the NEW current state
            try:
                self.dock.set_key_pil(index, self._face_for_index(index))
                self.dock.flush()
            except OSError:
                pass

    # ---- timer action (countdown / stopwatch rendered live on the key) ------
    @staticmethod
    def _timer_dur(action) -> int:
        if (action.get("mode") or "countdown").lower() != "countdown":
            return 0                                   # stopwatch counts up from zero
        return max(1, int(action.get("minutes", 0) or 0) * 60 + int(action.get("seconds", 0) or 0))

    @staticmethod
    def _timer_shown(st, now: float) -> float:
        """Seconds to DISPLAY: remaining for a countdown, elapsed for a stopwatch."""
        el = st["acc"] + ((now - st["t0"]) if st["running"] else 0.0)
        return max(0.0, st["dur"] - el) if st["mode"] == "countdown" else el

    def _fire_timer(self, action, index: int, kind: str) -> None:
        """Tap state-machine: start -> pause -> resume; a tap on a FINISHED timer (or an op:'reset'
        action, e.g. bound on the hold gesture) clears it back to the configured duration."""
        tkey = self._toggle_key(index, kind)           # same stable per-binding identity as toggles
        st = self._timers.get(tkey)
        now = time.time()
        if (action.get("op") or "startpause").lower() == "reset" or (st and st.get("fin_t")):
            self._timers.pop(tkey, None)
        elif st is None:                               # start
            mode = (action.get("mode") or "countdown").lower()
            self._timers[tkey] = {"mode": mode, "dur": self._timer_dur(action), "acc": 0.0,
                                  "t0": now, "running": True, "fin_t": None, "_shown": None,
                                  "beep": action.get("beep", True) is not False}
        elif st["running"]:                            # pause
            st["acc"] += now - st["t0"]
            st["running"] = False
        else:                                          # resume
            st["t0"] = now
            st["running"] = True
        self._repaint_timer_keys()

    def _advance_timers(self) -> None:
        """Per-loop-pass tick: fire the alarm the moment a countdown hits zero (state is
        timestamp-based, so timers on hidden pages still fire on time) and repaint visible
        timer keys once per displayed second (2 Hz flash for ~6s after finishing)."""
        if not self._timers:
            return
        now = time.time()
        for st in list(self._timers.values()):
            if (st["mode"] == "countdown" and st["running"] and not st.get("fin_t")
                    and self._timer_shown(st, now) <= 0.0):
                st["acc"] += now - st["t0"]
                st["running"] = False
                st["fin_t"] = now
                if st.get("beep", True):
                    self._timer_beep()
                if self._ambient is not None:          # surface the keys so the flash is seen
                    self._wake_ambient()
        self._repaint_timer_keys()

    def _repaint_timer_keys(self) -> None:
        if (not self._timers or not self.connected or not self._display_on
                or self._ambient is not None or self._panel or self._calib):
            return
        now = time.time()
        for i in range(len(LCD_KEYS)):
            st = self._timers.get(self._toggle_key(i, "key"))
            if st is None:
                continue
            fin = bool(st.get("fin_t"))
            flash = fin and (now - st["fin_t"] < 6.0) and (int(now * 2) & 1 == 0)
            sig = (int(self._timer_shown(st, now)), st["running"], fin, flash)
            if st.get("_shown") == sig:
                continue                               # displayed second unchanged -> no USB write
            st["_shown"] = sig
            item = self._current_items().get(LCD_KEYS[i]) or {}
            if (item.get("action") or {}).get("type") != "timer":
                continue                               # stale state; the visible key changed
            try:
                self.dock.set_key_pil(i, timer_face(sig[0], st["dur"], st["running"], fin, flash,
                                                    label=item.get("label", "")))
                self.dock.flush()
            except OSError:
                pass

    @staticmethod
    def _timer_beep() -> None:
        def _bp():
            try:
                import winsound
                for _ in range(3):                     # short attention pattern, no sound asset needed
                    winsound.Beep(880, 170)
                    winsound.Beep(1245, 240)
                    time.sleep(0.10)
            except Exception:
                pass
        threading.Thread(target=_bp, name="timer-beep", daemon=True).start()

    def _fire_gesture(self, kind: str, index: int, binding, slot: str) -> None:
        # deferred (release/double/expired-tap) -> no animation; the press already gave feedback
        self._fire_action(self._gesture_action(binding, slot), index, kind, animate=False)

    def toggle_display(self) -> None:
        # HID writes are single-threaded — defer to the loop thread (the tray menu calls this on the
        # Qt thread; the middle-button hold on the loop thread). Both just flag; the loop applies it.
        self._pending_display = not self._display_on

    def set_display(self, on: bool) -> None:
        self._pending_display = bool(on)

    def _apply_display(self, on: bool) -> None:
        """Turn the dock screen on (wake + restore brightness + render) or off (cleared + HAN sleep,
        backlight fully off). MUST run on the device-loop thread — it writes raw HID frames, which are
        not thread-safe (a stray command frame between a BAT header and its chunks desyncs the stream)."""
        self._display_on = bool(on)
        if self.connected:
            try:
                if self._display_on:
                    self._last_input = time.time()         # manual wake resets the idle timer, else
                    self.dock.wake()                       # _maybe_enter_ambient fires again at once
                    self.dock.set_brightness(self.config.brightness)
                    self._render_requested = True
                else:
                    self._anim = None
                    self.dock.set_brightness(0)
                    self.dock.clear_all()
                    self.dock.flush()
                    self.dock.sleep()                      # HAN: backlight fully off (deeper than brightness 0)
            except OSError:
                pass
        if self.on_status:
            self.on_status()

    # ---- the loop ----------------------------------------------------------
    def _run(self) -> None:
        while self._running:
            if not self.connected:
                if not self._try_connect():
                    # is_present() is a FULL HID bus enumeration (~19 ms here: hidapi opens a
                    # handle to every interface). Poll fast right after a disconnect so a
                    # re-plug is picked up at once, then back off — an unplugged dock should
                    # not cost ~2% of a core all day.
                    self._retries += 1
                    time.sleep(1.0 if self._retries <= 20 else 3.0)
                    continue
            try:
                self._service_requests()
                self._check_gestures()
                self._drain_remote()
                # Spin faster while an animation/wallpaper/calibration runs, or while a gesture is
                # pending (a held key awaiting hold, or a tap awaiting a possible double) so the
                # 0.5s hold / 0.28s double resolve promptly. 10ms = true per-frame animations; the
                # marquee is only 20fps so 40ms feeds it fine (it accumulates from capped elapsed dt).
                # Animation terms are gated on the screen being ON so an OFF screen with stale state
                # drops to 200ms; gesture polling is NOT gated so a hold can wake the screen.
                on = self._display_on
                # Fast-poll only while a gesture is still UNRESOLVED: a held key awaiting its hold,
                # or a tap awaiting a double. Once the hold has fired we stop spinning (the release
                # arrives as an event regardless of poll rate) — avoids a busy-wait during a long hold.
                gesture = bool(self._tap_pending) or any(
                    pr.get("hold_pending") and not pr["hold_fired"]
                    for pr in self._press.values())
                anim_idle = self._ambient is not None and self._ambient.get("anim")
                # A wallpaper needs per-frame wakeups only while it HAS frames to advance: a
                # still image is pushed once and _advance_panel then no-ops, so the 10 ms tier
                # burned ~99 of every 100 wakeups on nothing. An animated one wakes on its own
                # frame grid (fps, default 12) instead of at 100 Hz.
                panel_anim = bool(self._panel) and len(self._panel["jpeg"]) > 1
                # 10ms only for real per-frame work (animation/HUD/calib) or a pending gesture.
                fast = on and (self._anim or self._page_anim or self._calib
                               or self._volume_hud)
                if fast or gesture:
                    timeout = 10
                elif on and panel_anim:                 # wake on the wallpaper's own frames
                    p = self._panel
                    per = 1.0 / p["fps"]
                    due = per - ((time.time() - p["start"]) % per)
                    timeout = max(1, min(100, int(due * 1000)))
                elif on and anim_idle:                  # animated idle -> wake to the next ~15fps
                    due = self._ambient.get("last_anim", 0.0) + 0.066 - time.time()   # frame, not 100Hz
                    timeout = max(1, min(66, int(due * 1000)))
                elif on and self._marquee:
                    timeout = 40
                elif on and self._ambient is not None:  # static idle clock -> 1s granularity is plenty
                    timeout = 1000
                else:
                    timeout = 200
                if self.mirror_clients > 0:             # a VR panel is watching: its presses must not
                    timeout = min(timeout, 25)          # wait out a 200 ms / 1 s read (hold = 0.5 s)
                ev = self.dock.read_event(timeout_ms=timeout)
                if ev:
                    if self.on_input is not None:
                        try:
                            self.on_input(ev)
                        except Exception as e:
                            _log_error("mirror echo", e)
                    self._handle_event(ev)
            except OSError:
                # device was unplugged / I/O error -> drop and retry
                self._set_connected(False)
                self.dock.close()
                time.sleep(0.5)
            except Exception as e:
                # A render/encode bug must NEVER kill the loop — otherwise the last frame
                # (e.g. the calibration pattern) freezes on screen and input dies. Log + go on.
                _log_error("loop", e)
                time.sleep(0.05)

    # ---- remote input (Orbit's VR panel, dock/mirror.py) --------------------
    def inject(self, input_id: str, down: Optional[bool] = None, turn: int = 0) -> bool:
        """Queue an input as if the dock sent it: key1..key6 / btn7..btn9 / enc0..enc2 with
        down=True/False (press and release, so hold and double-tap work as on the device), or
        turn=+1/-1 on an encoder. Any thread; the loop handles it. False = not a valid input."""
        ev = remote_event(input_id, down, turn)
        if ev is None:
            return False
        self._remote.append(ev)
        return True

    def request_wake(self) -> None:
        """A VR pointer arrived: leave the idle screen and reset the idle timer, fire nothing (else
        the first real press would only wake the screen and be swallowed)."""
        self._wake_requested = True

    def jog(self, input_id: str, steps: int) -> bool:
        """A VR jog slider: fire encoder enc0..enc2's turn binding |steps| times (at most 10), each
        like one slow detent (never accelerated). Leaves the idle screen first, so no step is
        swallowed. Any thread; the loop handles it. False = not a valid jog."""
        try:
            n = max(-_JOG_MAX, min(_JOG_MAX, int(steps)))
        except (TypeError, ValueError):
            return False
        if remote_event(input_id, None, n) is None or not input_id.startswith("enc"):
            return False
        self._wake_requested = True                 # drained before the steps below
        for _ in range(abs(n)):
            self._remote.append(remote_event(input_id, None, n))
        return True

    def request_set(self, input_id: str, kind: str, value, final: bool = False) -> bool:
        """A VR slider drag: set encoder enc0..enc2's CURRENT turn value to `value` (0..1). Any
        thread; latest-wins per encoder (a 'final' is never lost to a later tick). The loop applies
        it only if the binding still has this kind (an auto page switch mid-drag can't turn a volume
        drag into a monitor set). No knob HUD. False = not a valid request."""
        try:
            i = int(input_id[3:]) if str(input_id).startswith("enc") else -1
            v = float(value)
        except (TypeError, ValueError):
            return False
        if not (0 <= i <= 2) or not math.isfinite(v) or not isinstance(kind, str):
            return False
        v = max(0.0, min(1.0, v))
        with self._set_lock:
            prev = self._remote_set.get(i)
            self._remote_set[i] = (kind, v, bool(final) or bool(prev and prev[2]))
        return True

    def _drain_remote(self) -> None:
        if self._wake_requested:
            self._wake_requested = False
            self._last_input = time.time()
            if self._ambient is not None:
                self._wake_ambient()
        if self._remote_set:
            with self._set_lock:
                sets, self._remote_set = self._remote_set, {}
            for i, (kind, v, final) in sets.items():
                try:
                    self._apply_set(i, kind, v, final)
                except Exception as e:                 # a bad set must never kill the loop
                    _log_error("mirror set", e)
        while self._remote:
            self._handle_event(self._remote.popleft())
        if self._bright_save_at and time.time() - self._bright_save_at >= _BRIGHT_SAVE_IDLE:
            self._save_brightness()                    # the drag rested without a 'final'

    # ---- mirror sliders (dock/mirror.py) ----------------------------------------------------
    @staticmethod
    def _turn_relative(a) -> bool:
        """A plain relative nudge an absolute slider can stand in for (not set / mute)."""
        t = (a.get("type") or "").lower()
        if t == "volume":
            return (a.get("volume") or "").lower() in ("up", "down")
        if t == "brightness":
            return (a.get("mode") or "set").lower() in ("up", "down") or "delta" in a
        if t == "monitor":
            return (a.get("monitor") or "up").lower() in ("up", "down")
        return False

    def slider_info(self, enc: str):
        """(slider, kind, monitor index) for encoder 'enc0'..'enc2' on the CURRENT page:
        'abs' = both turn directions nudge one value Hexpad can read AND set (dock brightness, system
        volume, the same monitor(s)); 'jog' = any other turn binding; '' = none. Binding lookups only
        (cheap, any thread)."""
        def act(iid):
            b = self._binding_for(iid)
            a = b.get("action") if isinstance(b, dict) else None
            return a if isinstance(a, dict) and (a.get("type") or "none") not in ("none", "") else None
        right, left = act(enc + "+"), act(enc + "-")
        if right is None and left is None:
            return "", "", None
        kind = str((right or left).get("type") or "").lower()
        if (right is None or left is None or kind not in _ABS_KINDS
                or str(left.get("type") or "").lower() != kind
                or not (self._turn_relative(right) and self._turn_relative(left))):
            return "jog", kind, None
        if kind == "monitor":
            idx = ActionEngine.monitor_index(right)
            if ActionEngine.monitor_index(left) != idx:
                return "jog", kind, None              # + and - drive different monitors
            return "abs", kind, idx
        return "abs", kind, None

    def slider_values(self, sliders: Dict[str, tuple]) -> Dict[str, dict]:
        """{'enc1': {'v': 0.42, 'muted': False}, ..} for the 'abs' encoders of `sliders` ({enc:
        slider_info}), read from CACHES only (config, the last system-volume reading, the monitor
        dimmer's set-point); v None = unknown. Never touches a device: safe on the Qt thread."""
        out: Dict[str, dict] = {}
        for e, (slider, kind, idx) in sliders.items():
            if slider != "abs":
                continue
            if kind == "brightness":
                out[e] = {"v": round(self.config.brightness / 100.0, 2)}
            elif kind == "volume":
                from . import actions
                last = actions.sysvol_last()
                out[e] = {"v": None if last is None else round(last[0] / 100.0, 2),
                          "muted": bool(last and last[1])}
            elif kind == "monitor":
                from . import monitors
                cur = self.engine.monitor_value(idx)
                out[e] = {"v": None if cur is None else round(max(0.0, min(100.0, cur)) / 100.0, 2),
                          "off": bool(monitors.is_off() or monitors.is_busy())}
        return out

    def _apply_set(self, i: int, kind: str, v: float, final: bool) -> bool:
        """Loop thread: one slider value. Every setter here returns at once (an attribute + the
        next pass's HID write; the ordered system-volume thread; the monitor dimmer worker)."""
        slider, cur, idx = self.slider_info(f"enc{i}")
        if slider != "abs" or cur != kind:
            return False                               # the binding changed under the drag -> drop
        if kind == "monitor":
            from . import monitors
            if monitors.is_off() or monitors.is_busy():
                return False                           # a set would re-light switched-off monitors
        pct = int(round(v * 100))
        self._last_input = time.time()
        if self._ambient is not None:
            self._wake_ambient()                       # wake AND apply: a set is not swallowed
        if kind == "brightness":
            if self._display_on:
                self.set_brightness_live(pct)
            else:
                self.config.brightness = pct           # the screen-on path applies it
            if final:
                self._save_brightness()
            else:
                self._bright_save_at = time.time()
        elif kind == "volume":
            self.engine.set_system_volume(pct)
        else:
            self.engine.set_monitor_brightness(pct, idx, quiet=True)
        return True

    def _save_brightness(self) -> None:
        self._bright_save_at = 0.0
        try:
            self.config.save()
        except Exception as e:
            _log_error("save brightness", e)

    def set_mirror_hi(self, on: bool) -> None:
        """A client wants (or no client wants any more) sharp 2x faces. Any thread; the loop applies
        it (_apply_mirror_hi)."""
        self._mirror_hi_req = bool(on)

    def _apply_mirror_hi(self, on: bool) -> None:
        """Loop thread: start/stop the 2x copies. Turning on re-pushes what the keys show, so the
        copies exist at once (the dock gets the same pictures again; cached faces = same bytes)."""
        self._mirror_hi = on
        _images.set_mirror_hi(on)
        self.dock.mirror_hi = on
        if not on:
            _images.drop_mirror_hi()                   # free the copies the cached faces carry
            return
        if not self._calib:                            # never draw the page over a calib preview
            self._render_requested = True
        self._live_sig.clear()
        if self._ambient is not None:                  # re-push all six idle tiles, not just changes
            self._ambient["pushed"] = None
            self._ambient["sig"] = None

    def _try_connect(self) -> bool:
        try:
            if not AKP03.is_present():
                return False
            self._retries = 0                  # found it -> next disconnect polls fast again
            self.dock.open()
            self.dock.set_brightness(self.config.brightness)
            self.dock.clear_all()
            self.dock.flush()
            self._set_connected(True)
            self._render_requested = True
            self._service_requests()
            return True
        except Exception:
            self.dock.close()
            return False

    def _service_requests(self) -> None:
        want = self._mirror_hi_req                 # before the idle-screen early return below
        if want != self._mirror_hi or want != _images.mirror_hi():   # (a restarted loop thread too)
            self._apply_mirror_hi(want)
        if self._reload_requested:
            self._reload_requested = False
            with self._lock:
                self.config = Config.load()
            self._apply_geometry()
            self._folder = None
            self._folder_page = 0
            self._pending_brightness = self.config.brightness
            self._render_requested = True
        if self._pending_brightness is not None:
            b = self._pending_brightness
            self._pending_brightness = None
            self.dock.set_brightness(b)
        if self._pending_display is not None:  # tray/gesture screen on/off -> apply HID writes here
            on = self._pending_display
            self._pending_display = None
            self._apply_display(on)
        self._advance_timers()                 # timers tick (and alarm) even while ambient is up
        # While the ambient/idle screen is showing, hold it and defer page/switch work until a wake.
        if self._ambient is not None:
            self._advance_ambient()
            return
        if self._pending_switch is not None:
            sw = self._pending_switch
            self._pending_switch = None
            self._apply_app_switch(*sw)            # may set _pending_page, serviced just below
        if self._pending_page is not None:
            folder, direction, idx = self._pending_page
            self._pending_page = None
            self._start_page_swipe(folder, direction, idx)
        if self._volume_hud is not None:
            self._advance_volume_hud()
            return                                # hold the HUD; don't draw the page/live over it
        # Render the calibration preview FIRST, then the page — so when both fire in one pass
        # (e.g. a preview tick landing right as the dialog closes), the normal page is always
        # the LAST thing written to the keys and the pattern can never win the final write.
        if self._calib_dirty:
            self._calib_dirty = False
            try:
                self._render_calib()
            except OSError:
                raise
            except Exception as e:
                _log_error("render_calib", e)
        if self._render_requested:
            self._render_requested = False
            try:
                self._render_page()
            except OSError:
                raise
            except Exception as e:
                _log_error("render_page", e)
        if self._anim:
            self._advance_anim()
        if self._page_anim:
            self._advance_page_anim()
        if self._panel:
            try:
                self._advance_panel()
            except Exception as e:
                _log_error("advance_panel", e)
        elif self._has_live and not self._anim and not self._page_anim:
            now = time.time()
            if now - self._live_last >= 1.0:
                self._live_last = now
                try:
                    self._tick_live()
                except OSError:
                    raise
                except Exception as e:
                    _log_error("tick_live", e)
            if self._marquee:                       # scroll a long now-playing title between ticks
                try:
                    self._advance_marquee()
                except OSError:
                    raise
                except Exception as e:
                    _log_error("marquee", e)
        self._maybe_enter_ambient()                 # drop to the idle clock after enough idle time

    # ---- display geometry / calibration ------------------------------------
    def _apply_geometry(self) -> None:
        """Push the config's display calibration (w/h tile + shift) into the render modules."""
        from . import images, device
        d = self.config.display()                 # normalises w/h (incl. old `size`) + shift
        w = max(40, min(180, int(d.get("w", 88))))
        h = max(40, min(180, int(d.get("h", 88))))
        # Clamp the nudge so a stale/large saved shift can't crop the icon off the far edge.
        dx = max(-20, min(20, int(d.get("dx", 0))))
        dy = max(-20, min(20, int(d.get("dy", 0))))
        ins = max(0, min(10, int(d.get("inset", 2))))
        images.KEY_SIZE = (w, h)
        images.AUTO_ICONS = bool(self.config.data.get("auto_icons", True))
        device.KEY_SIZE = (w, h)
        device.CONTENT_SHIFT = (dx, dy)
        device.EDGE_INSET = ins
        images.clear_face_cache()      # geometry/auto-icon changes: drop stale-sized cached faces

    def begin_calibration(self) -> None:
        """Open a calibration session: only now will preview_calibration take effect."""
        self._calib_active = True
        _log_debug("begin")

    def _render_calib(self) -> None:
        if not self.connected or not self._calib or not self._calib_active:
            return
        from .images import calib_pattern
        from .device import encode_key_image
        w, h, dx, dy, ins = self._calib
        try:
            jb = encode_key_image(calib_pattern((w, h)), self.dock.image_rotation,
                                  self.dock.image_mirror, size=(w, h), shift=(dx, dy), inset=ins)
            for i in range(len(LCD_KEYS)):
                self.dock.set_key_image(i, jb)
            self.dock.flush()
        except OSError:
            pass

    def preview_calibration(self, w: int, h: int, dx: int, dy: int, inset: int = 2) -> None:
        """Live preview (any thread): show the centring target with these values.

        Ignored unless a calibration session is open — so a late debounced preview that
        fires AFTER Save/Cancel (Qt timer/GC race) can never re-arm the stuck pattern.
        """
        if not self._calib_active:
            return
        self._calib = (int(w), int(h), int(dx), int(dy), max(0, min(10, int(inset))))
        self._calib_dirty = True

    def apply_calibration(self, w: int, h: int, dx: int, dy: int, inset: int = 2) -> None:
        """Persist the calibration, apply it to the render pipeline, re-render."""
        # Close the session + leave calibration mode + request the page restore FIRST, so the
        # calib pattern is always cleared from the dock even if persisting the config throws.
        self._calib_active = False
        self._calib = None
        self._calib_dirty = False
        d = self.config.data.setdefault("display", {})
        d.pop("size", None)                       # superseded by independent w/h
        d["w"], d["h"], d["dx"], d["dy"] = int(w), int(h), int(dx), int(dy)
        d["inset"] = int(inset)
        self._apply_geometry()
        self._render_requested = True
        _log_debug(f"apply w={w} h={h} dx={dx} dy={dy} inset={inset} connected={self.connected}")
        try:
            self.config.save()
        except Exception as e:
            _log_error("save_calibration", e)

    def end_calibration(self) -> None:
        """Leave calibration mode without saving — restore the normal page."""
        self._calib_active = False
        self._calib = None
        self._calib_dirty = False
        self._render_requested = True
        _log_debug(f"end connected={self.connected}")

    # ---- rendering ---------------------------------------------------------
    def _current_items(self) -> Dict[str, Any]:
        if self._folder is not None:
            folder = self.config.folders_of().get(self._folder)
            if folder is not None:
                pages = self.config._norm_folder(folder)["pages"]
                self._folder_page = max(0, min(self._folder_page, len(pages) - 1))
                return pages[self._folder_page].get("items", {})
            self._folder = None                     # folder was deleted -> fall back to page
        return self.config.page(self.page_index).get("items", {})

    def _face_for_index(self, i: int, live_val=None, scroll=None):
        # The last key is the 'Back' key whenever a folder is open; its look is customisable
        # per folder (folder['back']), its action stays fixed (return to the page).
        if self._folder is not None and i == len(LCD_KEYS) - 1 and self._folder_back_is_key6():
            folder = self.config.folders_of().get(self._folder, {})
            back = {**_BACK_FACE, **(folder.get("back") or {})}
            return render_face(back, show_label=back.get("show_label", True))
        item = self._current_items().get(LCD_KEYS[i]) or {}
        show = item.get("show_label", self.config.data.get("show_labels", True))
        # A live/dynamic key renders its current value (clock, CPU, ...). live_val lets the caller
        # pass an already-sampled value tuple so we don't double-sample (e.g. net rate) per tick.
        if item.get("live"):
            src = (item.get("live") or {}).get("source", "")
            if src == "media":                             # ALWAYS read title + cover under ONE lock
                text, caption, frac, art = live.media_snapshot()   # (never pair a stale title with a
                kind = "media"                             # freshly-fetched cover on a track change)
            else:
                text, caption, frac, kind = live_val if live_val is not None else live.value(src)
                art = None
            wx = live.weather_payload() if kind in ("weather", "wxmetric", "wxforecast") else None
            return live_face(item, text, caption, frac, kind, show_label=show,
                             style=self.config.data.get("live_style", "gauge"),
                             history=live.history(src), scroll=scroll, artwork=art, weather=wx)
        # A toggle key shows the look of its CURRENT state (so the dock reflects which way it's set).
        action = item.get("action")
        action = action if isinstance(action, dict) else {}   # tolerate a hand-edited non-dict action
        if action.get("type") == "toggle":
            states = self._toggle_states(action)
            if states:
                st = states[self._toggle_state_index(action, i, "key")]
                face = {k: st.get(k) for k in ("label", "icon", "color", "text_color", "color2")
                        if st.get(k) not in (None, "")}
                return render_face(face, show_label=st.get("show_label", show))
        # A timer key renders its live countdown/stopwatch state (configured duration when idle).
        if action.get("type") == "timer":
            st = self._timers.get(self._toggle_key(i, "key"))
            now = time.time()
            if st is None:
                dur = self._timer_dur(action)
                return timer_face(dur, dur, False, False, False, label=item.get("label", ""))
            fin = bool(st.get("fin_t"))
            flash = fin and (now - st["fin_t"] < 6.0) and (int(now * 2) & 1 == 0)
            return timer_face(int(self._timer_shown(st, now)), st["dur"], st["running"], fin,
                              flash, label=item.get("label", ""))
        # A folder key renders as a mini-grid of its contents (iOS/macOS-style tile).
        if action.get("type") == "folder":
            fid = action.get("folder")
            f = self.config.folders_of().get(fid) if fid else None
            # preview the folder's FIRST page in the mini-grid tile
            contents = self.config._norm_folder(f)["pages"][0].get("items", {}) if f else {}
            return folder_face(item, contents, show_label=show)
        return render_face(item, show_label=show)

    def _render_page(self) -> None:
        if not self.connected or not self._display_on:
            return
        self._live_sig.clear()                      # a full render invalidates the per-key live cache
        self._marquee.clear()                       # re-evaluated by the next _tick_live
        # Animated full-panel wallpaper (page-level) takes over all 6 keys, unless a folder is open.
        panel = None if self._folder is not None else self.config.page(self.page_index).get("panel")
        if panel and panel.get("path") and self._setup_panel(panel):
            self._has_live = False
            if self.on_status:
                self.on_status()
            return
        self._panel = None
        items = self._current_items()
        for i in range(len(LCD_KEYS)):              # key1..key6 -> device key 0..5
            self.dock.set_key_pil(i, self._face_for_index(i))
        self.dock.flush()
        live_keys = list(LCD_KEYS)
        if self._folder is not None and self._folder_back_is_key6():
            live_keys = live_keys[:-1]              # the Back tile owns key6's slot in this mode
        self._has_live = any((items.get(k) or {}).get("live") for k in live_keys)
        self._live_last = time.time()
        if self._has_live:
            self._live_last = 0.0          # prime the next tick so a marquee starts scrolling at once
        if self.on_status:
            self.on_status()

    # ---- full-panel wallpaper / animation ----------------------------------
    def _setup_panel(self, panel: Dict[str, Any]) -> bool:
        """Decode (and cache) the page's wallpaper frames, push the first one. False if missing."""
        from . import images, device
        path = panel.get("path")
        if not path or not os.path.exists(path):
            self._panel = None
            return False
        gap = int(panel.get("gap", 22))
        # rotation/mirror AND the calibrated shift/inset are baked into the pre-encoded JPEGs, so a
        # change to any of them must invalidate the cache (else a nudge/inset replays stale frames).
        key = (path, images.KEY_SIZE, gap, self.page_index,
               self.dock.image_rotation, self.dock.image_mirror,
               device.CONTENT_SHIFT, device.EDGE_INSET)
        if not self._panel or self._panel.get("key") != key:
            try:
                frames = panel_frames(path, gap=gap)
            except Exception as e:
                _log_error("panel_decode", e)
                self._panel = None
                return False
            if not frames:
                self._panel = None
                return False
            # Pre-encode every frame's 6 key JPEGs ONCE (a looping wallpaper plays the same bytes
            # forever, so re-encoding per frame just burned CPU). Playback is then pure HID writes,
            # and we drop the (larger) PIL tiles to save memory.
            from .device import encode_key_image
            rot, mir = self.dock.image_rotation, self.dock.image_mirror
            jpeg = [[encode_key_image(t, rot, mir, _ANIM_Q, _ANIM_SS) for t in fr] for fr in frames]
            self._panel = {"key": key, "jpeg": jpeg, "last": -1,
                           "fps": max(1, min(60, int(panel.get("fps", 12)))),
                           "start": time.time()}
        self._advance_panel()
        return True

    def _advance_panel(self) -> None:
        p = self._panel
        if not p or not self.connected or not self._display_on:
            return
        jpeg = p["jpeg"]
        if len(jpeg) <= 1:
            if p["last"] == 0:
                return                              # static wallpaper: push once
            fi = 0
        else:
            fi = int((time.time() - p["start"]) * p["fps"]) % len(jpeg)
            if fi == p["last"]:
                return
        p["last"] = fi
        for i, jb in enumerate(jpeg[fi]):           # pre-encoded -> just upload, no re-encode
            self.dock.set_key_image(i, jb)
        self.dock.flush()

    # ---- transient knob-feedback HUD (any encoder that changes a value) -----
    def show_value_hud(self, value, name: str, *, muted: bool = False, accent=None,
                       unit: str = "%", relative: int = 0, vmax: int = 100) -> None:
        """Pop the knob HUD (hud_fx.KnobHud: name, odometer number, LED ladder) on the keys; it
        lingers 1.3 s after the LAST call. `value` is a 0..vmax reading (vmax=200 for Discord
        output), or None with `relative` = ±1 when only the direction is known (RGB etc.).

        Runs on action worker threads (system-volume reader, Discord/OBS coalescers, the Tapo
        loop...), so it only RECORDS the detent, under _hud_lock; the device loop renders and
        pushes the frames (_advance_volume_hud), keeping every HID write on the loop thread. The
        same name continues the running session (the number glides on, the peak marker holds, a
        turn during the exit sweep re-enters); a new name starts a new session."""
        target = None if value is None else int(value)
        rel = int(relative or 0)
        ev = {"value": target, "name": name or "", "muted": bool(muted), "accent": accent,
              "unit": unit or "", "relative": (rel > 0) - (rel < 0),
              "vmax": max(1, int(vmax or 100))}
        now = self._hud_clock()
        with self._hud_lock:
            hud = self._volume_hud
            if hud is None or hud["ev"]["name"] != ev["name"]:
                # a new session; "faces" None = the loop has not rendered a frame of it yet
                hud = {"t0": now, "events": 0, "dir": 0, "dur": 1.3, "ev": ev, "faces": None}
                self._volume_hud = hud
            else:
                # the turn direction (chevrons, odometer kick) comes from the last two readings,
                # or from a relative ±1 detent
                a = hud["ev"]["value"]
                if a is not None and target is not None and target != a:
                    hud["dir"] = 1 if target > a else -1
                elif ev["relative"]:
                    hud["dir"] = ev["relative"]
                hud["ev"] = ev
            hud["events"] += 1
            hud["start"] = now                     # the LAST detent: the HUD lingers dur after it

    def show_volume_hud(self, vol: int, muted: bool, name: str) -> None:
        """Back-compat: a volume HUD is a value HUD (negative vol = unknown / relative)."""
        self.show_value_hud(None if (vol is not None and vol < 0) else vol, name, muted=muted)

    def _advance_volume_hud(self) -> None:
        """One HUD frame, on the device-loop thread: build the state KnobHud renders from (the
        fields of the approved bench's HudSim.make_state), push ONLY the keys whose pixels changed,
        and hand the keys back to the page once the HUD has lingered 1.3 s. At rest every tile is
        the same cached image, so a resting HUD costs no USB writes; after the hand-back nothing
        keeps rendering (_volume_hud is None -> the loop drops to its idle tiers)."""
        hud = self._volume_hud
        if not hud:
            return
        now = self._hud_clock()
        live = self.connected and self._display_on
        with self._hud_lock:
            if self._volume_hud is not hud:
                return                                # replaced meanwhile -> the next pass starts it
            # a backwards wall-clock jump must not pin the HUD on screen: it just ends
            done = not live or now - hud["start"] >= hud["dur"] or now < hud["start"] - 1.0
            if done:
                self._volume_hud = None
            ev, events, direction, last = hud["ev"], hud["events"], hud["dir"], hud["start"]

        def hand_back(render: bool) -> None:
            self._hud_keys = None
            if self._hud_fx is not None:
                self._hud_fx.reset()                  # drop the page faces + per-tile caches
            if render:
                self._render_page()                   # the page again, at full JPEG quality

        if done:
            hand_back(render=live)
            return
        try:
            if hud["faces"] is None:                  # the first frame of a new session
                from . import hud_fx                  # lazy: numpy + the LED geometry load on the
                if self._hud_fx is None:              # first HUD, not at app start
                    self._hud_fx = hud_fx.KnobHud()
                self._hud_fx.reset()
                # Another knob's HUD still up -> wipe in from ITS tiles (what the keys show), not
                # from the page: no flash of the page between two knobs. Otherwise what the keys
                # show is not known for sure (an idle-clock wake, a press bounce or a toggle repaint
                # may have drawn since the last page render), so the first frame pushes all 6 once.
                on = self._hud_keys
                n = len(LCD_KEYS)
                hud["under"] = list(on["img"]) if on else None
                hud["img"] = list(on["img"]) if on else [None] * n
                hud["raw"] = list(on["raw"]) if on else [None] * n
                # the power-on sparkle only on a FRESH HUD - not on every turn a few seconds apart
                hud["fresh"] = on is None and now - self._hud_shown >= 5.0
                hud["disp"] = hud["disp_t"] = None
                hud["faces"] = [self._face_for_index(i) for i in range(n)]   # for ENTER / EXIT
            from . import hud_fx
            t = now - hud["t0"]
            target, prev, pt = ev["value"], hud["disp"], hud["disp_t"]
            # Ease the DISPLAYED value toward the target (frame-rate independent, tau 55 ms) so the
            # odometer rolls and the ladder glides between readings. Snap on a big jump - a hue
            # wrap (359->1) or a fast spin - so it never sweeps the long way round.
            if target is None:
                disp = None
            elif prev is None:
                disp = float(target)
            elif abs(target - prev) > ev["vmax"] * 0.5:
                disp = float(target)
            else:
                disp = prev + (target - prev) * (1.0 - math.exp(-max(0.0, t - pt) / 0.055))
                if abs(target - disp) < 0.4:
                    disp = float(target)
            vel = (disp - prev) / (t - pt) if (disp is not None and prev is not None and t > pt) else 0.0
            hud["disp"], hud["disp_t"] = disp, t
            st = {"t": t,                                  # s since the session appeared
                  "since_event": now - last,               # s since the last detent / push
                  "remaining": max(0.0, last + hud["dur"] - now),   # until the exit completes
                  "events": events, "name": ev["name"], "unit": ev["unit"], "vmax": ev["vmax"],
                  "value": target, "disp": disp, "vel": vel, "dir": direction,
                  "relative": ev["relative"], "muted": ev["muted"], "accent": ev["accent"],
                  # the enter wipe of a knob switch sweeps over the old HUD; the exit always
                  # dissolves into the real page
                  "page": (hud["under"] if hud["under"] is not None and t < hud_fx.ENTER
                           else hud["faces"]),
                  "tile": hud_fx.T, "gap": hud_fx.GAP, "fresh": hud["fresh"]}
            tiles = self._hud_fx.render(st)
            pushed = False
            for i, tile in enumerate(tiles):
                if tile is hud["img"][i]:
                    continue                          # the very same cached tile -> no USB write
                raw = tile.tobytes()
                if raw != hud["raw"][i]:              # pixels changed -> push this key only
                    self.dock.set_key_pil(i, tile, quality=_ANIM_Q, subsampling=_ANIM_SS)
                    hud["raw"][i] = raw
                    pushed = True
                hud["img"][i] = tile
            if pushed:
                self.dock.flush()
            self._hud_keys = {"img": hud["img"], "raw": hud["raw"]}
            self._hud_shown = now
        except OSError:
            with self._hud_lock:
                if self._volume_hud is hud:
                    self._volume_hud = None
            hand_back(render=False)                   # the loop's next read sees the unplug
        except Exception as e:                        # a render bug must not freeze a half-drawn HUD
            _log_error("knob_hud", e)
            with self._hud_lock:
                if self._volume_hud is hud:
                    self._volume_hud = None
            hand_back(render=True)

    # ---- ambient / idle screen --------------------------------------------
    def _maybe_enter_ambient(self) -> None:
        """Drop to the idle clock once the dock has been untouched long enough — but never over an
        animation, wallpaper, calibration preview or a live HUD."""
        if self._ambient is not None or not self.connected or not self._display_on:
            return
        idle = self.config.data.get("idle", {})
        if not idle.get("enabled", True):
            return
        if self._anim or self._page_anim or self._calib or self._panel or self._volume_hud:
            return
        delay = max(10, int(idle.get("delay", 120) or 120))
        if time.time() - self._last_input >= delay:
            self._enter_ambient()

    def _enter_ambient(self) -> None:
        idle = self.config.data.get("idle", {})
        dim = max(2, min(int(self.config.brightness), int(idle.get("dim", 25) or 25)))
        try:
            self.dock.set_brightness(dim)               # dim for ambience + burn-in safety
        except OSError:
            return
        self._ambient = {"start": time.time(), "sig": None, "last_anim": 0.0, "anim": False,
                         "media_last": 0.0, "media_state": None, "np": None, "wx": None}
        try:
            self._advance_ambient()
        except OSError:
            self._ambient = None

    def _ambient_style(self) -> str:
        s = str((self.config.data.get("idle", {}) or {}).get("style", "classic") or "classic")
        return s if s in AMBIENT_STYLE_ORDER else "classic"

    def _ambient_media(self, now: float):
        """Coalesced now-playing read: (title, artist, art, playing) or None. Poll fast (1s) while a
        track is showing; while merely WATCHING for music to start, poll every 15s with one 2.5s
        confirm re-poll after each wake. The slow poll's snapshot can be a stale pre-park value (the
        sampler answers from cache the instant it is woken), so the confirm poll re-reads FRESH; and
        15s — well past the 5s park grace + the sampler's 2s fetch cadence — lets the WinRT media
        thread actually PARK for most of the cycle (6s barely exceeded the grace and parked nothing)."""
        amb = self._ambient
        st = amb.get("media_state")
        if st and st[3]:
            interval = 1.0                      # track showing -> keep title/cover/progress fresh
        elif amb.get("media_confirm"):
            interval = 2.5                      # sampler just woke from park -> re-read a FRESH value
        else:
            interval = 15.0                     # quiet watch -> the sampler parks most of the cycle
        if now - amb.get("media_last", 0.0) >= interval:
            amb["media_last"] = now
            title, artist, frac, art = live.media_snapshot()
            playing = bool(frac and frac > 0.0)
            amb["media_state"] = ((title, artist, art, playing)
                                  if title and title != "--" else None)
            amb["media_confirm"] = (not playing) and not amb.get("media_confirm")
        return amb.get("media_state")

    def _pick_ambient_layer(self, now: float) -> str:
        """Dynamic idle: now-playing whenever music is actually playing, else the clock and
        (optionally) the weather taking turns; otherwise just the chosen clock design."""
        idle = self.config.data.get("idle", {})
        if idle.get("playing", True):
            st = self._ambient_media(now)
            if st and st[3]:                                # something is playing -> cover takes over
                self._ambient["np"] = st
                return "playing"
        if idle.get("weather", False):
            wx = live.weather_payload()
            if wx and int((now - self._ambient["start"]) / _AMBIENT_WX_CYCLE) % 2 == 1:
                self._ambient["wx"] = wx                # hand the payload to _advance_ambient (avoid a re-fetch)
                return "weather"
        return "clock"

    def _advance_ambient(self) -> None:
        if not self.connected or not self._display_on:
            self._ambient = None
            return
        now = time.time()
        amb = self._ambient
        lt = time.localtime(now)
        hh, mm = time.strftime("%H", lt), time.strftime("%M", lt)
        layer = self._pick_ambient_layer(now)
        style = self._ambient_style()
        animated = (layer == "playing") or (layer == "clock" and style in AMBIENT_ANIMATED)
        amb["anim"] = animated                              # feeds the loop's 40 ms poll tier
        if animated and now - amb.get("last_anim", 0.0) < 0.066:
            return                                          # ~15 fps cap on moving layers
        step = int((now - amb["start"]) / 25)               # nudge content every 25 s (burn-in)
        drift = (step % 5 - 2, (step * 2) % 5 - 2)
        phase = now - amb["start"]
        try:
            if layer == "playing":
                # Pace first (even when the frame is skipped below) — the loop derives its
                # next wake from last_anim, so leaving it stale makes it busy-spin at 1 ms.
                amb["last_anim"] = now
                title, artist, art, playing = amb["np"]
                pos = live.media_position()
                sig = now_playing_sig(title, artist, art, phase, playing, pos)
                if sig is not None and amb.get("sig") == sig:
                    return          # still cover, ticker/progress unmoved -> no render, no USB
                amb["sig"] = sig
                tiles = ambient_now_playing_tiles(title, artist, art, phase, playing, pos=pos)
            elif layer == "weather":
                wx = amb.get("wx")
                if not wx:                              # payload expired between pick and draw -> hold the frame
                    return
                sig = ("wx", hh, mm, drift, wx.get("temp"), wx.get("feels"), wx.get("cond"),
                       wx.get("night"), wx.get("hi"), wx.get("lo"), wx.get("label"))
                if amb.get("sig") == sig:
                    return
                amb["sig"] = sig
                tiles = ambient_weather_tiles(wx, hh, mm, drift=drift)
            else:                                           # clock design (static or animated)
                wd = time.strftime("%a", lt).upper()
                day, mon = time.strftime("%d", lt), time.strftime("%b", lt).upper()
                solar = live.solar_times() if style == "solar" else None
                if animated:
                    amb["last_anim"] = now
                    amb["sig"] = None
                else:
                    sig = (hh, mm, wd, day, mon, drift, style, solar)
                    if amb.get("sig") == sig:
                        return
                    amb["sig"] = sig
                tiles = ambient_clock_tiles(hh, mm, wd, day, mon, drift=drift,
                                            style=style, phase=phase, solar=solar)
            q, ss = (_ANIM_Q, _ANIM_SS) if animated else (92, 0)
            # Push only the tiles whose pixels actually changed. On the now-playing layer the
            # cover tiles are identical frame after frame, so this alone removes most of the
            # idle USB traffic (and the JPEG encode that feeds it).
            pushed = amb.get("pushed") or [None] * len(tiles)
            dirty = False
            for i, tile in enumerate(tiles):
                raw = tile.tobytes()
                if pushed[i] == raw:
                    continue
                pushed[i] = raw
                dirty = True
                self.dock.set_key_pil(i, tile, quality=q, subsampling=ss)
            amb["pushed"] = pushed
            if dirty:
                self.dock.flush()
        except OSError:
            self._ambient = None

    def _wake_ambient(self) -> None:
        """Leave the ambient screen: restore brightness and redraw the working page next pass."""
        self._ambient = None
        try:
            self.dock.set_brightness(self.config.brightness)
        except OSError:
            pass
        self._render_requested = True

    def refresh_live(self) -> None:
        """Force the live/dynamic keys to re-render on the very next loop pass (≤200 ms), instead of
        waiting out the ~1 s cadence — used right after the dock toggles the bulb for instant feedback.
        Thread-safe enough: a stale 0.0 only ever causes one extra (harmless) render."""
        self._live_last = 0.0

    def _tick_live(self) -> None:
        """Re-render the visible view's live/dynamic keys — the page's, or the open folder's
        (a Weather center / clock / mic-state key must keep ticking inside its folder). Called
        ~once a second; skips the expensive render + JPEG encode + USB upload for any key whose
        displayed value is unchanged since the last push."""
        if not self.connected or self._panel or not self._display_on:
            return
        items = self._current_items()               # folder-aware: the open folder page's items
        style = self.config.data.get("live_style", "gauge")
        back6 = self._folder is not None and self._folder_back_is_key6()
        pushed = False
        for i, kid in enumerate(LCD_KEYS):
            if back6 and i == len(LCD_KEYS) - 1:
                continue                            # the Back tile owns this slot
            item = items.get(kid)
            if not (item and item.get("live")):
                continue
            src = (item.get("live") or {}).get("source", "")
            val = live.value(src)                      # one sample per tick (keeps history fresh)
            frac = val[2]
            show = item.get("show_label", self.config.data.get("show_labels", True))
            hist = tuple(live.history(src)) if style == "graph" else None
            sig = (src, val[0], val[1], round(frac, 3) if frac is not None else None,
                   val[3], style, show, hist)
            if val[3] in ("weather", "wxmetric", "wxforecast"):
                wx = live.weather_payload()            # the rich tile/atmospheric bg depend on the payload,
                if wx:                                 # not just the text -> fold it in or the tile goes stale
                    sig = sig + (wx.get("night"), wx.get("cond"), wx.get("label"),
                                 wx.get("hi"), wx.get("lo"), wx.get("feels"))
            # A long now-playing title scrolls — _advance_marquee OWNS this key's rendering, so we
            # must NOT also push a static frame here (that's what made the title jump on every
            # signature change, e.g. a play/pause flip). Just keep the marquee's state up to date.
            if val[3] == "media" and media_overflows(val[0]):
                prev = self._marquee.get(i)
                if prev and prev["val"][0] == val[0]:
                    prev["val"] = val                  # same title -> keep scroll position
                else:
                    self._marquee[i] = {"val": val, "offset": 0.0, "last": time.time()}
                self._live_sig[i] = sig                # mark handled; the marquee does the drawing
                continue
            self._marquee.pop(i, None)
            if self._live_sig.get(i) == sig:           # nothing changed -> don't touch the dock
                continue
            self.dock.set_key_pil(i, self._face_for_index(i, live_val=val))
            self._live_sig[i] = sig
            pushed = True
        if pushed:
            self.dock.flush()

    def _advance_marquee(self) -> None:
        """Scroll any overflowing now-playing title right->left (one key, like a light animation).
        The offset accumulates per frame from the ELAPSED time CAPPED to one frame — so when the
        marquee pauses (a key bounce / volume HUD blocks it) it resumes smoothly instead of jumping
        forward to catch up."""
        if not self._marquee or self._panel or not self._display_on:
            return                                  # folders scroll too — _tick_live seeds them
        now = time.time()
        if now - self._marquee_last < _MARQUEE_DT:
            return
        self._marquee_last = now
        items = self._current_items()
        pushed = False
        for i, m in list(self._marquee.items()):
            item = items.get(LCD_KEYS[i])
            if not (item and item.get("live")):
                self._marquee.pop(i, None)
                continue
            dt = min(now - m.get("last", now), _MARQUEE_DT * 2)   # cap -> no catch-up jump on resume
            m["last"] = now
            m["offset"] = m.get("offset", 0.0) + dt * _MARQUEE_SPEED
            self.dock.set_key_pil(i, self._face_for_index(i, live_val=m["val"], scroll=int(m["offset"])),
                                  quality=_ANIM_Q, subsampling=_ANIM_SS)   # cheaper encode for a 20fps scroll
            pushed = True
        if pushed:
            self.dock.flush()

    def _key_face(self, index: int):
        return self._face_for_index(index)

    def _animate_key(self, index: int) -> None:
        """Kick off a squash-and-stretch bounce on a key (frames stepped in the loop)."""
        if not (0 <= index < len(LCD_KEYS)) or not self._display_on:
            return
        # If another key is still bouncing, snap it back to rest first.
        if self._anim and self._anim["index"] != index:
            self._restore_key(self._anim["index"], self._anim["page"])
        try:
            frames = press_frames(self._key_face(index),
                                  self.config.data.get("press_anim", "bounce"))
        except Exception:
            return
        self._anim = {"index": index, "page": self.page_index, "frames": frames,
                      "start": time.time(), "dur": 0.020, "last": -1}
        self._advance_anim()                  # show frame 0 immediately

    def _advance_anim(self) -> None:
        a = self._anim
        if not a:
            return
        if a["page"] != self.page_index or not self.connected:
            self._anim = None
            return
        fi = int((time.time() - a["start"]) / a["dur"])
        if fi >= len(a["frames"]):
            self._restore_key(a["index"], a["page"])
            self._anim = None
            return
        if fi != a["last"]:
            a["last"] = fi
            try:
                self.dock.set_key_pil(a["index"], a["frames"][fi], quality=_ANIM_Q, subsampling=_ANIM_SS)
                self.dock.flush()
            except OSError:
                self._anim = None

    def _restore_key(self, index: int, page: int) -> None:
        if page == self.page_index and self.connected:
            try:
                self.dock.set_key_pil(index, self._key_face(index))
                self.dock.flush()
            except OSError:
                pass

    # ---- events ------------------------------------------------------------
    def _binding_for(self, input_id: str):
        if input_id.startswith("key"):
            return self._current_items().get(input_id)            # LCD keys: per-page (or folder)
        if input_id.startswith("enc"):
            # Per-page knob: the page's override if set, else the shared global default.
            page_items = self.config.page(self.page_index).get("items", {})
            if input_id in page_items:
                return page_items[input_id]
            return self.config.globals_of().get(input_id)
        return self.config.globals_of().get(input_id)            # round buttons: profile-global

    def _encoder_accel(self, index: int) -> int:
        """Multiplier for an encoder turn, based on how fast it's being spun (1 = slow / disabled)."""
        if not self.config.data.get("encoder_accel", True):
            return 1
        now = time.time()
        dt = now - self._enc_last.get(index, 0.0)
        self._enc_last[index] = now
        if dt > 0.18:                                   # a real pause -> back to fine control
            return 1
        return max(1, min(_ENC_ACCEL_MAX, int(round(_ENC_ACCEL_WINDOW / max(dt, 0.001)))))

    def _scale_step(self, action, mult: int):
        """Return a copy of a step-based encoder action with its step scaled by `mult`.
        Only relative handlers read `step`, so this is a no-op for 'set'-style actions."""
        if mult <= 1 or not isinstance(action, dict):
            return action
        t = (action.get("type") or "").lower()
        has = action.get("step") not in (None, "")
        if t in ("smartlight", "rgbscene"):
            if not has:                                 # the dial setup always writes an explicit step
                return action
            base = int(action["step"])
        elif t in _ENC_ACCEL_DEFAULT:
            base = int(action["step"]) if has else _ENC_ACCEL_DEFAULT[t]
        else:
            return action
        a = dict(action)
        a["step"] = max(1, base) * mult
        return a

    def _folder_back_is_key6(self) -> bool:
        return self.config.data.get("folder_back", "key6") == "key6"

    def _opens_folder(self, index: int) -> bool:
        """True if pressing key `index` enters or leaves a folder (its press bounce is wasted)."""
        if self._folder is not None and index == len(LCD_KEYS) - 1 and self._folder_back_is_key6():
            return True                            # the auto Back key
        item = self._current_items().get(LCD_KEYS[index]) or {}
        return (item.get("action") or {}).get("type") == "folder"

    def _handle_event(self, ev: Event) -> None:
        # Any real input resets the idle timer. While the ambient screen is up, the FIRST input
        # just wakes it (and is swallowed — it must NOT also fire the key's binding).
        if ev.kind != "release_all":
            self._last_input = time.time()
            if self._ambient is not None:
                self._wake_ambient()
                return
        if ev.kind == "key" and ev.pressed:
            self._last_key_index = ev.index        # remember it for the 'expand' folder anim
        if ev.kind == "release_all":               # a final 'all up' -> resolve any lingering presses
            for iid in list(self._press.keys()):
                self._resolve_release(iid)
            return
        # Exit a folder: the 6th key (default) OR a configured round button acts as 'Back' (no gestures
        # on it). A round button keeps its normal action everywhere EXCEPT while a folder is open.
        if self._folder is not None and ev.pressed:
            fb = self.config.data.get("folder_back", "key6")
            if ((fb == "key6" and ev.kind == "key" and ev.index == len(LCD_KEYS) - 1)
                    or (fb in ("btn7", "btn8", "btn9") and ev.kind == "button" and ev.input_id == fb)):
                self.folder_back()
                return
        if ev.kind == "encoder_turn":              # turns fire immediately (with acceleration)
            binding = self._binding_for(ev.input_id)
            # A remote (VR) turn is never accelerated and leaves the knob's timing alone: remote
            # turns are drained in batches per loop pass, so their gaps say nothing about speed.
            mult = 1 if getattr(ev, "remote", False) else self._encoder_accel(ev.index)
            if binding and mult > 1:
                binding = {**binding, "action": self._scale_step(binding.get("action"), mult)}
            if binding:
                self.engine.execute(binding.get("action"))
            return
        if ev.kind in ("key", "button", "encoder_push"):
            if ev.pressed:
                self._on_press(ev)
            else:
                self._resolve_release(ev.input_id)

    def _on_press(self, ev: Event) -> None:
        iid = ev.input_id
        binding = self._binding_for(iid)
        if not self._has_gestures(iid, binding):
            # tap-only input: fire instantly on press (action FIRST, then the press bounce) — the
            # zero-latency common case, unchanged from before.
            self._fire_action(binding.get("action") if binding else None, ev.index, ev.kind)
            return
        # Multi-gesture input: bounce now for instant feedback; the action is disambiguated.
        if self._anim_ok(ev.kind, ev.index):
            self._animate_key(ev.index)
        if iid in self._tap_pending and self._nonempty_action(self._gesture_action(binding, "double")):
            self._tap_pending.pop(iid, None)       # a 2nd press inside the window -> double-tap
            self._press[iid] = {"t": time.time(), "index": ev.index, "kind": ev.kind,
                                "binding": binding, "hold_fired": True}   # consumed; release no-ops
            self._fire_gesture(ev.kind, ev.index, binding, "double")
            return
        hold_pending = (iid == "btn8") or self._nonempty_action(
            self._gesture_action(binding, "hold"))     # only fast-poll a held key that has a hold to fire
        self._press[iid] = {"t": time.time(), "index": ev.index, "kind": ev.kind,
                            "binding": binding, "hold_fired": False, "hold_pending": hold_pending}

    def _resolve_release(self, iid: str) -> None:
        pr = self._press.pop(iid, None)
        if pr is None or pr["hold_fired"]:
            return                                 # hold already fired, or a double consumed this press
        binding = pr["binding"]
        if self._nonempty_action(self._gesture_action(binding, "double")):
            # a double is configured -> wait out the window for a possible second tap
            self._tap_pending[iid] = {"t": time.time(), "index": pr["index"],
                                      "kind": pr["kind"], "binding": binding}
        else:                                      # only a hold was configured and it didn't fire -> tap
            self._fire_gesture(pr["kind"], pr["index"], binding, "tap")

    # ---- navigation (called by ActionEngine, in loop thread) ---------------
    def _page_count(self) -> int:
        return max(1, len(self.config.pages()))

    def _folder_page_count(self) -> int:
        f = self.config.folders_of().get(self._folder) if self._folder else None
        return max(1, len(self.config._norm_folder(f)["pages"])) if f else 1

    def next_page(self) -> None:
        # Inside an open folder, page-switch flips the FOLDER's pages; otherwise the profile pages.
        if self._folder is not None:
            self._pending_page = (True, 1, (self._folder_page + 1) % self._folder_page_count())
        else:
            self._last_manual_nav = time.time()
            self._pending_page = (False, 1, (self.page_index + 1) % self._page_count())

    def prev_page(self) -> None:
        if self._folder is not None:
            self._pending_page = (True, -1, (self._folder_page - 1) % self._folder_page_count())
        else:
            self._last_manual_nav = time.time()
            self._pending_page = (False, -1, (self.page_index - 1) % self._page_count())

    def goto_page(self, index: int) -> None:
        if self._folder is not None:
            n = self._folder_page_count()
            idx = max(0, min(n - 1, int(index)))
            direction = 0 if idx == self._folder_page else (1 if idx > self._folder_page else -1)
            self._pending_page = (True, direction, idx)
        else:
            self._last_manual_nav = time.time()
            idx = max(0, min(self._page_count() - 1, int(index)))
            direction = 0 if idx == self.page_index else (1 if idx > self.page_index else -1)
            self._pending_page = (False, direction, idx)

    def _start_page_swipe(self, folder: bool, direction: int, new_index: int) -> None:
        """Phone-style horizontal swipe across all 6 keys on a page change — a profile page, or a
        page WITHIN the open folder when `folder` is True."""
        self._anim = None                                 # cancel any key bounce
        cur = self._folder_page if folder else self.page_index

        def _commit():
            if folder:
                self._folder_page = new_index
            else:
                self._folder = None                       # a profile-page switch exits any folder
                self._folder_page = 0
                self.page_index = new_index

        if not self.connected or new_index == cur or not self.config.data.get("page_fx", True):
            _commit()
            self._render_requested = True
            return
        old_faces = [self._face_for_index(i) for i in range(len(LCD_KEYS))]
        _commit()
        new_faces = [self._face_for_index(i) for i in range(len(LCD_KEYS))]
        self._page_anim = {"frames": page_swipe_frames(old_faces, new_faces, direction, frames=16),
                           "start": time.time(), "dur": 0.018, "last": -1}
        self._advance_page_anim()
        if self.on_status:
            self.on_status()

    def _advance_page_anim(self) -> None:
        a = self._page_anim
        if not a:
            return
        if not self.connected or not self._display_on:
            self._page_anim = None
            return
        n = a["n"] if "gen" in a else len(a["frames"])
        total = a["dur"] * n
        elapsed = time.time() - a["start"]
        if elapsed >= total:
            self._page_anim = None
            self._render_page()                           # settle on the final page
            return
        # OutQuad eased index over the SAME frames/duration: motion starts quick and settles soft
        # instead of a robotic constant rate (pure timing — zero extra encodes / USB packets).
        t = elapsed / total if total > 0 else 1.0
        fi = min(n - 1, int((1.0 - (1.0 - t) * (1.0 - t)) * n))
        if fi != a["last"]:
            a["last"] = fi
            try:
                faces = a["gen"](fi) if "gen" in a else a["frames"][fi]  # lazy or pre-rendered
                for i, face in enumerate(faces):
                    self.dock.set_key_pil(i, face, quality=_ANIM_Q, subsampling=_ANIM_SS)
                self.dock.flush()
            except OSError:
                self._page_anim = None

    def enter_folder(self, fid: str) -> None:
        if fid and fid in self.config.folders_of():
            old = [self._face_for_index(i) for i in range(len(LCD_KEYS))] if self.connected else None
            self._folder_src = self._last_key_index        # the folder grows from the pressed key
            self._folder = fid
            self._folder_page = 0                          # always open a folder at its first page
            self._start_folder_anim(old, opening=True)

    def folder_back(self) -> None:
        if self._folder is not None:
            old = [self._face_for_index(i) for i in range(len(LCD_KEYS))] if self.connected else None
            self._folder = None
            self._folder_page = 0
            self._start_folder_anim(old, opening=False)

    def _start_folder_anim(self, old_faces, opening: bool) -> None:
        """Play a zoom transition for the folder open/close (reuses the page-anim stepper)."""
        if (not self.connected or old_faces is None
                or not self.config.data.get("page_fx", True)):
            self._render_requested = True                  # transitions off -> just snap
            return
        new_faces = [self._face_for_index(i) for i in range(len(LCD_KEYS))]
        self._anim = None                                  # cancel any in-flight key bounce
        name = self.config.data.get("folder_anim", "zoom")
        if name == "expand":
            # generate frames lazily (one per displayed step) so it starts instantly instead of
            # stalling ~100ms while all frames are pre-rendered.
            n, gen = expand_gen(old_faces, new_faces, opening, 14, self._folder_src)
            self._page_anim = {"gen": gen, "n": n, "start": time.time(), "dur": 0.020, "last": -1}
        else:
            frames = folder_transition_frames(old_faces, new_faces, opening, name=name,
                                              src=self._folder_src, frames=14)
            self._page_anim = {"frames": frames, "start": time.time(), "dur": 0.018, "last": -1}
        self._advance_page_anim()
        if self.on_status:
            self.on_status()

    def set_profile(self, name: str) -> None:
        if name and self.config.set_active_profile(name):
            self._folder = None
            self._folder_page = 0
            self.page_index = 0
            self.config.save()
            self._render_requested = True

    # ---- app-aware auto-switching ------------------------------------------
    def request_app_switch(self, profile: Optional[str], page) -> None:
        """Called from the poller thread; the loop applies it (with suppression) next tick."""
        self._pending_switch = (profile, page)

    def _apply_app_switch(self, profile: Optional[str], page) -> None:
        # Don't yank the dock out from under the user: skip while a folder/animation is up or
        # right after a hand-driven page change.
        if self._folder is not None or self._page_anim or self._anim:
            return
        if time.time() - self._last_manual_nav < 4.0:
            return
        if profile and profile != self.config.data.get("active_profile"):
            self.set_profile(profile)
        if page is not None:
            try:
                idx = max(0, min(self._page_count() - 1, int(page)))
            except (TypeError, ValueError):
                idx = self.page_index
            if idx != self.page_index:
                self._folder = None
                self._folder_page = 0
                direction = 1 if idx > self.page_index else -1
                self._pending_page = (False, direction, idx)   # animated swipe, this same pass
        if self.on_status:
            self.on_status()

    def set_brightness(self, value: int) -> None:
        self.config.brightness = int(value)
        self.config.save()
        self._pending_brightness = self.config.brightness

    def set_brightness_live(self, value: int) -> None:
        """Apply brightness to the device WITHOUT saving — for a slider drag (the GUI debounces the
        save). The loop tracks the slider instantly via _pending_brightness; disk I/O is deferred."""
        self.config.brightness = int(value)
        self._pending_brightness = self.config.brightness

    def adjust_brightness(self, delta: int) -> None:
        self.set_brightness(self.config.brightness + int(delta))

    # ---- external (UI thread) requests -------------------------------------
    def request_reload(self) -> None:
        """Re-read config from disk and re-render (safe from any thread)."""
        self._reload_requested = True

    def request_render(self) -> None:
        self._render_requested = True

    def replace_config(self, cfg: Config) -> None:
        """Swap in an already-loaded Config (e.g. after a restore/import) and re-render."""
        with self._lock:
            self.config = cfg
        self._apply_geometry()
        self._folder = None
        self.page_index = 0
        self._pending_brightness = cfg.brightness
        self._render_requested = True

    # ---- status ------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        pages = self.config.pages()
        return {
            "connected": self.connected,
            "profile": self.config.data.get("active_profile"),
            "profiles": self.config.profile_names(),
            "page_index": self.page_index,
            "page_count": len(pages),
            "page_name": pages[self.page_index % len(pages)].get("name") if pages else "",
            "brightness": self.config.brightness,
            # api 1 (the mirror pipe): where in the folders, screen on/off, idle screen up
            "api": 1,
            "folder": self._folder,
            "folder_name": (self.config.folders_of().get(self._folder) or {}).get("name", "") if self._folder else "",
            "folder_page": self._folder_page,
            "folder_pages": self._folder_page_count() if self._folder else 0,
            "display_on": self._display_on,
            "ambient": self._ambient is not None,
        }

    def _set_connected(self, value: bool) -> None:
        if value != self.connected:
            self.connected = value
            if self.on_status:
                self.on_status()
