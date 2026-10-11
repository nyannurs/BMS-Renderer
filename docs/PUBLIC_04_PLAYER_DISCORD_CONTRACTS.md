# Player, Discord & Data Contracts

## `player.py` — audio playback

The player streams a fully-rendered numpy buffer to the speakers. It deliberately does **not** use a sounddevice callback: a Python callback runs on PortAudio's realtime thread, where any GIL or garbage-collection pause starves the stream and causes audible stutter. Instead it opens a callback-less output stream with a ~0.25-second latency cushion and feeds it from a dedicated thread via blocking writes. PortAudio buffers internally and drains at the hardware clock, so a brief Python stall is absorbed rather than heard. At full volume the feeder writes a direct slice of the buffer (no per-block copying), and the buffer is pre-clipped once at load time. The feeder owns the playback cursor while playing; the GUI only reads it (for the timeline) and writes it on a seek, so no lock is taken on the audio path.

`Player` exposes `load`, `play`, `pause`, `stop`, `toggle`, `seek_seconds`, `set_volume`, `position_seconds` (which subtracts the stream latency so the timeline matches what is heard), `duration_seconds`, `poll_finished` (true once when a track ends, used for auto-advance), `redetect_device` (re-detect and switch to the current default output, preserving the playhead), and a `state` property. There is deliberately no end-of-track *callback* — the interface polls `poll_finished` from its transport timer, which keeps auto-advance on the GUI thread rather than the audio feeder thread. (Note: `state` is a *property*, not a method — it is `player.state`, never `player.state()`. Internal code that must be certain reads the underlying `_state` string directly.)

The module exposes `SD_OK` (whether `sounddevice` imported) and `SD_IMPORT_ERROR` (the reason it failed). On Linux a failure usually means the PortAudio system library is missing, not that there is no audio device — see `PUBLIC_05_DEVELOPMENT`.

## `discord_rpc.py` — optional Rich Presence

Self-contained and optional: if the module or its `pypresence` dependency is unavailable, every hook becomes a no-op and the app runs normally. The configuration block at the top of the file holds a Discord **Application ID** (which is public and safe to commit) and the large-image asset key and tooltip. Never commit a client secret or bot token — Rich Presence needs neither. The presence class connects, sets a now-playing or paused state, clears, and closes; the app drives it from the play, pause, stop, and close events. It is sent as a "Listening" activity and carries no timestamp of its own. Note that Discord itself displays an elapsed timer on the card that counts from when the activity was received; this is client-side behaviour and is not controlled by, or removable from, the app. A configurable button (label and URL, e.g. a link to the project's GitHub) is attached to the card and opens the URL when clicked. Note a Discord quirk: you do not see your own activity buttons on your own profile, but everyone else does.

---

## Data contracts

Exact shapes that cross module and process boundaries.

**Song dict:** `{path, title, artist, genre, bpm, notes, mode, md5, tags?, art?}` — `art` is a path or the sentinel `"__BLACK__"`.

**Tags dict:** `{Title, Artist, Album, AlbumArtist, Genre, BPM}` (all strings).

**Audio render job** — `render_one_job(job)` where `job = (in_path, out_path, fmt, tags, cover_bytes|None, ffmpeg|None, library_root [, quality [, progress_path]])`. `fmt` ∈ FLAC/WAV/OGG/MP3. The 8th element overrides encoder defaults: `{"flac_level": 0–12}`, `{"ogg_q": 0–10}`, or `{"mp3_vbr_q": 0–9}`. The optional 9th is a file path the job writes its 0–1 progress fraction to. Returns `(out_path, title, None | error_string)`.

**BGA video job (image sequence)** — `render_bga_video_job(job)` where `job = (in_path, out_path, ffmpeg, library_root, fps, size [, encode_opts [, n_workers [, progress_path]]])`. The 7th element is the encode-options dict; with none, it reproduces the original MP4 output. Returns `(out_path, title, None | error_string)`; the output path may have changed to `.mkv`.

**BGA video job (muxed / remux)** — `render_bga_muxed_job(job)` where `job = (in_path, out_path, ffmpeg, library_root, size [, encode_opts [, n_workers [, progress_path]]])`. Note there is **no `fps`** (the source video supplies it) and the return is a **4-tuple**: `(out_path, title, None | error_string, warnings_list)`. The warnings list carries non-fatal per-chart notes — honoured start offsets, detected overlay layers, remux fallbacks — for the log.

**Encode options dict:** `{video: default|x264|x264_nvenc|hevc|hevc_nvenc|av1|vp9, vbitrate, audio: default|flac|wav|ogg|mp3|aac, abitrate, flac_level, remux: bool, audio_only_rest: bool}`. `remux` copies a video BGA's stream instead of re-encoding (video BGAs only; forces `.mkv`). `audio_only_rest` renders queued charts that have no BGA as plain audio rather than skipping them.

**`bga_timeline(path)`** → `(events, total_seconds, missing, layer_events)`; each event list is `[(seconds, image_path_or_None), …]`.

**`detect_bga(path)`** → `{type: sequence|static|video|none, frames, images}`.

**`video_bga_info(path)`** → `{video_file, start_sec, total_sec, has_image_overlay, warnings}` or `None`.

**`predicted_container(opts)`** → `".mp4"` or `".mkv"` — the container the given options will actually produce.

**`nvenc_available(encoder)`** → bool (cached hardware probe); **`nvenc_reason(encoder)`** → ffmpeg's error from the last failed probe.

**`parse_bms(path, text=None)`** → `{header, wav_table, bpm_table, stop_table, lnobj, bars, base, has_random, is_pms}`. `text` lets a caller that already read the file (the scanner, which needs the raw bytes for the MD5) pass the decoded chart in rather than have it read a second time.

**`scan_library(root, conn, log, progress, should_stop=None)`** → `(songs, stats)`. `should_stop` is an optional predicate that cuts the scan short; on abort it returns an empty song list with `stats["aborted"]` set and skips the vanished-chart cleanup.

**`render_bms(path, log, progress=None)`** → `(audio: float32 (N, 2) at 44100 Hz, header)`. `progress` is an optional `callable(fraction)` invoked at coarse points during decode and mix.

**Process priority** is one of High / Above Normal / Normal / Below Normal / Low.

**Config keys:** `library`, `output`, `render_threads`, `lib_col_widths`, `lib_sort`, `window_geometry_qt`, `dark_mode`, `art_viewer_scaling`, `art_viewer_geometry`, `shuffle`, `loop`, `nowplaying_output`, `nowplaying_format`, `song_visualizer`.
