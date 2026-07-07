# Engine Reference (`bms_core.py`)

Pure logic — no Qt, no audio device. The source of truth for parsing, rendering, BGA, tagging, scanning, persistence, and table fetching.

## Constants
`SR = 44100`. `BMS_EXTS = (.bms, .bme, .bml, .pms)`, `AUDIO_EXTS`, `IMAGE_EXTS`, `VIDEO_EXTS`. `APP_VERSION` (the app version string; single source, surfaced in the window title). Paths under `program_dir()`: `DB_PATH`, `CONFIG_PATH`, `TABLES_PATH`, `PLAYLISTS_DIR` (legacy `bms_cache.json`/`playlists.json` auto-migrate).

## Library root & safety
`set_library_root(path)` sets the module root. `assert_safe_output(out)` raises if the output is inside the library (every render job calls it). `program_dir()` is the base for all persistence paths.

## Helpers
`b36(s)` (base-36 → int); `read_bms_text(path)` reads a chart trying **Shift-JIS first** then UTF-8 fallbacks (always use it for chart text); `ffmpeg_path()`; `file_md5(path)`; channel classifiers.

## File resolution (case-insensitive)
BMS charts are authored on Windows (a case-insensitive filesystem) and frequently reference, say, `BD_000.wav` when the file on disk is `bd_000.ogg`. On case-sensitive filesystems (Linux/macOS) an exact-case lookup misses, every such keysound goes silent, and the render comes out nearly empty. To prevent this:
- `find_audio(folder, name)` / `find_image(folder, name)` try the exact name (fast path), then swap among the known extensions, then fall back to a **case-insensitive** match of both the exact name and each candidate extension.
- `_dir_index(folder)` builds a `{lowercase_name: real_name}` map, cached per folder for the process (a song folder is static during a render, and the case-insensitive path only runs after the exact and extension-swap lookups miss). `clear_dir_index_cache()` drops the cache if folder contents change.

## Parsing
`parse_bms(path)` returns `{header, wav_table, bpm_table, stop_table, lnobj, bars, has_random, is_pms}`, where `bars` is `{measure → {channel → [payload, …]}}`. `#RANDOM`/`#SWITCH` control flow is implemented, with branch selection deterministically seeded by the chart's filename so repeat renders are identical.

`detect_bga(path)` returns `{type: sequence|static|video|none, frames, images}`. It counts BGA events on **both** the base layer (channel 04) and the overlay layer (channel 07), since many charts animate entirely on 07 over a static or empty 04. It classifies by comparing image events to video events on the timeline rather than by the mere presence of a video definition — a chart may define both a video BGA and a full image fallback sequence, and the image sequence wins. `"video"` is returned only when there is no usable image sequence.

`count_playable_notes` and `detect_mode_from_bars` derive the note count and play mode.

## Scanning & caching
`db_connect()` opens the SQLite cache (`charts` table keyed by path). `scan_library(root, conn, log, progress)` walks the library, parses changed/new charts, removes vanished ones, and returns `(songs, stats)`. The reported total is the raw count of files matching the scanned extensions. (Note: charts whose paths exceed the Windows 260-character limit may fail to stat and be skipped.)

## Persistence & table fetching
`load_config`/`save_config`, `load_tables_file`/`save_tables_file`, and the playlist CRUD functions persist to the paths above. `load_config` is non-destructive if the config file is corrupt: a file that exists but won't parse is moved aside to `bms_config.json.bad` rather than being treated as empty, so a later save can't overwrite and wipe the user's settings. For difficulty tables, `_http_get` retries with an unverified SSL context on certificate errors (some table hosts have expired certs), and `fetch_table(url)` prepends `https://` when the scheme is missing and follows the standard chain (`<meta name="bmstable">` → `header.json` → `data.json`), resolving entries to library charts by MD5.

## Audio rendering
`render_bms(path, log)` returns a float32 stereo numpy array at `SR` plus the parsed header, using the timing model above. Long notes sound only at the start; `#LNOBJ`-marked notes are silent; identical `(sample position, keysound)` pairs are de-duplicated so a sound at a given instant plays once (genuine layering — the same sound at different times, or different sounds together — is preserved); and the mix is peak-normalised to within [-1, 1]. Keysounds whose sample rate differs from the output rate are resampled with anti-aliasing via `soxr` (with a plain linear-interpolation fallback only if soxr isn't installed); same-rate keysounds are used as-is.

The encoders write a temporary WAV and invoke ffmpeg. Each accepts an optional quality argument that defaults to the original behaviour: OGG `-q:a 6`, FLAC `-compression_level 8`, MP3 320 kbps CBR. `write_tags_to_file` embeds metadata and cover art via mutagen; `process_cover` normalises an image to embeddable bytes.

## Offline render jobs
These run in worker processes, so they are top-level and picklable, and each restores the library root and calls `assert_safe_output`.

`render_one_job(job)` — `job = (in_path, out_path, fmt, tags, cover_bytes, ffmpeg, library_root [, quality])`. The optional 8th element overrides encoder defaults (`{"flac_level":…}`, `{"ogg_q":…}`, or `{"mp3_vbr_q":…}`). Returns `(out_path, title, None | error)`.

`bga_timeline(path)` returns `(events, total_seconds, missing, layer_events)` — the base-layer and overlay-layer event lists, each `[(seconds, image_path_or_None), …]`, using the same accumulator as `render_bms`.

`_bga_encode_args(opts, out_path)` translates BGA export options into ffmpeg arguments and the actual output path. With no options (or video and audio both "default") it reproduces the original H.264 4:2:0 + AAC 192k in an MP4; any other combination outputs to MKV for broad codec compatibility. Video codecs: x264 (software/NVENC), HEVC (software/NVENC), AV1, VP9. Audio: AAC, WAV (PCM), FLAC, OGG (VBR), MP3 (VBR). Opus is intentionally omitted (it requires 48 kHz; resampling is out of scope).

`render_bga_video_job(job)` — `job = (in_path, out_path, ffmpeg, library_root, fps, size [, encode_opts [, n_workers]])` (where `size` is a `(width, height)` tuple). It renders the audio, builds the base and overlay BGA timelines, composites the overlay (channel 07) over the base (channel 04), and pipes raw frames to ffmpeg with the audio muxed in. The returned output path reflects the actual file written (which may be `.mkv`). If an NVENC (hardware) video encode fails — most commonly because consumer NVIDIA GPUs limit the number of simultaneous NVENC sessions, so a large parallel batch exhausts them and the excess encoders produce no output — the job automatically retries once with the equivalent software encoder, so those charts still render (on CPU) instead of failing.

The compositing follows beatoraja's layer model. If an overlay image carries its own alpha channel, that alpha is used as-is — some layers define their shape entirely in alpha (and may store inverted colour underneath), so re-deriving transparency from the colour data would corrupt them. If an overlay has no alpha (a plain BMP or JPEG), black is the transparent colour and every non-black pixel is fully opaque — a simple binary key. The black threshold is kept tight (only true, near-pure black keys out) because BMS layers use pure black as the transparent colour and dark-but-coloured content sits just above it: a looser threshold would wrongly erase things like dark-brown hair. It deliberately does **not** scale opacity by pixel brightness: a layer's translucency is part of its artwork, and deriving it from brightness would wash out bright, fully-opaque overlays. Transparency is keyed at the source resolution and scaled together with the image so edges stay clean without dark fringing. Base images are likewise composited over black using their alpha when present, so a transparent base (for example white title text on transparency) shows its content over black rather than being flattened. Scaling, keying, and compositing are each cached per unique image and per unique base/overlay pair, so the work happens once rather than per output frame. These caches are **memory-bounded** (least-recently-used, under a byte cap) rather than unbounded: a long chart can reference on the order of a thousand distinct BGA frames, and holding every scaled image and composited frame as raw pixels would grow to several gigabytes and exhaust memory during parallel renders (which manifested as a killed worker and ffmpeg reporting it received no packets — a zero-byte output). The caps also **scale down with the render's worker count**, so the total memory across all worker processes stays bounded no matter how many run at once — without this, a heavy chart that renders fine on its own could still exhaust memory when many workers each cache a large BGA simultaneously. Because playback is sequential, only a small working set is live at any moment, so the LRU keeps the per-frame speedup while capping memory; an evicted frame simply recomposites identically the next time it is needed.

## Known limitation
Only `.bms/.bme/.bml/.pms` are scanned and parsed; `.bmson` is unsupported and would need a dedicated parser.
