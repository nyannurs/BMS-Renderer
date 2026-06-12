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
2. Install ffmpeg.
3. Install the dependencies:
   ```
   pip install soundfile numpy mutagen Pillow scipy sounddevice
   ```
4. Run `bms_renderer.py` (double-click it, or `python bms_renderer.py`).

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
- **Render Sequence BGA** — you can choose to render BMP/JPG/PNG sequence BGAs together with the song. Needs `ffmpeg` to work.
- **Player** — seek, volume, shuffle, loop, prev/next, near-gapless auto-advance.
- **Your BMS files are never touched** — the app only ever reads your charts,
  sound files, and folders. Rendered audio is written exclusively to the separate
  output folder you choose, and every write is checked against your source path
  first, so scanning or rendering can never modify, overwrite, or delete anything
  in your collection.
