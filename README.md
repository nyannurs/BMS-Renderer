# BMS Renderer

![Screenshot](https://i.imgur.com/NI21a1S.png)

This program is vibe coded, JSYK.

BMS Renderer turns a BMS library into a tagged music collection. It scans your
charts, lets you browse and play them like a music player, and renders them to
FLAC, WAV, or OGG with proper tags and cover art — keysounds, BPM changes, and
stops are handled the way bmx2wav does it.

## Folder structure

Keep `bms_renderer.py`, `bms_core.py`, and `player.py` together in one folder.
That folder is also where the app writes its own files: the cache
(`bms_cache.db`), settings (`bms_config.json`), saved tables (`tables.json`), and
a `Playlists` folder.

## Usage

1. Install Python from <https://www.python.org/downloads/> (tick **"Add
   python.exe to PATH"**).
2. Install the dependencies:
   ```
   pip install soundfile numpy mutagen Pillow scipy sounddevice
   ```
3. Run `bms_renderer.py` (double-click it, or `python bms_renderer.py`).

Optional: if `ffmpeg` is on your PATH you also get OGG export and
maximum-compression FLAC. Without it, FLAC and WAV still work.

First library scan is slow; after that it's cached and fast.

## Features

- **Library** — search, filter by play type, sort by any column. **Songs only**
  collapses the list to one row per song; expand a row for its charts, or
  double-click the song to play a casual-friendly chart (100–500 notes).
- **Discovery** — an endlessly scrolling art grid of your whole library, with
  cover art picked per song (stagefile, then any folder image, then a BGA frame).
  Double-click a tile to listen; right-click to queue, jump to its charts, or add
  it to a playlist.
- **Tables** — paste a difficulty-table URL and it loads and saves itself,
  matched to your library by chart hash. Charts are grouped by level.
- **Custom Playlists** — build playlists, render them to their own folder, or
  re-curate songs from one playlist into another. Each playlist is a portable
  JSON file that resolves on anyone's library by hash.
- **Queue** — collect songs, edit tags, set per-song or
  whole-queue cover art, then render everything. Batch rendering uses multiple
  CPU cores.
- **Player** — seek, volume, shuffle, loop, prev/next, near-gapless auto-advance.
- **Your BMS files are never touched** — the app only ever reads your charts,
  sound files, and folders. Rendered audio is written exclusively to the separate
  output folder you choose, and every write is checked against your source path
  first, so scanning or rendering can never modify, overwrite, or delete anything
  in your collection.

## Changelog

- **1.9.3** — Added MP3 export at a fixed 320 kbps. Appears as a format option when ffmpeg is installed, with full tags (including Album Artist) and embedded cover art.
- **1.9.2** — Tagging, album views, and polish.
  - Album tag now writes what you type (it was stuck on "BMS"), and empty albums are allowed.
  - Added an Album Artist tag — the correct way to group a render into one album in music players. Album and Album Artist are session-wide: empty at startup, applied to every queued song (and any added afterward), and reset on restart.
  - Keyboard tagging: UP/DOWN move between songs; TAB/Shift-TAB cycle only the tag fields (Title–BPM, wrapping).
  - Tags carry over when you send a song to another playlist or the queue (Album/Album Artist excluded).
  - Custom Playlists: edit tags and pick art per song, saved into the playlist's .json to tag now and render later. Missing art on a shared playlist falls back gracefully instead of crashing.
  - "Assign black square" button for a clean black cover (generated in memory, shows in the preview, persists per playlist entry).
  - Album view for Tables, Custom Playlists, and Queue (the Discovery-style art grid), with a highlighted selected/now-playing tile; the Queue plays left-to-right. Tables album view shows level headers between rows.
  - Shift-click to multi-select in Tables/Playlists, then right-click to add all to the queue or a playlist.
  - The console window no longer stays open behind the app on Windows (with a fallback if it can't relaunch).
  - Fixed Album view freezing on large tables; removed the diamonds from the Discovery tab name; no-art square is now true black.
  - Internal cleanup: faster indexed lookups, removed dead code.
- **1.8.0** — Added album view mode toggle for most tabs. Album tag now writes what you type (was stuck on "BMS"). Added an Album Artist tag for grouping renders into one album. Album/Album Artist are session-wide (empty on startup, applied to all queued + newly-added songs, reset on restart). TAB between tag fields, DOWN to next song. Edit tags and pick art in Custom Playlists — saved into the playlist .json to tag now and render later, with graceful fallback if a shared playlist's art file is missing.
- **1.6.8** — Discovery now fills in correctly if opened while the library cache is still loading.
- **1.6.7** — Discovery: hovering anywhere on a tile (including the art) triggers
  the title marquee.
- **1.6.6** — Discovery: fixed the artist line being clipped; tiles now fit the
  art and both text lines with a gap between rows.
- **1.6.5** — Discovery: added spacing between rows.
- **1.6.4** — Discovery rebuilt with view recycling — only on-screen tiles exist
  as widgets, so scrolling stays smooth on any library size. Fixed the right
  panels being squished when toggling "Songs only".
- **1.6.3** — Smoother Discovery scrolling and parallel thumbnail loading.
- **1.6.2** — Fixed the Discovery grid glitching during fast scrolling; letterboxed
  art centered on black; taller tiles.
- **1.6.1** — Discovery became an infinite-scrolling grid; long titles marquee on
  hover; added "Show all charts" and "Add to playlist" to its right-click menu.
  Library BPM left-aligned; "Chart count" header in Songs-only mode.
- **1.6.0** — Added the "Songs only" Library toggle and the Discovery tab.
  Playlists can re-curate into other playlists.
- **1.5.4** — Official public release.
