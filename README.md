# Hexpad

A lean, **native** controller for the **Ajazz AKP03E** "Stream Dock" (6 LCD keys + 3 buttons
+ 3 rotary encoders) — written in Python + Qt (PySide6). No browser, no web view, no account,
no cloud, no telemetry, no background updater. A real desktop window + a system-tray icon that
talk to the device directly over its vendor HID interface.

Built and verified against real hardware (USB **VID `0x0300` / PID `0x3002`**). Protocol credit:
[`4ndv/mirajazz`](https://github.com/4ndv/mirajazz) + [`4ndv/opendeck-akp03`](https://github.com/4ndv/opendeck-akp03).

## What it does

- Renders icons + labels (full-color emoji, a **Fluent** icon set, **or** your own PNG/JPG) to
  the 6 LCD keys, with live "card"-style tiles and frosted folder faces.
- Binds **every** input — 6 keys, 3 round buttons, 3 encoders (turn ◀/▶ and push) — to actions,
  each with independent **tap / double-tap / hold** gestures.
- **Actions:** open app/file/folder/URL · hotkeys (`ctrl+shift+t`) · type text · media transport ·
  system + **per-app** volume (encoder mixer with on-screen HUD) · **mic mute** · switch page/profile ·
  set brightness · **HTTP / webhook** request · multi-step macros · **toggle** (2-state) actions.
- **Live keys:** faces that show real state — mic/caps state, now-playing, CPU/GPU load & temperature,
  a rich **weather** folder (Open-Meteo: 7-day / UV / precip / wind / humidity / sun).
- **Folders** (multi-page), **pages** (unlimited per profile) and **profiles**, switchable from a key,
  button, the tray menu, or the app. Optional **app-aware** auto-switching (foreground app → profile/page).
- **Ambient idle screen:** 16 clock designs (rainbow, aurora, matrix rain, synthwave, starfield,
  plasma, lava, flip-clock, nixie…) plus dynamic **now-playing** and **weather** screens that rotate in.
- **Integrations:** Tapo smart bulbs (python-kasa), RGB scenes, OBS (websocket), Discord voice (RPC).
- Native dark-themed configurator laid out like the real device: key grid with live previews,
  drag-to-assign action sidebar, searchable action picker, color/emoji/PNG pickers, accent themes,
  **undo/redo** (Ctrl+Z/Y) and non-blocking toasts. Auto-reconnects if you unplug/replug.

## Install & run

```powershell
# one-time setup
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
./build.ps1       # produces the dist\Hexpad\ folder with Hexpad.exe (no Python needed to run)
./install.ps1     # installs to %LOCALAPPDATA%\Hexpad, makes Start Menu + Desktop shortcuts, enables autostart, launches it
```

The app runs **elevated** (so it can read CPU/GPU temperature), so `install.ps1` self-elevates via
UAC and registers autostart as an **elevated Scheduled Task** at logon (an HKCU Run entry can't
auto-elevate). After install: launch from the **Start Menu / Desktop** shortcut, or it starts
automatically on login (to the tray). Right-click the tray icon for **Open Configurator**, profile
switching, the **Start with Windows** toggle, and **Quit**. Closing the window hides it to the tray.

`./uninstall.ps1` removes the app, shortcuts, and autostart (your settings in
`%APPDATA%\AjazzDock` are kept).

### Run from source (dev)

```powershell
.\.venv\Scripts\python.exe run.py            # native window + tray
.\.venv\Scripts\python.exe -m dock.gui --tray   # start hidden to tray
```

## Config

Plain JSON at `%APPDATA%\AjazzDock\config.json`; uploaded key images in
`%APPDATA%\AjazzDock\icons\` (the Discord OAuth token, if used, lives in its own
`discord_token.json`). Binding ids match the hardware: `key1`–`key6`, `btn7`–`btn9`,
`enc0`–`enc2` (push) and `enc0+`/`enc0-` … (turn cw/ccw). Faces (label/icon/color) only render
on `key1`–`key6`. The config directory keeps the `AjazzDock` name so settings survive across
versions.

## Architecture

| File | Role |
|------|------|
| `dock/device.py`     | Low-level AKP03 HID driver (images, brightness, input events, JPEG cache) — hardware-verified |
| `dock/images.py`     | Renders key faces (emoji/Fluent/PNG + label) and the 15 ambient idle screens |
| `dock/actions.py`    | Action engine (open/hotkey/text/media/volume/mic/page/profile/brightness/http/macro/toggle) |
| `dock/actionart.py`  | Generated glyphs for actions in the picker |
| `dock/config.py`     | JSON config: profiles → pages → bindings (+ separate Discord token file) |
| `dock/controller.py` | Event loop: device input → actions, gestures, renders pages, ambient idle, reconnect |
| `dock/gui.py`        | **Native PySide6 app**: window + editor + system tray + single-instance IPC |
| `dock/live.py`       | Live data sources (media/now-playing, mic/caps state) |
| `dock/monitors.py`   | CPU/GPU load & temperature (LibreHardwareMonitor via pythonnet) |
| `dock/tapo.py` · `dock/obs.py` · `dock/discord.py` | Smart-bulb / OBS websocket / Discord-voice integrations |
| `dock/apppoller.py`  | Foreground-app watcher for app-aware profile/page switching |
| `dock/appicon.py`    | Resolves an app's real icon (follows `.lnk` shortcuts) |
| `dock/iconart.py`    | Hexpad app-icon art |
| `dock/autostart.py`  | Start-with-Windows (elevated Scheduled Task at logon) |
| `dock/native/`       | LibreHardwareMonitor DLLs (bundled for temperature sensors) |
| `tools/`             | probes, render checks, and test suites (`test_*.py`, `make_icon.py`) |
| `build.ps1` · `install.ps1` · `uninstall.ps1` | package / install / remove |

## Dependencies

`PySide6` (Qt), `hidapi`, `Pillow`, `keyboard` + `mouse`, `pycaw` + `comtypes` (audio/mic),
`sounddevice` + `soundfile`, `psutil`, `python-kasa` (Tapo), `websocket-client` (OBS), and the
`winrt-*` packages (now-playing media). Temperature sensors use the bundled LibreHardwareMonitor
DLLs via `pythonnet`. Windows-only.
