# BMS Renderer

![Screenshot](https://i.imgur.com/DiekJzp.png)

## This program is built by exclusively iterating with Claude. It's fully vibe coded.

BMS Renderer turns a BMS library into a tagged music collection. It scans your
charts, lets you browse and play them like a music player, and renders them to
FLAC, WAV, or OGG with proper tags and cover art — keysounds, BPM changes, stops,
and `#RANDOM`/`#SWITCH` charts are handled the way bmx2wav does it.

## Folder structure

Keep `bms_renderer_qt.py`, `bms_core.py`, and `player.py` together in one folder.
That folder is also where the app writes its own files: the cache
(`bms_cache.db`), settings (`bms_config.json`), saved tables (`tables.json`), and
a `Playlists` folder.

## Usage

Download the executable from the Releases page and place `BMS Renderer.exe` inside of its own folder and run it. If you use the executable binary, you can skip all of the steps below. Your antivirus might throw you a warning. If you have a problem with this, feel free to use the included Python scripts by following the steps below.

## Using the Python scripts

1. Install Python from <https://www.python.org/downloads/> (tick **"Add
   python.exe to PATH"**).
2. Install `ffmpeg` (don't forget to **add it to PATH**).
3. Install the dependencies (you can ignore `pypresence` if you don't want Discord RPC):
   ```
   pip install PyQt5 soundfile numpy mutagen Pillow soxr sounddevice pypresence
   ```
4. Make sure all of the files are in their own folder, i.e. `BMS Renderer`
5. Run `bms_renderer_qt.py` (double-click it, or `python bms_renderer_qt.py`).

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
  matched to your library by chart hash. Charts are grouped by level, with an
  album view and collapse/expand controls.
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
- **Completely optional Discord RPC** — don't want it? delete `discord_rpc.py` and don't worry about it! NOTE: the RPC module is enabled by default in the executable distributed on the Releases page.
- **Output a self-updating "Now Playing" file for OBS** — in bms_config.json, you can specify "nowplaying_output" as true/false, and "nowplaying_format" as txt or html. txt is just the song title and artist, while html displays automatically selected album art.
- **Music visualization choices** — in bms_config.json, you can specify "song_visualizer" as waveform or moodbar.

## BMS library folder structure ##

BMS Renderer assumes your library is organized one folder per song, with that song's chart files, keysounds, and artwork all kept together inside it. Some features depend on this assumption, such as "Show all charts for this song" and the album art picking.

## Media hotkeys ##

If your keyboard has media keys, they should just work. These will only work if the program is focused.

- CTRL + M: mute current playback
- CTRL + left arrow/right arrow: play previous/next song respectively
- CTRL + up arrow/down arrow: increase/decrease volume respectively
- CTRL + spacebar: pause/unpause the currently playing song


## Linux users ##

Audio playback needs the `PortAudio` system library, which pip can't install on Linux. Examples:

- **Debian/Ubuntu/Mint**: `sudo apt install libportaudio2`
- **Arch**: `sudo pacman -S portaudio`
- **Fedora**: `sudo dnf install portaudio`

Windows and macOS users can skip this — `pip install sounddevice` includes PortAudio there.

## Documentation ##

Just like everything in this project, the included "docs" folder is generated entirely by Claude, so it could be inaccurate or missing details.

## License ##

BMS Renderer is licensed under the GNU General Public License v3.0 (see `LICENSE`).
Binary releases bundle FFmpeg (the gyan.dev "full" build, GPLv3) for audio and video encoding. FFmpeg is called as a separate executable and is unmodified. Its source and license are available at https://ffmpeg.org and https://www.gyan.dev/ffmpeg/builds/. FFmpeg is a trademark of Fabrice Bellard; BMS Renderer is not affiliated with the FFmpeg project.