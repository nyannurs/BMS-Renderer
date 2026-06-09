"""
bms_core.py -- pure logic for BMS Renderer (no GUI).

Everything here is importable and testable without Tkinter, audio hardware, or a
display: the BMS parser, the render engine, the SQLite cache, config, playlists,
difficulty-table fetching, tag writing, and the parallel-render worker. The GUI
application (bms_renderer.py) imports from this module, so there is a single
source of truth shared by the app and the test suite.
"""

import os, re, sys, json, time, io, threading, traceback
from math import gcd
import numpy as np
import soundfile as sf
from mutagen.flac import FLAC, Picture
try:
    from PIL import Image
    _PIL_OK = True
except Exception:
    _PIL_OK = False
try:
    from scipy.signal import resample_poly
    _SCIPY_OK = True
except Exception:
    _SCIPY_OK = False
try:
    from player import Player, SD_OK as _SD_OK
except Exception:
    Player = None
    _SD_OK = False

SR = 44100
BMS_EXTS = (".bms", ".bme", ".bml", ".pms")
AUDIO_EXTS = (".ogg", ".wav", ".flac", ".mp3", ".aiff", ".aif")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")

def list_folder_images(folder, include_bmp=False):
    """Return image file paths in `folder`, sorted by name. When include_bmp is
    False, .bmp files are skipped (BGA frame-bmps can number in the hundreds)."""
    exts = IMAGE_EXTS if include_bmp else tuple(e for e in IMAGE_EXTS if e != ".bmp")
    if not folder:
        return []
    folder = os.path.normpath(folder)   # tidy separators / relative bits
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    for n in names:
        full = os.path.join(folder, n)
        if n.lower().endswith(exts) and os.path.isfile(full):
            out.append(full)
    return out

# Embedded window icon (64px PNG, base64). No external file needed.
_ICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAF/klEQVR4nO2bXW9URRjHfzPnbLdb"
    "SjEYG4hiIIItFCEWApd6peAdHwES9UrRcAExASsk3nFlhBh5SfgOgPGWqxIsAi1tQ01ETCA1ELus"
    "lN0954wXM7Pn9LSldDtn+7L8k8lmz5zzzPN/5v2ZeQTuIE0KEs9ywHZgD/A+0AO8BbwOrE59/xR4"
    "DDwA7gI3gX5gCKgm3vOByKQlAQl4if8F4BPgLFr5CqDqTBUj46yR2ZYoxzNlLxoEU4lvAk4CY0wn"
    "EqJbRoiuudkIR6l30/ljpoxNiXI9o0tDkbT8RuBHoMhUwlXmJjxXilKy7POiKXPjLDplCt/85oBj"
    "wJOEYmlFXSdrDPv/idEhl9ItEySb/F7gOlOJL6Sm62kZSUNcNzpBRl1CJIR+DZQXifiLDFE2uqX1"
    "XTBs3/KBc4mCg0Uknk4BcUWcI+4KCx4XrIA24DJLo9ZfpjVcJp4y6zaCRDejNuCaEbyQOb1Ryep4"
    "zegu6jGCHfA84MoyIp82wpUEj3mNCbYPnV+G5NNGOJ/iNCfsVHd4GZNPG+FwitussH1ll/k4Obou"
    "x2RnqwrQm+I4DbbftwC/GwFZruoalSyHm4bbrOOBbR7HzAfJldZyT5bL0RTXGuyU9yYwwcI3MUst"
    "2U3VhOE4bWq0FjmTsthKSpbTmRTnmiU2ACVWXu2nW0HJcAWQ1o0F8BmwyrzYcAdDAyDQ3FahuYLh"
    "LtBurPtoSzVg5BdKCKmE8EySCkQjWoHl9ifQSqKi9zWCvCU9e741RkOMsA/i5eEB4n6SiWtJCA+l"
    "QgB8v52O9i7y+TcAKJf/oVgaJQhK097NALaLHwB+8dGLgw+pc9c0NwRCgFIhr63ZSdc7X7Cu8yNW"
    "FTboEQiIIvhv8gGPxn9l9I8f+HfiFkIIlAJdL05hp/wPgBaB9tf3E/vVHEIYIhHbu7/lva3fkPNb"
    "CEKIIoXSDBFCIKXA96AaVLgz/D2DI98hhDTvODcC6CXyHon2peVwftAgDIGIvb0X2L2zD6VylCsB"
    "UaRq+ULoCokiRbkSoFSO3Tv72Nt7AaWiWr5jROjuv0eiWwA4NrMmH9LTdYKtWw7ybLJinvvMTEiY"
    "PHg2WWHrloP0dJ1AqdAYwSks110S2FbTwBEs+TUdPezYdpzn5RApcy9ZhEDKHM/LITu2HWdNR08W"
    "RrCKdEviVZHDdqZFdW8+Qs73TT+ej3iBUoqc79O9+Yh79WJhb0tgrUvJWvkQ329nfed+gpC6ak8I"
    "SRDC+s79+H67mRadjwVrJfEprRPplmxHexeFwjoz4NXTfCVRpCgU1tHR3jVFtgs1ze/qzM7T8vlO"
    "PI/aVFcPlFJ4npaVFRb1eHkpIDMDlMvjhKFe5NQLIQRhqGVlBYm+mQGO1gFK6fVUsTTK5OQjpLS7"
    "0PkiQkrB5OQjiqXRKbJdqGl+n0r0EbNDKITwCIISD8ev4nv1Ka5UhO/Bw/GrBEEJITwyWBI/lsBf"
    "tkx3crWokbHTVIPAdIP5iFcIIagGASNjp92rFwv7WwIjrkvQa3iPieIQt++eojXvEUXWJTe3blFU"
    "pTXvcfvuKSaKQ2Z77HSrYhUZlsCA+eN0lWGNMDR6kuF7F2krtJjn9lR9uk46D9oKLQzfu8jQ6Mks"
    "yEPM9TeJ3gpXcT4jqNpurn/gEDdu9SFElXyLbwZGna/JKaQU5Ft8hKhy41Yf/QOHarvJjHwCAXBd"
    "oB0id4B3ycQjZB0iaqk4RCzHUWCHbQo/AZ+i/WWZXDRaQi6xAH0m8DPwuX34MU3qFAU9ILTSXG7x"
    "+4azsNdcnwOXjEEacAfXDoChSZkMdDPBcruE5uzBq6MxImOJB8Yy0ry40hCiuV1Cc/VItPamPx6H"
    "+Lj4qPloJR2Rz3lBAqZekblpPmiqKzIQN4temvCSlMVKvCb3ZYrbnGjqi5Lw6qos0OSXpZNGgCa9"
    "Lp82QlMGTFgsl5CZr2bQ1xmaOmgqiaYNm0uiqQMnLRYjdPYeSyR0NokXBU8PsvDg6UHi4OlCohwn"
    "wdMuLTdT+LxPHD7fi76PtIGXC58fQA9ugzPIdBY+/z8cfXb0bb4G9QAAAABJRU5ErkJggg=="
)

import shutil as _shutil
def ffmpeg_path():
    """Return the path to ffmpeg if available on PATH, else None.
    OGG export uses ffmpeg to avoid libsndfile's unreliable Vorbis encoder."""
    return _shutil.which("ffmpeg")

def _no_window_kwargs():
    """Return subprocess kwargs that prevent a console window from flashing on
    Windows (e.g. when calling ffmpeg). No-op on other platforms."""
    if sys.platform == "win32":
        import subprocess
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        # CREATE_NO_WINDOW also keeps console subprocesses fully hidden
        return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}
    return {}

# Cache lives next to the program, NEVER in the library.
# (sys.argv[0] is this script; if frozen to .exe, sys.executable's folder.)
def program_dir():
    # Resolve the real folder this program lives in, independent of the
    # current working directory (which can differ when double-clicked or
    # launched from another folder). realpath also resolves any symlinks.
    if getattr(sys, "frozen", False):           # packaged .exe
        return os.path.dirname(os.path.realpath(sys.executable))
    try:
        return os.path.dirname(os.path.realpath(__file__))
    except NameError:
        return os.path.dirname(os.path.realpath(sys.argv[0]))

CACHE_PATH = os.path.join(program_dir(), "bms_cache.json")   # legacy, removed on migrate
DB_PATH = os.path.join(program_dir(), "bms_cache.db")
CONFIG_PATH = os.path.join(program_dir(), "bms_config.json")
TABLES_PATH = os.path.join(program_dir(), "tables.json")
PLAYLISTS_PATH = os.path.join(program_dir(), "playlists.json")   # legacy, migrated
PLAYLISTS_DIR = os.path.join(program_dir(), "Playlists")

APP_VERSION = "1.5.4"
CHANGELOG = [
    ("1.5.4", "Fixed a serious bug where adding a song to the queue from the Playlists "
              "tab could queue the WRONG song (whatever was selected over on the "
              "Library tab) — the add-to-queue action now always uses the song you "
              "actually right-clicked. Also fixed the song art resetting to the first "
              "image when you right-click a song to queue it: right-clicking the "
              "already-selected row no longer reloads the picker."),
    ("1.5.3", "Fixed the song folder art selection being lost when adding a song to "
              "the queue: the image you've scrolled to in the picker is now carried "
              "over as that song's cover when you queue it, instead of resetting to "
              "the first image."),
    ("1.5.2", "Selecting a song in the Custom Playlists tab now updates the right-"
              "hand panels (BMS information, tags preview, and the song folder art "
              "picker) like the other tabs do — previously the playlist tab had no "
              "selection handler, so those panels (including album art) didn't "
              "respond there."),
    ("1.5.1", "Fixed column-header sorting in the Custom Playlists and Tables tabs — "
              "the headers weren't wired up before (clicking them did nothing). "
              "Click a header to sort; click again to reverse. In Tables, songs sort "
              "within each level group (the level grouping is preserved)."),
    ("1.5.0", "You can now click and hold the ◀ ▶ art arrows to speed through a "
              "folder's images — a single click still steps one at a time, but "
              "holding ramps up and races through them (handy for folders with "
              "hundreds of BGA frames)."),
    ("1.4.2", "Fixed the art-picker buttons still shifting as you scrolled images: "
              "the arrows now sit in a fixed-size centered group and the status line "
              "(with the filename) can no longer change the panel width. Long "
              "filenames are shortened in the status display."),
    ("1.4.1", "Internal cleanup: removed code left orphaned by recent reworks (a "
              "duplicate art-reload method, an unused helper, and two dead "
              "constants) and cached the blank art-preview image instead of "
              "rebuilding it each time. No behavior changes."),
    ("1.4.0", "BMS information fields are now selectable/copyable (read-only text "
              "boxes, like the library/output paths) so you can copy a title, MD5, "
              "etc. Added a 'Go to #' box to the song folder art picker: type an "
              "image number and press Enter to jump straight to it, handy for folders "
              "with lots of pictures."),
    ("1.3.3", "Song folder art preview is now a fixed 1:1 square (images are scaled "
              "and letterboxed to fit), so the ◀ ▶ buttons no longer jump around as "
              "you scroll through differently-sized images. You can now scroll the "
              "preview with the arrows on any tab, not just the Queue."),
    ("1.3.2", "The song folder art preview now shows on every tab (Library, Tables, "
              "Queue, playlists), so you can always see a song's folder images — "
              "previously it only worked on the Queue and looked empty everywhere "
              "else. Default window size 1600x1000."),
    ("1.3.0", "New per-song art picker (between BMS information and the whole-queue "
              "album art): for a queued song, scroll through the images in that "
              "song's own folder with ◀ ▶ and whatever's shown becomes its cover. "
              "Has an 'ignore .bmp' toggle (on by default, to skip BGA frame-bmps). "
              "Global whole-queue art still wins when set; the per-song picker is "
              "only used when there's no global art."),
    ("1.2.2", "Hid the console window that briefly flashed when ffmpeg ran (OGG / "
              "level-8 FLAC encoding) — no more black boxes popping up during a "
              "render."),
    ("1.2.1", "The custom window icon is now embedded directly in the program (no "
              "icon file needed in the folder)."),
    ("1.2.0", "Custom window/taskbar icon (replaces the default Tk feather). The app "
              "loads bms_icon.ico / bms_icon.png from its own folder; if they're "
              "missing it just falls back to the default with no error."),
    ("1.1.0", "Multi-select: select several charts in the Library (Ctrl/Shift-click) "
              "and right-click → Add to Queue, or use the 'Add selected to Queue' "
              "button, to queue them all at once (duplicates skipped). Right-clicking "
              "keeps your multi-selection instead of collapsing it to one row."),
    ("1.0.0", "First stable release. Packaged as a standalone Windows .exe (no Python "
              "install needed) via PyInstaller. Core functionality and full feature "
              "set verified through a manual test pass plus the automated suite. "
              "Everything from the 0.x line — library, queue, difficulty tables, "
              "portable playlists, tagged FLAC/WAV/OGG export, in-app playback with "
              "shuffle/loop/skip and near-gapless advance, parallel batch rendering — "
              "is included."),
    ("0.27.0", "Internal restructure for testability: all non-GUI logic (parser, "
               "render engine, cache, config, playlists, tables, safety guard) now "
               "lives in bms_core.py, which the app imports — one shared source of "
               "truth. Added an automated test suite (test_bms_core.py, 47 tests) "
               "covering that logic, plus a manual test checklist for the GUI/audio/"
               "Windows parts. No behavior or feature changes; bms_core.py and "
               "player.py must sit next to bms_renderer.py."),
    ("0.26.0", "Internal performance pass on the render engine: channel range "
               "constants are computed once instead of on every note, and each "
               "channel in a bar is classified a single time rather than re-checked "
               "for every subdivision — faster rendering of dense charts, with "
               "bit-identical output (verified). Removed code orphaned by the "
               "refactor and refreshed a couple of stale comments. No behavior "
               "changes."),
    ("0.25.0", "Loop now repeats the current song (was a no-op before). Shuffle "
               "follows the selection into view. Added Prev/Next (skip) buttons. "
               "Transport buttons are now icon-only (⏮ ▶/⏸ ■ ⏭). New 'Add table by "
               "URL…' button fetches the table's own name automatically and saves it "
               "to tables.json — no manual naming needed."),
    ("0.24.0", "Added Shuffle and Loop toggles at the bottom (left of the volume "
               "slider); they apply to Queue, playlists, and Tables — not the Library. "
               "Table playback now auto-advances down the list like the Queue, with "
               "the same near-gapless pre-rendering. Replaced the volume emoji with a "
               "plain 'Vol:' label to match the app's ASCII style."),
    ("0.23.0", "Performance pass. Near-gapless playback: the previous and next song in "
               "a queue/playlist are pre-rendered in the background while the current "
               "one plays, so moving between them is instant (bounded in-memory cache, "
               "one ahead + one back). Faster rendering overall — the mix step is now "
               "vectorized, and only the keysounds a chart actually plays are decoded "
               "(unused #WAV defs are skipped). Fixed sluggish text-caret/selection in "
               "the Tags fields (the timeline updater no longer does any work while "
               "idle). Minor internal cleanups."),
    ("0.22.2", "Tag fields are now greyed out (read-only) when viewing Library or "
               "Tables charts, since edits only save for queued songs — making it "
               "clear where editing actually takes effect."),
    ("0.22.1", "Threads/Format controls now read left-to-right in the right order. "
               "The window remembers its size/position between launches. Faster "
               "first-play playback: keysounds for a song now decode in parallel."),
    ("0.22.0", "Batch rendering now runs in parallel across multiple worker processes "
               "(big speedup on large queues/playlists). A 'Threads' control next to "
               "the format dropdown sets how many to use (default = CPU count), saved "
               "in bms_config.json as 'render_threads'. The now-playing [♪] marker now "
               "shows on the Tables tab too. Right-click a level header in a table → "
               "'Add all in this level to Queue' (one per song, lowest notecount)."),
    ("0.21.0", "Tables tab: 'Add all owned to Queue' button — queues every owned chart "
               "in the table, one per song (one per folder, keeping the lowest-"
               "notecount chart), skipping anything already queued."),
    ("0.20.1", "Actually fixed Tables playback: double-click now resolves the chart "
               "under the cursor (it was reading a not-yet-updated selection), and the "
               "Play button now correctly plays the Tables selection instead of the "
               "Library one (tab detection now compares tab indices, not widget "
               "pathnames)."),
    ("0.20.0", "Playlists are now stored one JSON file per playlist in a 'Playlists' "
               "folder (auto-created) for easy sharing — hand someone a single file. "
               "Old playlists.json migrates automatically. Removed unused code "
               "(keysound fingerprint/overlap, the same-song threshold, and other "
               "dead functions); the keysound column is dropped from the cache, which "
               "rebuilds automatically on next launch."),
    ("0.19.2", "Playback now works from the Tables tab too: double-click an owned "
               "chart to play it, and the Play button plays the selected table chart."),
    ("0.19.1", "FLAC now encodes at maximum compression (level 8) via ffmpeg when "
               "ffmpeg is on PATH (lossless, ~10-20% smaller files); falls back to "
               "soundfile's level 5 when ffmpeg isn't present."),
    ("0.19.0", "Play button now starts the selected song (not just double-click). "
               "'Show all charts' groups by folder instead of keysounds. Now-playing "
               "title scrolls (marquee) when too long. OGG export is back, via ffmpeg "
               "(reliable, unlike the old libsndfile path) — appears in the format "
               "dropdown only when ffmpeg is on PATH. Added a redraw nudge to help a "
               "Tk-on-Linux/Wayland repaint quirk."),
    ("0.18.1", "Reduced the visual 'rebuild' flicker when restoring the window from "
               "minimized (timeline updates pause while minimized so they don't queue "
               "up and flush noisily on restore)."),
    ("0.18.0", "Playlists are now portable: each song is stored by MD5 hash + a "
               "title/artist label instead of a file path, so a shared playlists.json "
               "resolves to whatever paths the recipient has. Charts the recipient "
               "doesn't own show greyed with their label. Existing path-based "
               "playlists auto-migrate to hash form."),
    ("0.17.0", "New 'Custom Playlists' tab (between Tables and Queue): create locally "
               "stored playlists that reference charts by path (no file copies). "
               "Right-click any song → Add to playlist. Double-click a playlist song "
               "to play (auto-advances). 'Render playlist' renders into a subfolder of "
               "the output path named after the playlist. Added a playback volume "
               "slider at the bottom-right (affects playback only, not exported files)."),
    ("0.16.0", "Cache moved from one big JSON file to a SQLite database "
               "(bms_cache.db) — faster loads, smaller footprint, no full-file parse "
               "on launch. Old bms_cache.json is removed automatically. Raised the "
               "minimum window size so the right panel never clips."),
    ("0.15.1", "Fixed transport controls getting clipped when the window was made "
               "short: the log and transport bar are now anchored to the bottom and "
               "keep their space, with the main area shrinking instead; added a "
               "minimum window size."),
    ("0.15.0", "Removed OGG export (libsndfile Vorbis encoding was unreliable / "
               "produced corrupt files on some systems) — FLAC and WAV remain. "
               "Library and output paths are now selectable, full-width fields. "
               "Fixed play/stop buttons rendering inconsistently (single state-driven "
               "label, wider button)."),
    ("0.14.0", "Much smaller cache (keysounds stored as compact int hashes). "
               "Same-song threshold now configurable in bms_config.json "
               "(default 0.95). 'Exit lookup' button leaves show-all mode. Tables "
               "auto-load when picked from the dropdown (no Fetch button). Export "
               "format dropdown (FLAC / WAV / OGG) next to Render All."),
    ("0.13.0", "Right-click menu now on Library and Queue too (Add to Queue greyed "
               "out on the Queue tab). New 'BMS information' section under Tags "
               "(Title/Artist/Genre/BPM/Notes/Play type/File/MD5); Notes moved there. "
               "Uniform, de-bolded panel fonts. App now uses Meiryo UI for clean "
               "Japanese text. Right panel order: Tags, BMS information, Album art."),
    ("0.12.0", "Playback now on double-click (single-click just selects/shows tags). "
               "Tab order is Library, Tables, Queue. Now-playing icon shown as [♪]. "
               "Table levels are collapsible dropdown groups for faster navigation."),
    ("0.11.1", "Fix: restored a method header accidentally dropped in 0.11.0 that "
               "prevented the app from starting."),
    ("0.11.0", "Difficulty Tables tab: add table URLs to tables.json, fetch them "
               "(BMS-table standard, matched by MD5), and see your charts grouped by "
               "level with owned vs missing (missing shown grey). Right-click a chart "
               "for Play / Add to Queue / Show all charts (same-song variants matched "
               "by keysound overlap). MD5 + keysound fingerprint now cached per chart."),
    ("0.10.0", "Native playback: click a song to render+play it. Transport bar under "
               "the log (play/pause/stop + draggable seek, no scrub preview). ♪ marks "
               "the now-playing row. Library plays to end then stops; Queue auto-"
               "advances to the next song. Needs the 'sounddevice' library."),
    ("0.9.2", "Config JSON is now pretty-printed. Album art is session-only: it is "
              "no longer saved to config and resets each time the app starts."),
    ("0.9.1", "Moved album-art controls into the Tags panel (right side, below the "
              "tag fields)."),
    ("0.9.0", "High-quality polyphase resampling (scipy) for keysounds whose sample "
              "rate isn't 44.1kHz — objectively cleaner than the old nearest-sample "
              "method and than bmx2wav's linear interpolation. Falls back to linear "
              "interpolation if scipy is absent."),
    ("0.8.0", "Album art: choose one cover for the whole queue; auto-converted to a "
              "safe square-ish JPEG (≤1000px, ≤500KB) and embedded in every FLAC. "
              "Remembered in config. Requires Pillow."),
    ("0.7.0", "Charts containing #RANDOM are shown in red in the Library list "
              "(not yet evaluated at render time — flag only)."),
    ("0.6.0", "Playable note-count column in Library and Queue, shown for the "
              "selected chart (helps pick the simplest chart for a clean render). "
              "Charts now parsed once per scan (faster). Sortable by clicking the "
              "Notes column header."),
    ("0.5.1", "Fixed hard UI freezes: all scan/render work now updates the UI only "
              "from the main thread; library list draws in small batches with a "
              "display cap; per-keystroke queue rebuild removed."),
    ("0.5.0", "Library/Queue tabs, per-song tag panels, Render All (removes each "
              "when done), remembered output folder, Album tag pinned to 'BMS'."),
    ("0.4.0", "Engine overhaul from bmx2wav reference: correct base-36 channel "
              "handling, per-measure resolution, mid-song BPM changes (ch 03/08), "
              "STOP sequences (ch 09 + #STOPxx), measure-length (ch 02), long notes "
              "(51-6Z + #LNOBJ), landmine suppression (D/E), robust error handling."),
    ("0.3.0", "Auto-rescan on launch (remembers library), version label, "
              "scrollbar, fast search, 7K-SP / play-type filtering."),
    ("0.2.0", "Library cache (path+size+mtime) for fast relaunch + incremental scan."),
    ("0.1.0", "Initial: scan, browse, auto-fill+edit tags, render to FLAC, "
              "read-only library protection."),
]

# ============================================================================
#  LIBRARY PROTECTION  --  this section guarantees the library is never written
# ============================================================================
#
#  Two rules, enforced in code:
#    1. Every file inside the library is opened READ-ONLY ("rb"). There is no
#       code path anywhere in this program that opens a library file for writing.
#    2. Before ANY file is written (the FLAC export), assert_safe_output() checks
#       that the destination is NOT inside the library root. If it is, the write
#       is refused and nothing happens to your library.
#
#  These are belt-and-suspenders: even a future bug or a mis-click in the save
#  dialog cannot land a write inside the protected folder.

_LIBRARY_ROOT = None   # set once when you choose your library folder

def set_library_root(path):
    global _LIBRARY_ROOT
    _LIBRARY_ROOT = os.path.realpath(path) if path else None

def _is_inside(child, parent):
    """True if `child` is the same as, or nested under, `parent`. Case-insensitive
    on Windows. Uses realpath so symlinks/.. tricks can't slip past."""
    if not parent:
        return False
    c = os.path.realpath(child)
    p = os.path.realpath(parent)
    if os.name == "nt":
        c, p = c.lower(), p.lower()
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False  # different drives -> definitely not inside

def open_readonly(path):
    """The ONLY way this program opens a library file. Read-only, always."""
    return open(path, "rb")

def assert_safe_output(out_path):
    """Refuse to write anywhere inside the protected library. Raises on violation."""
    if _is_inside(out_path, _LIBRARY_ROOT):
        raise PermissionError(
            "BLOCKED: the output path is inside your read-only library.\n"
            f"  library : {_LIBRARY_ROOT}\n  output  : {os.path.realpath(out_path)}\n"
            "Choose an export location OUTSIDE your library. Nothing was written."
        )

# ---------------------------------------------------------------- BMS parsing

def b36(s):
    return int(s, 36)

def read_bms_text(path):
    with open_readonly(path) as f:
        data = f.read()
    for enc in ("shift_jis", "cp932", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")

def parse_bms(path):
    """Parse a BMS chart. Returns a dict with:
      header   : generic #KEY value pairs (TITLE, ARTIST, BPM, ...)
      wav_table: wav-id(int) -> filename
      bpm_table: id(int) -> float  (from #BPMxx, for channel 08)
      stop_table:id(int) -> float  (from #STOPxx, for channel 09)
      lnobj    : set of wav-ids that terminate a long note (silent)
      bars     : measure(int) -> { channel_str -> [payload, ...] }
    Channels are kept as their raw 2-char base-36 strings ('01','11','5A',...).
    """
    header, wav_table = {}, {}
    bpm_table, stop_table = {}, {}
    lnobj = set()
    bars = {}
    has_random = False

    for line in read_bms_text(path).splitlines():
        line = line.strip()
        if not line.startswith("#"):
            continue
        body = line[1:]

        # flag the #RANDOM control family (we don't evaluate it yet, just detect it)
        up = body.upper()
        if (up.startswith("RANDOM") or up.startswith("SETRANDOM") or
                up.startswith("IF") or up.startswith("ELSEIF") or
                up.startswith("ENDIF") or up.startswith("ENDRANDOM")):
            has_random = True

        # channel data: mmmCC:payload  (mmm=measure base-10, CC=channel base-36)
        m = re.match(r"(\d{3})([0-9A-Za-z]{2}):(.*)", body)
        if m:
            measure = int(m.group(1))
            chan = m.group(2).upper()
            payload = m.group(3).strip()
            bars.setdefault(measure, {}).setdefault(chan, []).append(payload)
            continue

        # indexed header arrays: WAVxx / BPMxx / STOPxx
        m = re.match(r"(WAV|BPM|STOP)([0-9A-Za-z]{2})\s+(.+)", body, re.IGNORECASE)
        if m:
            kind = m.group(1).upper()
            idx = b36(m.group(2).upper())
            val = m.group(3).strip()
            if kind == "WAV":
                wav_table[idx] = val
            elif kind == "BPM":
                try: bpm_table[idx] = float(val)
                except ValueError: pass
            elif kind == "STOP":
                try: stop_table[idx] = float(val)
                except ValueError: pass
            continue

        # generic header: KEY value
        m = re.match(r"([A-Za-z]+)\s+(.+)", body)
        if m:
            key = m.group(1).upper()
            val = m.group(2).strip()
            header[key] = val
            if key == "LNOBJ":
                try: lnobj.add(b36(val.upper()))
                except ValueError: pass
            continue

    return {"header": header, "wav_table": wav_table, "bpm_table": bpm_table,
            "stop_table": stop_table, "lnobj": lnobj, "bars": bars,
            "is_pms": path.lower().endswith(".pms"), "has_random": has_random}

# ---- channel classification (mirrors the bmx2wav reference) ----
def _ch(s):  # base-36 value of a 2-char channel string
    return int(s, 36)

def _num(v):
    """Coerce a possibly-blank, possibly-string value to a float for sorting.
    Blanks sort as -1 so empty BPM/notes group at the bottom ascending."""
    try:
        return float(v)
    except (ValueError, TypeError):
        return -1.0

_C_BGM       = _ch("01")
_C_BPM       = _ch("03")   # inline hex BPM
_C_BPM_EXT   = _ch("08")   # extended BPM (references #BPMxx)
_C_STOP      = _ch("09")   # stop sequence (references #STOPxx)

# Channel-range boundaries, computed once (these were previously recomputed on
# every predicate call, inside the per-note render loop). All inclusive ranges.
_C_P1_LO, _C_P1_HI = _ch("11"), _ch("1Z")   # player-1 visible lanes
_C_P2_LO, _C_P2_HI = _ch("21"), _ch("2Z")   # player-2 visible lanes
_C_LN1_LO, _C_LN1_HI = _ch("51"), _ch("5Z") # player-1 long notes
_C_LN2_LO, _C_LN2_HI = _ch("61"), _ch("6Z") # player-2 long notes
_C_MINE1_LO, _C_MINE1_HI = _ch("D1"), _ch("DZ")  # player-1 landmines
_C_MINE2_LO, _C_MINE2_HI = _ch("E1"), _ch("EZ")  # player-2 landmines

def ch_is_long_note(c):
    v = _ch(c)
    return _C_LN1_LO <= v <= _C_LN1_HI or _C_LN2_LO <= v <= _C_LN2_HI

def ch_is_player_note(c):
    v = _ch(c)
    return (_C_P1_LO <= v <= _C_P1_HI or _C_P2_LO <= v <= _C_P2_HI or
            _C_LN1_LO <= v <= _C_LN1_HI or _C_LN2_LO <= v <= _C_LN2_HI)

def _classify_channel(c):
    """Classify a channel once into the single category the render loop acts on,
    using the SAME precedence the per-subdivision predicate chain used:
      landmine > inline-BPM > ext-BPM > stop > long-note > player-note > BGM.
    Returns one of 'mine','bpm_inline','bpm_ext','stop','long','note','bgm', or
    None for channels the renderer ignores (visual/BGA/etc)."""
    try:
        v = _ch(c)
    except ValueError:
        return None
    if _C_MINE1_LO <= v <= _C_MINE1_HI or _C_MINE2_LO <= v <= _C_MINE2_HI:
        return "mine"
    if v == _C_BPM:
        return "bpm_inline"
    if v == _C_BPM_EXT:
        return "bpm_ext"
    if v == _C_STOP:
        return "stop"
    if _C_LN1_LO <= v <= _C_LN1_HI or _C_LN2_LO <= v <= _C_LN2_HI:
        return "long"            # long-note channels (start sounds, end muted)
    if _C_P1_LO <= v <= _C_P1_HI or _C_P2_LO <= v <= _C_P2_HI:
        return "note"            # visible player lanes (subject to #LNOBJ muting)
    if v == _C_BGM:
        return "bgm"             # autoplay BGM lane
    return None

def count_playable_notes(parsed):
    """Count playable notes the way difficulty is usually rated:
       objects in player lanes (11-1Z, 21-2Z) + long-note channels (51-5Z, 61-6Z),
       excluding BGM/autoplay and control channels. Long notes count as ONE note
       per head/tail pair, and #LNOBJ terminators are not counted.
       `parsed` is the dict returned by parse_bms()."""
    bars = parsed["bars"]
    lnobj = parsed.get("lnobj", set())
    total = 0
    ln_open = {}  # channel -> bool, tracks an open long note so we count the pair once
    for measure in sorted(bars.keys()):
        bar = bars[measure]
        for chan, payloads in bar.items():
            if not ch_is_player_note(chan):
                continue
            is_ln = ch_is_long_note(chan)
            for payload in payloads:
                pairs = [payload[i:i+2] for i in range(0, len(payload) - len(payload) % 2, 2)]
                for pair in pairs:
                    if pair == "00" or pair == "":
                        continue
                    try:
                        wid = b36(pair.upper())
                    except ValueError:
                        continue
                    if is_ln:
                        # head starts the note (count it); tail closes it (don't double-count)
                        if not ln_open.get(chan):
                            ln_open[chan] = True
                            total += 1
                        else:
                            ln_open[chan] = False
                    else:
                        if wid in lnobj:
                            continue  # LNOBJ terminator: not a separate note
                        total += 1
    return total

def detect_mode_from_bars(parsed):
    """Infer play type from the parsed chart's channel usage.
    Returns '7K SP', '5K SP', 'DP', 'PMS', or '?'.
    Player-1 keys live in 11-19 (18/19 = keys 6/7) with LN variants 51-59;
    player-2 side 21-29 / 61-69 means double play."""
    if parsed.get("is_pms"):
        return "PMS"
    p1 = set(); p2 = False
    for chs in parsed["bars"].values():
        for ch in chs:
            if ch in ("11","12","13","14","15","16","18","19",
                      "51","52","53","54","55","56","58","59"):
                p1.add(ch)
            if ch in ("21","22","23","24","25","26","28","29",
                      "61","62","63","64","65","66","68","69"):
                p2 = True
    if p2:
        return "DP"
    if not p1:
        return "?"
    if p1 & {"18","19","58","59"}:
        return "7K SP"
    return "5K SP"


# ============================================================================
#  LIBRARY CACHE  --  fast relaunch via path + size + mtime
# ============================================================================
#
#  The cache stores, per chart: size, mtime, and parsed header fields.
#  On scan we list the library (cheap, no file reads) and only re-parse charts
#  whose size or mtime differ from the cache, or that are brand new. Charts that
#  vanished from disk are dropped. This makes relaunches near-instant and picks
#  up newly added BMS automatically.

import sqlite3

def db_connect():
    """Open (creating if needed) the SQLite cache database, with the charts table.
    If an existing table has an outdated column set, it's dropped and rebuilt."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")      # faster, safer concurrent writes
    conn.execute("PRAGMA synchronous=NORMAL")
    expected = ["path","size","mtime","title","artist","genre","bpm","mode",
                "notes","random","md5"]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(charts)")]
    if cols and cols != expected:
        conn.execute("DROP TABLE charts")        # schema changed → rebuild from scratch
        conn.commit()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS charts (
            path   TEXT PRIMARY KEY,
            size   INTEGER,
            mtime  INTEGER,
            title  TEXT,
            artist TEXT,
            genre  TEXT,
            bpm    TEXT,
            mode   TEXT,
            notes  INTEGER,
            random INTEGER,
            md5    TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_md5 ON charts(md5)")
    conn.commit()
    return conn

def _row_to_song(r):
    """Turn a DB row tuple into the song dict the rest of the app expects."""
    (path, size, mtime, title, artist, genre, bpm, mode, notes, random, md5) = r
    return {"path": path, "title": title or os.path.basename(path),
            "artist": artist or "", "genre": genre or "", "bpm": bpm or "",
            "mode": mode or "?", "notes": notes if notes is not None else "",
            "random": bool(random), "md5": md5 or "",
            "_sig": (size, mtime)}

# Legacy JSON cache is no longer used; if present we just ignore/remove it.
def _migrate_old_cache():
    try:
        if os.path.exists(CACHE_PATH):
            os.remove(CACHE_PATH)
    except OSError:
        pass

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)

def file_md5(path):
    """MD5 of the raw .bms file bytes — the LR2/BMS-standard chart identifier."""
    import hashlib
    h = hashlib.md5()
    with open_readonly(path) as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def load_tables_file():
    """Read tables.json: a list of {name, url} the user is tracking.
    Returns [] if the file is missing or unreadable."""
    try:
        with open(TABLES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tables", []) if isinstance(data, dict) else data
    except Exception:
        return []

def save_tables_file(tables):
    tmp = TABLES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tables": tables}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, TABLES_PATH)

def _safe_playlist_filename(name):
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad else c for c in name).strip()
    return (cleaned or "untitled") + ".json"

def load_playlists():
    """Load all playlists from the Playlists/ folder — one JSON file per playlist,
    each {"name","songs":[{md5,title,artist}, ...]}. Returns {name: [entries]}.
    Also migrates an old single playlists.json (if present) into the folder."""
    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
    playlists = {}
    # one-time migration from the old single-file format
    if os.path.exists(PLAYLISTS_PATH):
        try:
            with open(PLAYLISTS_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
            for name, songs in (old.get("playlists", {}) if isinstance(old, dict) else {}).items():
                playlists[name] = songs
                save_one_playlist(name, songs)
            os.replace(PLAYLISTS_PATH, PLAYLISTS_PATH + ".migrated")
        except Exception:
            pass
    for fn in os.listdir(PLAYLISTS_DIR):
        if not fn.lower().endswith(".json"):
            continue
        try:
            with open(os.path.join(PLAYLISTS_DIR, fn), "r", encoding="utf-8") as f:
                data = json.load(f)
            name = data.get("name") or os.path.splitext(fn)[0]
            playlists[name] = data.get("songs", [])
        except Exception:
            continue
    return playlists

def save_one_playlist(name, songs):
    """Write a single playlist to its own portable JSON file in Playlists/."""
    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
    path = os.path.join(PLAYLISTS_DIR, _safe_playlist_filename(name))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"name": name, "songs": songs}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)

def delete_playlist_file(name):
    path = os.path.join(PLAYLISTS_DIR, _safe_playlist_filename(name))
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass

def _http_get(url, timeout=20):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "BMS-Renderer"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def _resolve(base, ref):
    import urllib.parse
    return urllib.parse.urljoin(base, ref)

def fetch_table(url, log=lambda s: None):
    """Fetch a BMS difficulty table following the bmstable standard.
    Returns {name, symbol, entries:[{md5,level,title,artist,url}]}.
    The page's meta-bmstable points to a header JSON; the header's data_url
    points to the data JSON (an array of chart entries keyed by md5)."""
    import re as _re, json as _json
    header_url = url
    # If given the HTML page, find <meta name="bmstable" content="header.json">
    if not url.lower().endswith(".json"):
        try:
            html = _http_get(url)
            m = _re.search(r'<meta[^>]+name=["\']bmstable["\'][^>]+content=["\']([^"\']+)["\']',
                           html, _re.IGNORECASE)
            if not m:
                m = _re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']bmstable["\']',
                               html, _re.IGNORECASE)
            if m:
                header_url = _resolve(url, m.group(1))
            else:
                # some tables are linked directly to header.json-style URLs
                header_url = _resolve(url, "header.json")
        except Exception as e:
            log(f"  couldn't read table page: {e}")
            header_url = _resolve(url, "header.json")

    header = _json.loads(_http_get(header_url))
    name = header.get("name", url)
    symbol = header.get("symbol", "")
    data_url = _resolve(header_url, header.get("data_url", "data.json"))
    raw = _json.loads(_http_get(data_url))

    entries = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        md5 = (e.get("md5") or "").lower().strip()
        entries.append({
            "md5": md5,
            "level": str(e.get("level", "")),
            "title": e.get("title", ""),
            "artist": e.get("artist", ""),
            "url": e.get("url", ""),
        })
    return {"name": name, "symbol": symbol, "entries": entries}

def scan_library(root, conn, log=lambda s: None, progress=lambda d, t: None):
    """Incrementally update the SQLite cache for `root`, then return
    (songs list, stats). Only changed/new charts are parsed; vanished charts
    are deleted. `conn` is an open sqlite3 connection."""
    reused = parsed = removed = 0

    # existing signatures, read once: path -> (size, mtime)
    existing = {row[0]: (row[1], row[2])
                for row in conn.execute("SELECT path, size, mtime FROM charts")}

    # cheap pass: every chart path on disk
    disk_paths = []
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith(BMS_EXTS):
                disk_paths.append(os.path.join(dirpath, fn))
    total = len(disk_paths)
    disk_set = set(disk_paths)

    upserts = []
    for i, p in enumerate(disk_paths):
        try:
            st = os.stat(p)
        except OSError:
            continue
        sig = (st.st_size, int(st.st_mtime))
        if existing.get(p) == sig:
            reused += 1
        else:
            try:
                d = parse_bms(p); h = d["header"]
                notes = count_playable_notes(d)
                mode = detect_mode_from_bars(d)
                has_random = 1 if d.get("has_random", False) else 0
            except Exception:
                h, notes, mode, has_random = {}, 0, "?", 0
            try:
                md5 = file_md5(p)
            except Exception:
                md5 = ""
            upserts.append((p, sig[0], sig[1],
                            h.get("TITLE", os.path.basename(p)),
                            h.get("ARTIST", ""), h.get("GENRE", ""),
                            h.get("BPM", ""), mode, notes, has_random, md5))
            parsed += 1
            # commit in batches so a huge first scan isn't one giant transaction
            if len(upserts) >= 500:
                conn.executemany(
                    "INSERT OR REPLACE INTO charts VALUES (?,?,?,?,?,?,?,?,?,?,?)", upserts)
                conn.commit(); upserts.clear()
        if i % 200 == 0:
            progress(i, total)
    if upserts:
        conn.executemany(
            "INSERT OR REPLACE INTO charts VALUES (?,?,?,?,?,?,?,?,?,?,?)", upserts)
        conn.commit()
    progress(total, total)

    # delete charts no longer on disk
    gone = [p for p in existing if p not in disk_set]
    if gone:
        conn.executemany("DELETE FROM charts WHERE path=?", [(p,) for p in gone])
        conn.commit()
        removed = len(gone)

    # build the song list from the DB
    songs = [_row_to_song(r) for r in conn.execute(
        "SELECT path,size,mtime,title,artist,genre,bpm,mode,notes,random,md5 FROM charts")]
    return songs, {"reused": reused, "parsed": parsed, "removed": removed, "total": total}

def find_audio(folder, name):
    """BMS often names a .wav but ships .ogg. Try exact, then swap extensions."""
    exact = os.path.join(folder, name)
    if os.path.exists(exact):
        return exact
    stem = os.path.splitext(name)[0]
    for ext in AUDIO_EXTS:
        cand = os.path.join(folder, stem + ext)
        if os.path.exists(cand):
            return cand
    return None

def process_cover(path, max_px=1000, max_bytes=500_000):
    """Load a JPEG/PNG and return safe-spec JPEG bytes for embedding:
       RGB, at most max_px on the long edge, at most max_bytes.
       Returns (jpeg_bytes, (w,h), quality) or raises on failure."""
    if not _PIL_OK:
        raise RuntimeError("Pillow not installed — album art unavailable. "
                           "Run: pip install Pillow")
    img = Image.open(path)
    img = img.convert("RGB")           # flatten alpha/palette -> always-valid JPEG
    w, h = img.size
    if max(w, h) > max_px:
        scale = max_px / max(w, h)
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    data = None
    for q in (90, 85, 80, 72, 65, 58, 50):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data, img.size, q
    return data, img.size, 50          # smallest achievable, even if still over

def _resample_to_sr(audio, sr):
    """Resample stereo float32 audio from `sr` to the output SR.
    Uses high-quality polyphase resampling (scipy) when available — this is
    objectively better than the linear interpolation bmx2wav uses, with far
    less aliasing. Falls back to linear interpolation (still much better than
    nearest-neighbor) if scipy isn't installed."""
    if sr == SR:
        return audio
    if _SCIPY_OK:
        g = gcd(int(SR), int(sr))
        up, down = int(SR) // g, int(sr) // g
        # resample_poly applies an anti-aliasing FIR filter internally
        out = resample_poly(audio, up, down, axis=0).astype(np.float32)
        return out
    # fallback: linear interpolation per channel
    n_out = int(len(audio) * SR / sr)
    xp = np.arange(len(audio))
    xq = np.linspace(0, len(audio) - 1, n_out)
    ch = [np.interp(xq, xp, audio[:, c]) for c in range(audio.shape[1])]
    return np.stack(ch, axis=1).astype(np.float32)

def _decode_clip(ap):
    audio, sr = sf.read(ap, dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    audio = _resample_to_sr(audio, sr)
    return audio

def render_bms(path, log=lambda s: None):
    """Faithful render following the bmx2wav timing model:
       - per-measure native resolution (lcm of all channel lengths in that bar)
       - sample position advances by (SR*60/bpm)/(resolution/4)*ratio per step
       - channel 02 = measure-length ratio
       - channel 03 = inline hex BPM, 08 = extended BPM (#BPMxx)
       - channel 09 = STOP (#STOPxx), freezes time
       - 51-5Z / 61-6Z = long notes: only the START sounds, END is silent
       - #LNOBJ-marked notes in player lanes are silent (they end an LN)
       - D1-DZ / E1-EZ = landmines: never sound
    """
    d = parse_bms(path)
    header, wav_table = d["header"], d["wav_table"]
    bpm_table, stop_table, lnobj, bars = d["bpm_table"], d["stop_table"], d["lnobj"], d["bars"]
    folder = os.path.dirname(path)

    try:
        base_bpm = float(header.get("BPM", 120))
        if base_bpm <= 0:
            base_bpm = 120.0
    except (ValueError, TypeError):
        base_bpm = 120.0

    if not bars:
        raise RuntimeError("no note data found in this BMS")

    max_measure = max(bars.keys())

    # ---- pass 1: schedule events as (sample_position, wav_id) ----
    # We accumulate sample position bar by bar, subdivision by subdivision,
    # exactly like the reference, so BPM/length/STOP all fall out naturally.
    # (Audio is decoded AFTER this, for only the wav-ids that actually sound.)
    schedule = []
    bpm = base_bpm
    sample_pos = 0.0
    open_ln = {}  # channel -> True while a long note is open (so we mute its end)

    for measure in range(max_measure + 1):
        bar = bars.get(measure, {})

        # measure-length ratio (channel 02), default 1.0
        ratio = 1.0
        if "02" in bar:
            try:
                ratio = float(bar["02"][-1])  # last definition wins
                if ratio <= 0:
                    ratio = 1.0
            except (ValueError, IndexError):
                ratio = 1.0

        # resolution = lcm of the lengths (in note-pairs) of every channel here.
        # We also classify each channel ONCE here (kind + base-36 value) instead of
        # re-deriving it for every subdivision below — that inner work is the hot path.
        resolution = 1
        parsed_channels = {}  # channel -> (kind, [pairs_line, ...])
        for chan, payloads in bar.items():
            if chan == "02":
                continue
            kind = _classify_channel(chan)
            if kind is None:
                continue  # not a channel we act on (e.g. BGA/visual lanes)
            for payload in payloads:
                pairs = [payload[i:i+2] for i in range(0, len(payload) - len(payload) % 2, 2)]
                n = len(pairs)
                if n == 0:
                    continue
                resolution = resolution * n // gcd(resolution, n)
                if chan not in parsed_channels:
                    parsed_channels[chan] = (kind, [])
                parsed_channels[chan][1].append(pairs)

        # advance over each subdivision of the bar
        for step in range(resolution):
            stop_here = 0.0
            for chan, (kind, lines) in parsed_channels.items():
                for pairs in lines:
                    n = len(pairs)
                    # does this channel place an object at this subdivision?
                    if (step * n) % resolution != 0:
                        continue
                    pair = pairs[(step * n) // resolution]
                    if pair == "00" or pair == "":
                        continue
                    try:
                        wid = b36(pair.upper())
                    except ValueError:
                        continue

                    if kind == "mine":
                        continue  # landmines never sound
                    if kind == "bpm_inline":
                        try:
                            bpm = float(int(pair.upper(), 16))
                        except ValueError:
                            pass
                        continue
                    if kind == "bpm_ext":
                        if wid in bpm_table:
                            bpm = bpm_table[wid]
                        continue
                    if kind == "stop":
                        stop_here = stop_table.get(wid, 0.0)
                        continue

                    # kind is a sounding channel ("bgm", "note", or "long")
                    if kind == "long":
                        # long-note start/end pairing: mute the closing note
                        if not open_ln.get(chan):
                            open_ln[chan] = True   # START -> sounds
                        else:
                            open_ln[chan] = False  # END -> silent
                            continue
                    elif kind == "note" and wid in lnobj:
                        continue  # #LNOBJ terminator in a player lane is silent
                    schedule.append((sample_pos, wid))

            # advance sample position for this subdivision
            if bpm <= 0:
                bpm = base_bpm
            sample_pos += (SR * 60.0 / bpm) / (resolution / 4.0) * ratio
            if stop_here != 0.0:
                sample_pos += (SR * 60.0 / bpm) * (stop_here / 192.0 * 4.0)

    if not schedule:
        raise RuntimeError("no audible notes found in this BMS")

    # ---- decode only the clips that actually sound, in parallel ----
    # (decoding dominates render time; each clip is independent and soundfile
    # releases the GIL, so a thread pool gives a real speedup. Skipping unused
    # #WAV definitions avoids reading files the chart never plays.)
    used_ids = {wid for _, wid in schedule}
    clips, missing = {}, 0
    to_decode = []
    for wid in used_ids:
        fn = wav_table.get(wid)
        if not fn:
            continue
        ap = find_audio(folder, fn)
        if not ap:
            missing += 1
            continue
        to_decode.append((wid, fn, ap))
    if to_decode:
        from concurrent.futures import ThreadPoolExecutor
        def _decode_one(item):
            wid, fn, ap = item
            try:
                return wid, _decode_clip(ap), None
            except Exception as e:
                return wid, None, f"{fn}: {e}"
        workers = min(8, (os.cpu_count() or 4) * 2, len(to_decode))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for wid, clip, err in ex.map(_decode_one, to_decode):
                if err:
                    log(f"  could not read {err}")
                else:
                    clips[wid] = clip
    if missing:
        log(f"  note: {missing} referenced sounds not found in folder")

    # ---- pass 2: mix (vectorized) ----
    # Group note onsets by wav-id so each distinct clip is added in as few numpy
    # operations as possible. Output is bit-identical to a per-note add loop.
    if not clips:
        # nothing decoded successfully — return a short silence so callers don't crash
        return np.zeros((SR, 2), dtype=np.float32), header

    # total length = furthest (onset + clip length)
    last_end = 0
    for pos, wid in schedule:
        clip = clips.get(wid)
        if clip is not None:
            e = int(pos) + len(clip)
            if e > last_end:
                last_end = e
    out = np.zeros((last_end + SR, 2), dtype=np.float32)  # +1s tail

    # bucket onsets per wav-id
    onsets = {}
    for pos, wid in schedule:
        if wid in clips:
            onsets.setdefault(wid, []).append(int(pos))
    for wid, positions in onsets.items():
        clip = clips[wid]; clen = len(clip)
        for s in positions:
            out[s:s + clen] += clip[:len(out) - s]   # tail-safe slice add

    # gentle normalize if overlaps clipped
    peak = float(np.abs(out).max()) if out.size else 0.0
    if peak > 1.0:
        out /= peak
    return out, header

def write_tags_to_file(path, fmt, tags, cover):
    """Write format-appropriate tags. FLAC/OGG get full tags + cover; WAV basic only.
    Module-level so both the GUI and render worker processes can call it."""
    if fmt == "FLAC":
        f = FLAC(path)
        f["title"] = tags.get("Title",""); f["artist"] = tags.get("Artist","")
        f["album"] = "BMS"; f["genre"] = tags.get("Genre","")
        if tags.get("BPM"): f["bpm"] = tags["BPM"]
        if cover:
            pic = Picture(); pic.type = 3; pic.mime = "image/jpeg"
            pic.desc = "Cover"; pic.data = cover
            f.clear_pictures(); f.add_picture(pic)
        f.save()
    elif fmt == "WAV":
        from mutagen.wave import WAVE
        from mutagen.id3 import TIT2, TPE1, TALB, TCON
        f = WAVE(path)
        if f.tags is None:
            f.add_tags()
        f.tags.add(TIT2(encoding=3, text=tags.get("Title","")))
        f.tags.add(TPE1(encoding=3, text=tags.get("Artist","")))
        f.tags.add(TALB(encoding=3, text="BMS"))
        f.tags.add(TCON(encoding=3, text=tags.get("Genre","")))
        f.save()
    elif fmt == "OGG":
        import base64
        from mutagen.oggvorbis import OggVorbis
        from mutagen.flac import Picture as _Pic
        f = OggVorbis(path)
        f["title"] = tags.get("Title",""); f["artist"] = tags.get("Artist","")
        f["album"] = "BMS"; f["genre"] = tags.get("Genre","")
        if tags.get("BPM"): f["bpm"] = tags["BPM"]
        if cover:
            pic = _Pic(); pic.type = 3; pic.mime = "image/jpeg"
            pic.desc = "Cover"; pic.data = cover
            f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        f.save()

def _encode_ogg_ffmpeg(ff, audio, out_path):
    import subprocess
    tmp_wav = out_path + ".tmp.wav"
    try:
        sf.write(tmp_wav, audio, SR, format="WAV")
        proc = subprocess.run([ff, "-y", "-i", tmp_wav, "-c:a", "libvorbis", "-q:a", "6",
                               out_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **_no_window_kwargs())
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + proc.stderr.decode("utf-8","replace")[-300:])
    finally:
        if os.path.exists(tmp_wav):
            try: os.remove(tmp_wav)
            except OSError: pass

def _encode_flac_ffmpeg(ff, audio, out_path):
    import subprocess
    tmp_wav = out_path + ".tmp.wav"
    try:
        sf.write(tmp_wav, audio, SR, format="WAV")
        proc = subprocess.run([ff, "-y", "-i", tmp_wav, "-c:a", "flac",
                               "-compression_level", "8", out_path],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **_no_window_kwargs())
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + proc.stderr.decode("utf-8","replace")[-300:])
    finally:
        if os.path.exists(tmp_wav):
            try: os.remove(tmp_wav)
            except OSError: pass

def render_one_job(job):
    """Render ONE song to its final tagged file. Runs in a worker process.
    `job` = (in_path, out_path, fmt, tags, cover_bytes, ffmpeg, library_root).
    Returns (out_path, title, None) on success or (out_path, title, error_str)."""
    in_path, out_path, fmt, tags, cover, ff, lib_root = job
    title = tags.get("Title", os.path.basename(in_path))
    try:
        set_library_root(lib_root)              # restore guard in this subprocess
        assert_safe_output(out_path)            # never write into the library
        audio, _ = render_bms(in_path)
        assert_safe_output(out_path)
        if fmt == "OGG":
            _encode_ogg_ffmpeg(ff, audio, out_path)
        elif fmt == "FLAC" and ff:
            _encode_flac_ffmpeg(ff, audio, out_path)
        else:
            sf.write(out_path, audio, SR, format=("WAV" if fmt == "WAV" else "FLAC"))
        write_tags_to_file(out_path, fmt, tags, cover)
        return (out_path, title, None)
    except Exception:
        return (out_path, title, traceback.format_exc())

