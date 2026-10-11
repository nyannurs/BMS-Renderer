# BMS Renderer — Architecture Overview

Developer documentation for contributors. Start here, then read `PUBLIC_02_ENGINE`, `PUBLIC_03_GUI`, `PUBLIC_04_PLAYER_DISCORD_CONTRACTS`, and `PUBLIC_05_DEVELOPMENT`.

**Version:** defined by `APP_VERSION` in `bms_core.py` (single source of truth). Entry point: `python bms_renderer_qt.py`.

## What it does
BMS Renderer manages a large library of BMS charts. Each song lives in its own folder with one or more chart files plus the keysound audio and background-animation (BGA) images it references. The app scans the library into a SQLite cache, lets you browse/search/preview charts, plays a chart in-app by rendering it to audio on the fly, renders charts offline to tagged FLAC/WAV/OGG/MP3, and optionally renders the chart's background animation (BGA) to video with the audio muxed in — either by encoding an image sequence frame by frame, or, when the BGA *is* a video file, by muxing that existing video with the rendered audio (optionally as a lossless stream copy).

## File map
| File | Role |
|------|------|
| `bms_core.py` | **Engine.** Pure logic, no GUI, no audio device. BMS parsing, audio rendering, BGA rendering, tagging, scanning, caching, persistence, difficulty-table fetching. Unit-tested in isolation. |
| `bms_renderer_qt.py` | **PySide6 GUI.** Main window, dialogs, custom widgets, background workers, and the entry point. |
| `player.py` | **Audio playback.** Wraps sounddevice/PortAudio in a feeder-thread streaming model. |
| `discord_rpc.py` | **Optional** Discord Rich Presence. The app runs fine without it. |
| `test_bms_core.py` | Unit tests for the engine (`python3 -m unittest test_bms_core`). |

## Architecture
- **The engine (`bms_core.py`) never imports Qt and never touches an audio device.** It only produces data — parsed charts, numpy audio buffers, and files on disk. This keeps it independently testable.
- The GUI keeps the main thread free: scanning, offline rendering, BGA detection, thumbnail loading, table fetching, and the per-play render all run on `QThread` workers that call the engine and emit signals back.
- **Offline renders run in separate processes** (`ProcessPoolExecutor`). The per-song job functions (`render_one_job`, `render_bga_video_job`, `render_bga_muxed_job`) are top-level functions so they can be pickled and shipped to worker processes; each process re-imports the engine and re-establishes the library root.
- **In-app playback** is separate: a worker renders one chart to a numpy buffer, which `player.Player` streams to the speakers.
- **Render progress crosses the process boundary by file.** Because jobs run in separate processes, each job writes its own 0–1 completion fraction to a small file in a temporary directory, and the progress dialog polls that directory. This is what lets a single slow encode show real movement instead of jumping from 0% to 100% when it finishes.

## The BMS format (essentials)
A BMS file is a text file (usually Shift-JIS) of `#`-prefixed commands. Header commands define metadata and resource tables (`#TITLE`, `#WAVxx file`, `#BMPxx file`, `#BPMxx`, `#STOPxx`, `#LNOBJ`, and `#RANDOM/#IF/#SWITCH/#CASE` control flow). Channel commands place objects on the timeline as `#MMMCC:payload`, where the payload is a run of 2-char base-36 objects and the measure is divided into N = payload-length/2 equal slots (`00` = empty).

Channels the engine handles: `01` BGM (always sounds), `02` measure-length ratio, `03` inline BPM, `04` BGA base layer, `06` poor layer (ignored), `07` BGA overlay (composited over `04` with black treated as transparent), `08` extended BPM, `09` STOP (freezes time), player-lane note channels (which also sound their keysound), and long-note channels (`51`–`5Z`/`61`–`6Z`, where only the start sounds).

Two non-obvious rules the engine implements: `#LNOBJ`-marked lane notes are silent (they terminate a long note); and when the same keysound appears on both the BGM channel and a player lane at the exact same instant, it is played once rather than twice (in a real game the player either hits the note or it autoplays, never both — so summing both copies would double the volume).

## Timing model
`render_bms` and `bga_timeline` share one time accumulator so audio and BGA stay in sync. Per slot, the sample cursor advances `(SR*60/bpm)/(resolution/4)*ratio`, where resolution is the per-measure least-common-multiple of channel object counts and `ratio` is the channel-`02` measure length. BPM changes (03/08) update the rate mid-measure; STOP (09) freezes the cursor. `SR = 44100`. The model follows the bmx2wav renderer; cross-check the BMS command spec (`https://hitkey.nekokan.dyndns.info/cmds.htm`) and reference implementations (bmx2wav, beatoraja, jbms-parser) before changing format behaviour.

## The library-root guard
The engine holds a module-global library root (`set_library_root`). It serves two purposes: `assert_safe_output` raises if a render would write *into* the library (renders must never modify the library), and because renders run in separate processes, each job re-establishes the root from its job tuple. Any new render job must do the same.

## Persistence
All under the program directory: `bms_cache.db` (SQLite metadata cache), `bms_config.json`, `tables.json`, and `Playlists/` (one JSON file per playlist). Config keys include `library`, `output`, `render_threads`, `lib_col_widths`, `lib_sort`, `window_geometry_qt`, `dark_mode`, `art_viewer_scaling`, `art_viewer_geometry`, `shuffle`, `loop`, `nowplaying_output`, `nowplaying_format`, and `song_visualizer`.

## Conventions
The engine stays Qt-free and audio-device-free. Render job functions stay picklable and top-level. UI uses native Qt icons (`QStyle.SP_*`), not emoji. Encode/quality options are designed so that unchanged settings reproduce the exact prior output — when adding an option, preserve this.

## Known limitation
The scanner recognises `.bms`, `.bme`, `.bml`, and `.pms` only. **`.bmson`** (the modern JSON-based variant) is not scanned or parsed, so a library containing bmson charts will report a lower count than expected. Supporting it would require a separate parser, since bmson is a different format.
