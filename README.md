# BMS Renderer — README

*Version 1.5.4*

---

## 1. Introduction

BMS Renderer is a Windows desktop app for turning BMS charts (beatmania IIDX–style
rhythm-game files, as played in Lunatic Rave 2 / beatoraja) into tagged lossless audio
files. It scans a large BMS library, lets you browse and curate charts, plays them back
in-app, organizes them against online difficulty tables, and renders them to FLAC or WAV
(plus OGG if ffmpeg is installed) with proper metadata and optional album art.

The tool exists to fill the gap left by renderers like **bmx2wav**: those render BMS
accurately but only output untagged WAV, with no library management, playback, or tagging.
BMS Renderer reuses the same proven timing logic (its render engine is modeled on bmx2wav's
source) and adds the entire curate-tag-export-organize workflow on top.

> **This program is entirely "vibe coded."** It was built end-to-end through conversational
> AI assistance, by a user with no programming background, iterating feature by feature. It
> works and its logic is tested, but it has not been professionally engineered or audited.
> Treat it accordingly: keep backups of anything important, and expect the occasional rough
> edge. (That said, see §3 for why your BMS library specifically is safe.)

---

## 2. Setup & Dependencies

BMS Renderer runs from source with Python. (It's a personal project shared as-is — no
installer, just the scripts.)

### Install Python (one time)
Install Python from <https://www.python.org/downloads/> and during install **tick "Add
python.exe to PATH."** Then open a terminal (Windows key → `cmd` → Enter) and run:

```
pip install soundfile numpy mutagen Pillow scipy sounddevice
```

What each is for:

| Library | Purpose | If missing |
|---|---|---|
| `soundfile` | read keysounds, write FLAC/WAV | required |
| `numpy` | audio buffer math | required |
| `mutagen` | write tags + cover art | required |
| `Pillow` | process album art images | album art disabled |
| `scipy` | high-quality resampling of off-rate keysounds | falls back to linear interpolation |
| `sounddevice` | in-app playback | Play button disabled |

**Optional — ffmpeg (recommended):** install **ffmpeg** and make sure it's on your PATH
(`ffmpeg -version` should work in a terminal). With it, FLAC exports at maximum compression
(level 8, lossless, smaller files) and OGG export becomes available. Without ffmpeg, FLAC
still works at the default level 5 and WAV is unaffected; only OGG is unavailable.

Put `bms_renderer.py`, `bms_core.py`, `player.py`, and `Run_BMS_Renderer.bat` in the same
folder. Double-click **`Run_BMS_Renderer.bat`** (it launches the app from the right folder and
keeps the window open if there's an error). All three `.py` files must sit together — the app
won't start without `bms_core.py` (the engine) and `player.py` (audio) next to it.


---

## 3. How it works & what it does

### The render pipeline
A BMS chart is a text file that maps two-character IDs to small audio clips (keysounds) and
schedules them across measures and channels. To produce a song, the app:

1. **Parses** the chart — keysound table, note data, BPM, STOP, measure-length, long notes.
2. **Schedules** every sound to an exact sample position, walking each measure at its native
   resolution. This correctly handles mid-song BPM changes (channels 03 and 08), STOP
   sequences (channel 09 / `#STOPxx`), time-signature/measure-length changes (channel 02),
   and long notes (channels 51–6Z and `#LNOBJ`), and ignores landmine channels (D/E). This
   timing model follows the bmx2wav reference implementation.
3. **Mixes** all the decoded keysounds into one stereo buffer, resampling any clip that
   isn't 44.1 kHz using high-quality polyphase resampling (cleaner than nearest-sample or
   linear interpolation).
4. **Encodes** the buffer to your chosen format (FLAC / WAV, or OGG if ffmpeg is installed)
   and writes the tags.

### File structure: one folder per song
BMS relies on a chart file sitting **in the same folder as its keysounds** (the `.wav` /
`.ogg` audio clips it references). The renderer finds a chart's sounds by looking in that
chart's own folder, so this layout is required for a render to actually produce audio — a
chart whose audio lives elsewhere will render as silence (with "sound not found" notes in
the log).

The same convention drives two features, so both work best when **all difficulties of a song
share one folder** (the standard way BMS packs are organized):

- **"Show all charts for this song"** groups by folder — it finds the other charts sitting in
  the same directory.
- **"Add all owned to Queue"** and the per-level bulk-add pick **one chart per folder** (the
  lowest note count) to avoid queuing every difficulty of the same song.

If a pack stores a song's difficulties in *separate* folders, these features will treat them
as different songs (you may get duplicates); if it crams *unrelated* songs into one folder,
they'll be treated as the same song (you may get only one). Most well-organized packs use one
folder per song, where this all behaves correctly.

### Your BMS library is read-only — by design, not just by intention
This is enforced structurally, because a curated library must never be altered:

- The moment you choose a library folder, the app marks it (and everything inside it) as
  protected. Every library file is opened **read-only** at the operating-system level — there
  is no code path that opens a library file for writing.
- Before the app writes any output file, it checks that the destination is **not inside** the
  library folder, resolving shortcuts and `..` tricks so nothing can sneak in. This check runs
  multiple times around each write. If the output location is inside the library, the app
  refuses and writes nothing.
- Therefore the app **cannot modify, move, rename, delete, or overwrite** anything in your
  library. Scanning only reads and lists files. Renders always go to a separate output folder.

(This protects against *this app*. It is not a substitute for your own backups against disk
failure or other software.)

### The cache (fast relaunch)
The first scan reads every chart's header and computes its MD5, then stores it in a
**SQLite database** (`bms_cache.db`) **next to the program**, never in your
library. SQLite loads incrementally and queries without parsing the whole file, so launches
stay fast even with a very large library. On later launches the app lists the library (fast —
folder listings only) and reuses cached rows for any chart whose size and modified-time are
unchanged; only new or changed charts are re-read, and deleted charts are dropped. (Older
versions used a single JSON cache file; it's removed automatically on first run.)

### Configuration
Settings live in `bms_config.json` next to the program (pretty-printed, human-editable):
your library path and output folder. Album art is **not** stored; it resets each session.
The render worker count is saved here as `render_threads` (settable from the Threads control
on the Queue tab; 0 or absent means use the CPU core count). The window size/position is
remembered between launches (`window_geometry`).

---

## 4. Feature set

### Library
- Scans a BMS root folder recursively for `.bms` / `.bme` / `.bml` / `.pms` charts.
- Browse with title, artist, BPM, play type, and playable note count.
- Fast debounced search and a play-type filter (All / 7K SP / 5K SP / DP / PMS).
- Click a column header to sort; sort by **Notes** to find the simplest chart of a song.
- Decodes Japanese (Shift-JIS) titles correctly; UI uses a Japanese-capable font (Meiryo UI).
- Charts containing `#RANDOM` are shown in **red** (not evaluated at render time yet — a flag).
- Display caps at 2,000 rows and draws in batches so the window never freezes; narrow the
  search to see more specific results.

### Playback
- **Double-click** a song to render and play it through your speakers.
- Transport bar under the log: previous (⏮), play/pause (▶/⏸), stop (■), next (⏭), a time
  readout, and a draggable timeline (drag is silent; playback jumps to the spot on release).
- A `[♪]` marks the now-playing row.
- On the **Library** tab a song plays to the end then stops; on the **Queue**, **Tables**,
  and **playlist** tabs it auto-advances to the next song down the list. Prev/Next skip
  through that same list.
- **Shuffle** and **Loop** toggles (bottom bar, left of the volume slider): Shuffle picks a
  random next song (and scrolls it into view); **Loop repeats the current song**. Shuffle
  applies to Queue/Tables/playlists; Loop works anywhere, including a single Library song.
- First play of a song renders it (a moment for busy charts); replaying/seeking is instant.
- **Near-gapless playback:** while a song plays in a Queue, table, or playlist, the previous
  and next song are rendered quietly in the background, so moving to them starts instantly.
  (Bounded to one ahead and one back, held in memory only — no files written.)
- A volume slider at the bottom-right controls playback loudness (playback only — it does
  not affect the volume of exported files).

### Custom Playlists
- A **Custom Playlists** tab (between Tables and Queue) holds locally saved playlists.
  Each song is stored by its **MD5 hash plus a title/artist label** — no file paths and no
  file copies. Each playlist is its own JSON file in a **`Playlists` folder** (auto-created)
  next to the program.
- **Portable / shareable:** because songs are identified by hash and each playlist is a
  single self-contained file, you can hand someone one `<playlist>.json` from your `Playlists`
  folder and it resolves to wherever *they* keep those charts. Charts the recipient doesn't
  own show greyed with the stored label, so they know what to find.
- Create / rename / delete playlists from the tab. Add songs by right-clicking any song
  (Library, Tables, or Queue) → **Add to playlist** → pick a playlist or "New playlist…."
- Double-click a playlist song to play it (auto-advances through the playlist).
- **Render playlist** renders all owned songs into a **subfolder of your output folder
  named after the playlist**, keeping each playlist's renders separate.
- Playlists made in older versions (a single `playlists.json`, or path-based entries)
  migrate automatically: they're split into per-playlist files and converted to the portable
  hash form the first time the library is scanned.

### Queue, tagging & export
- Add charts to a queue; each queued song has its own editable tags that auto-save as you
  type. The **Tags** fields are editable only for queued songs — when you're viewing a
  Library or Tables chart they show greyed/read-only (add the chart to the Queue to edit).
- Tags auto-fill from the chart header (Title, Artist, Genre, BPM); **Album is always "BMS."**
- A **BMS information** panel shows Title, Artist, Genre, BPM, Notes, Play type, File path,
  and MD5 for the selected chart. The fields are read-only but selectable, so you can copy any
  value (e.g. the MD5 or file path).
- **Render All in Queue** renders every queued song to the output folder and removes each as
  it finishes (failures stay in the queue with the error logged). Rendering runs in **parallel
  across worker processes** for a big speedup on large batches; the **Threads** control next
  to the format dropdown sets how many to use (default = your CPU core count).
- **Export format** dropdown: FLAC, WAV, and (if ffmpeg is installed) OGG. FLAC/OGG get full
  tags + cover art; WAV gets basic tags (no cover — the format doesn't support it). With
  ffmpeg on your PATH, FLAC is encoded at maximum compression (level 8, lossless, smaller
  files) and OGG becomes available; without ffmpeg, FLAC still works at the default level 5.
- **Album art (whole queue):** choose one cover for the whole queue; it's auto-converted to a
  safe square-ish JPEG (≤1000px, ≤500 KB) and embedded in each FLAC/OGG. Session-only.
- **Song folder art (per song):** the picker between "BMS information" and "Album art" shows
  the images found in the selected song's *own folder*, on **every tab** (Library, Tables,
  Queue, playlists). Scroll through them with the ◀ ▶ arrows (or **hold** an arrow to race
  through quickly), or type an image number in the
  **Go to #** box and press Enter to jump straight to it (handy for folders with many images);
  the preview is a fixed square so the buttons don't move as you scroll. An **ignore .bmp** toggle (on by default) hides BGA
  frame-bitmaps so you're not scrolling through hundreds of them. For a **queued** song the
  image you stop on becomes that song's cover; on other tabs scrolling is just a preview
  (covers are only applied at render time, which is queue-only). This per-song pick is used
  only when no whole-queue art is set — the whole-queue cover always takes priority.

### Difficulty Tables
- Click **Add table by URL…**, paste a difficulty-table link, and the program fetches the
  table, reads its own name from the table data, and saves it to `tables.json` automatically —
  no manual naming. It's added to the dropdown and loaded right away.
- Select any saved table from the dropdown to load it (BMS-table standard; charts matched to
  your library by MD5). `↻ Refresh` re-fetches the current one.
- Charts are grouped by level in collapsible rows; owned charts show normally, **missing**
  ones (in the table but not your library) show in grey.
- Right-click a chart: **Play**, **Add to Queue**, or **Show all charts for this song** —
  the last filters the Library to every chart in the **same folder** (where difficulties of a
  song almost always live together), sorted simplest-first. An **Exit lookup** button
  returns you to the full library.
- **Add all owned to Queue** queues every owned chart in the current table at once, with one
  chart per song (one per folder, keeping the lowest-notecount difficulty) and skipping
  anything already in the queue. Right-clicking a **level header** offers "Add all in this
  level to Queue" to do the same for just that difficulty level.

### Right-click menu (Library / Queue / Tables)
Play, Add to Queue, and Show all charts are available on all three tabs (Add to Queue is
greyed out on the Queue tab; actions are greyed out for charts you don't own on Tables).

In the Library you can **select several charts at once** (Ctrl-click to add individual rows,
Shift-click for a range) and then right-click → **Add to Queue** — or use the **Add selected
to Queue** button — to queue them all in one go. Duplicates already in the queue are skipped.

---

## 5. Known limitations

- **`#RANDOM` charts** are flagged (red) but not evaluated — they may render with conflicting
  data. **BMSON** (`.bmson`) is not supported.
- Same-song features ("Show all charts," bulk add-to-queue) group by **folder**, so they
  assume one folder per song with its difficulties together (see §3). Packs that split a
  song across folders, or mix songs in one folder, won't group as expected.
- Difficulty-table fetching depends on the table following the standard format and being
  reachable; unusual tables may not parse.
- The app is `.py`-based (run via Python); there is no standalone `.exe` (a build script can
  be provided to make one with PyInstaller).
- This is hobby software (see §1). Keep backups.

---

## 6. Changelog

- **1.5.4** — Fixed a bug where adding a song to the queue from the Playlists tab could queue
  the wrong song (whatever was selected on the Library tab). Also fixed song art resetting to
  the first image when right-clicking a song to queue it.
- **1.5.3** — Fixed the song folder art pick being lost when adding a song to the queue — the
  image you've scrolled to is now carried over as that song's cover instead of resetting.
- **1.5.2** — Selecting a song in the Custom Playlists tab now updates the BMS information,
  tags preview, and song folder art panels like the other tabs (the playlist tab was missing a
  selection handler, so album art didn't work there).
- **1.5.1** — Fixed column-header sorting in the Custom Playlists and Tables tabs (the headers
  weren't wired up). Click a header to sort, again to reverse; in Tables, songs sort within
  each level group.
- **1.5.0** — Click and hold the ◀ ▶ art arrows to speed through a folder's images (a single
  click still steps one at a time; holding ramps up and races through them).
- **1.4.2** — Fixed the art-picker arrows shifting position as you scrolled through images
  (the filename in the status line was changing the panel width). Arrows are now in a
  fixed-size centered group; long filenames are shortened in the display.
- **1.4.1** — Internal cleanup: removed code orphaned by recent reworks (a duplicate method,
  an unused helper, two dead constants) and cached the blank art-preview image. No behavior
  changes.
- **1.4.0** — BMS information fields are now selectable/copyable (read-only text boxes, like
  the path fields). Added a "Go to #" box to the song folder art picker — type an image number
  and press Enter to jump straight to it.
- **1.3.3** — Song folder art preview is now a fixed 1:1 square (scaled + letterboxed) so the
  ◀ ▶ buttons don't jump as you scroll different-sized images, and you can scroll the preview
  with the arrows on any tab.
- **1.3.2** — The song folder art preview now shows on every tab (Library, Tables, Queue,
  playlists), not just the Queue. Default window size 1600x1000.
- **1.3.0** — New per-song art picker (between BMS information and the whole-queue art): for a
  queued song, scroll the images in that song's own folder with ◀ ▶ and the shown one becomes
  its cover, with an "ignore .bmp" toggle (on by default). Whole-queue art still wins when set.
- **1.2.2** — Hid the console window that briefly flashed when ffmpeg ran (OGG / level-8
  FLAC) — no more black boxes during a render.
- **1.2.1** — The custom window icon is now embedded in the program (no icon file needed in
  the folder).
- **1.2.0** — Custom window/taskbar icon (replaces the default Tk feather), loaded from
  `bms_icon.ico` / `bms_icon.png` in the program folder; falls back silently if absent.
- **1.1.0** — Multi-select in the Library: Ctrl/Shift-click several charts and right-click →
  Add to Queue (or use the button) to queue them all at once, duplicates skipped. Right-click
  no longer collapses a multi-selection to a single row.
- **1.0.0** — First stable release, packaged as a standalone Windows .exe (no Python install
  needed). Core functionality and full feature set verified via a manual test pass plus the
  47-test automated suite. Everything from the 0.x line is included.
- **0.27.0** — Internal restructure for testability: all non-GUI logic now lives in
  `bms_core.py` (imported by the app — one shared source of truth). Added an automated test
  suite (`test_bms_core.py`, 47 tests) and a manual test checklist (`TESTING_MANUAL.md`). No
  feature or behavior changes. **`bms_core.py` and `player.py` must sit next to
  `bms_renderer.py`.**
- **0.26.0** — Internal performance pass on the render engine (channel constants computed
  once; each channel classified once per bar instead of per-subdivision) — faster on dense
  charts, with bit-identical output. Removed code orphaned by the refactor and refreshed a
  couple of stale comments. No behavior changes.
- **0.25.0** — Loop now repeats the current song (was a no-op before). Shuffle scrolls the
  picked song into view. Added Prev/Next skip buttons. Transport buttons are icon-only
  (⏮ ▶/⏸ ■ ⏭). New "Add table by URL…" button fetches the table's own name automatically and
  saves it to `tables.json` — no manual naming.
- **0.24.0** — Added Shuffle and Loop toggles at the bottom (left of the volume slider);
  they apply to Queue, Tables, and playlists, not the Library. Table playback now
  auto-advances down the list like the Queue, with the same near-gapless pre-rendering.
  Replaced the volume emoji with a plain "Vol:" label.
- **0.23.0** — Performance pass. Near-gapless playback: the previous and next song in a
  queue/playlist pre-render in the background while the current one plays, so moving between
  them is instant (in-memory, one ahead + one back). Faster rendering — the mix step is
  vectorized and only the keysounds a chart actually plays are decoded. Fixed sluggish
  text-caret/selection in the Tags fields. Minor internal cleanups.
- **0.22.2** — Tag fields are greyed out (read-only) when viewing Library or Tables charts,
  since edits only save for queued songs — making it clear where editing takes effect.
- **0.22.1** — Threads/Format controls now read left-to-right in the correct order. The
  window remembers its size/position between launches. Faster first-play playback (a song's
  keysounds now decode in parallel).
- **0.22.0** — Batch rendering now runs in parallel across worker processes (big speedup on
  large queues/playlists); a "Threads" control sets how many (default = CPU count, saved as
  `render_threads`). The now-playing `[♪]` marker shows on the Tables tab too. Right-click a
  level header in a table to add all songs in that level to the queue.
- **0.21.0** — Tables tab: "Add all owned to Queue" button queues every owned chart in the
  table, one per song (one per folder, lowest-notecount), skipping anything already queued.
- **0.20.1** — Fixed Tables playback for real: double-click resolves the chart under the
  cursor, and the Play button plays the Tables selection (not the Library one).
- **0.20.0** — Playlists are now one JSON file per playlist in an auto-created `Playlists`
  folder, so a single file is easy to share. Old `playlists.json` migrates automatically.
  Removed unused code (keysound fingerprint/overlap, same-song threshold, dead helpers); the
  keysound column is dropped from the cache, which rebuilds automatically on next launch.
- **0.19.2** — Playback now works from the Tables tab too: double-click an owned chart to
  play it, and the Play button plays the selected table chart.
- **0.19.1** — FLAC now encodes at maximum compression (level 8) via ffmpeg when ffmpeg is on
  PATH (lossless, ~10–20% smaller files); falls back to soundfile's level 5 without ffmpeg.
- **0.19.0** — Play button now starts the selected song (not just double-click). "Show all
  charts" groups by folder instead of keysounds. Now-playing title scrolls (marquee) when too
  long. OGG export is back via ffmpeg (reliable, unlike the old libsndfile path) — shown in
  the format dropdown only when ffmpeg is on PATH. Added a redraw nudge for a Tk-on-Linux/
  Wayland repaint quirk.
- **0.18.1** — Reduced the visual "rebuild" flicker when restoring the window from minimized.
- **0.18.0** — Playlists are now portable: each song is stored by MD5 hash + a title/artist
  label instead of a file path, so a shared `playlists.json` resolves to whatever paths the
  recipient has. Charts the recipient doesn't own show greyed with their label. Existing
  path-based playlists auto-migrate to hash form.
- **0.17.0** — New "Custom Playlists" tab (between Tables and Queue): locally stored
  playlists that reference charts by path (no file copies). Right-click any song → Add to
  playlist. Double-click a playlist song to play (auto-advances). "Render playlist" renders
  into a subfolder of the output path named after the playlist. Added a playback volume
  slider at the bottom-right (playback only; doesn't affect exported files).
- **0.16.0** — Cache moved from one big JSON file to a SQLite database (`bms_cache.db`):
  faster loads, smaller footprint, no full-file parse on launch. The old `bms_cache.json` is
  removed automatically. Raised the minimum window size so the right panel never clips.
- **0.15.1** — Fixed transport controls getting clipped when the window was made short
  (log + transport bar anchored to the bottom; main area shrinks instead; minimum window
  size added).
- **0.15.0** — Removed OGG export (libsndfile's Vorbis encoder was unreliable and produced
  corrupt files on some systems) — FLAC and WAV remain. Library and output paths are now
  selectable, full-width fields. Fixed play/stop buttons rendering inconsistently.
- **0.14.0** — Much smaller cache (keysounds stored as compact int hashes). Same-song
  threshold configurable in `bms_config.json` (default 0.95). "Exit lookup" button leaves
  show-all mode. Tables auto-load when picked from the dropdown (no Fetch button). Export
  format dropdown (FLAC / WAV / OGG) next to Render All.
- **0.13.0** — Right-click menu on Library and Queue too (Add to Queue greyed out on the
  Queue tab). New "BMS information" section under Tags; Notes moved there. Uniform, de-bolded
  panel fonts. App uses Meiryo UI for clean Japanese text. Right panel order: Tags, BMS
  information, Album art.
- **0.12.0** — Playback on double-click (single-click just selects/shows tags). Tab order
  Library, Tables, Queue. Now-playing icon shown as `[♪]`. Table levels are collapsible groups.
- **0.11.1** — Fix: restored a method header accidentally dropped in 0.11.0 that prevented
  the app from starting.
- **0.11.0** — Difficulty Tables tab: add table URLs to `tables.json`, fetch them (BMS-table
  standard, matched by MD5), see charts grouped by level with owned vs missing (missing grey).
  Right-click for Play / Add to Queue / Show all charts. MD5 + keysound fingerprint cached.
- **0.10.0** — Native playback: click a song to render+play. Transport bar with
  play/pause/stop + draggable seek. `♪` marks the now-playing row. Library plays to end;
  Queue auto-advances. Needs `sounddevice`.
- **0.9.2** — Config JSON pretty-printed. Album art is session-only (no longer saved/loaded).
- **0.9.1** — Moved album-art controls into the Tags panel.
- **0.9.0** — High-quality polyphase resampling (scipy) for off-rate keysounds; falls back to
  linear interpolation if scipy is absent.
- **0.8.0** — Album art: one cover for the whole queue, auto-converted to a safe JPEG
  (≤1000px, ≤500 KB) and embedded. Requires Pillow.
- **0.7.0** — Charts containing `#RANDOM` shown in red (flag only).
- **0.6.0** — Playable note-count column, shown for the selected chart. Charts parsed once
  per scan. Sortable by clicking the Notes header.
- **0.5.1** — Fixed hard UI freezes: all scan/render work updates the UI only from the main
  thread; library list draws in batches with a display cap.
- **0.5.0** — Library/Queue tabs, per-song tag panels, Render All (removes each when done),
  remembered output folder, Album tag pinned to "BMS."
- **0.4.0** — Engine overhaul from the bmx2wav reference: base-36 channels, per-measure
  resolution, mid-song BPM changes (ch 03/08), STOP (ch 09 / `#STOPxx`), measure length
  (ch 02), long notes (51–6Z / `#LNOBJ`), landmine suppression (D/E), robust error handling.
- **0.3.0** — Auto-rescan on launch (remembers library), version label, scrollbar, fast
  search, 7K-SP / play-type filtering.
- **0.2.0** — Library cache (path+size+mtime) for fast relaunch + incremental scan.
- **0.1.0** — Initial: scan, browse, auto-fill+edit tags, render to FLAC, read-only library
  protection.
