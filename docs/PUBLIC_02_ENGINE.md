# Engine Reference (`bms_core.py`)

Pure logic — no Qt, no audio device. The source of truth for parsing, rendering, BGA, tagging, scanning, persistence, and table fetching.

## Constants
`SR = 44100`. `BMS_EXTS = (.bms, .bme, .bml, .pms)`, `AUDIO_EXTS`, `IMAGE_EXTS`, `VIDEO_EXTS`. `APP_VERSION` (the app version string; single source, surfaced in the window title). Paths under `program_dir()`: `DB_PATH`, `CONFIG_PATH`, `TABLES_PATH`, `PLAYLISTS_DIR` (legacy `bms_cache.json`/`playlists.json` auto-migrate).

## Library root & safety
`set_library_root(path)` sets the module root. `assert_safe_output(out)` raises if the output is inside the library (every render job calls it). `program_dir()` is the base for all persistence paths.

## Helpers
`b36(s)` (base-36 → int); `read_bms_text(path)` reads a chart trying **Shift-JIS first** then UTF-8 fallbacks (always use it for chart text); `ffmpeg_path()` (searches PATH **and** the program's own directory, because binary releases ship ffmpeg beside the executable — `shutil.which` alone would miss it whenever the working directory isn't the app folder); `file_md5(path)`; channel classifiers.

## File resolution (case-insensitive)
BMS charts are authored on Windows (a case-insensitive filesystem) and frequently reference, say, `BD_000.wav` when the file on disk is `bd_000.ogg`. On case-sensitive filesystems (Linux/macOS) an exact-case lookup misses, every such keysound goes silent, and the render comes out nearly empty. To prevent this:
- `find_audio(folder, name)` / `find_image(folder, name)` try the exact name (fast path), then swap among the known extensions, then fall back to a **case-insensitive** match of both the exact name and each candidate extension.
- `_dir_index(folder)` builds a `{lowercase_name: real_name}` map, cached per folder for the process (a song folder is static during a render, and the case-insensitive path only runs after the exact and extension-swap lookups miss). `clear_dir_index_cache()` drops the cache if folder contents change.

## Parsing
Object ids are base-36 by default, or base-62 (case-sensitive) when the chart declares `#BASE 62`. `parse_bms(path)` returns `{header, wav_table, bpm_table, stop_table, lnobj, bars, has_random, is_pms}`, where `bars` is `{measure → {channel → [payload, …]}}`. `#RANDOM`/`#SWITCH` control flow is implemented, with branch selection deterministically seeded by the chart's filename so repeat renders are identical.

`detect_bga(path)` returns `{type: sequence|static|video|none, frames, images}`. It counts BGA events on **both** the base layer (channel 04) and the overlay layer (channel 07), since many charts animate entirely on 07 over a static or empty 04. It classifies by comparing image events to video events on the timeline rather than by the mere presence of a video definition — a chart may define both a video BGA and a full image fallback sequence, and the image sequence wins. `"video"` is returned only when there is no usable image sequence. It reads the chart in a single pass, collecting the image table and the placed events together; this runs once per queued chart when a BGA export is set up, so a second walk over every line was pure overhead.

**Known limitation:** classification trusts what the chart *declares*. A chart can name a video BGA that was never shipped, or ship a tiny black placeholder in its place; either is still reported as `"video"`, so the render then fails with "no video BGA found" or produces a black picture. A hardening pass (verifying the referenced file exists on disk and clears a minimum size) was written and deliberately reverted — it is deferred, not overlooked.

`video_bga_info(path)` returns what is needed to render a *video* BGA: `{video_file, start_sec, total_sec, has_image_overlay, warnings}`, or `None` if there is no usable video BGA. It reuses the same tempo/measure accumulator as `bga_timeline`, so the video's start time lines up with the audio exactly rather than assuming the video begins at t=0. `_find_bga_file(folder, name)` resolves the referenced file tolerantly, since charts routinely name a `.bmp` that ships as `.mp4` or `.wmv`.

`nvenc_available(encoder)` reports whether NVENC hardware encoding actually works on this machine, by running a throwaway encode and checking the exit code — ffmpeg lists these encoders whether or not an NVIDIA GPU is present, so listing alone proves nothing. The probe encodes a **1280×720** frame deliberately: NVENC enforces a minimum resolution (Turing and later require a width of at least 145 for H.264, more for HEVC), so probing with a small frame makes working hardware report failure. `nvenc_reason(encoder)` returns ffmpeg's own error from the last failed probe, so a negative result is diagnosable rather than mysterious. Results are cached per encoder.

`predicted_container(opts)` returns the extension a given options dict will actually produce (`.mp4` or `.mkv`), so the UI can show it before rendering rather than surprising the user afterwards.

`count_playable_notes` and `detect_mode_from_bars` derive the note count and play mode.

## Scanning & caching
`db_connect()` opens the SQLite cache (`charts` table keyed by path). `scan_library(root, conn, log, progress, should_stop=None)` walks the library, parses changed/new charts, removes vanished ones, and returns `(songs, stats)`.

Parsing is distributed across **worker processes**, not threads: a cold scan is CPU-bound (parsing plus note-counting), so threads were limited by the GIL and their database contention could stall the interface at full library scale. Processes avoid both; they return plain row tuples and **all database writes stay on the calling thread**. Small updates (fewer than 200 changed charts) stay sequential, and any failure to start the pool falls back to sequential parsing so a scan can never be left broken.

Each chart is read **once**: the raw bytes are hashed for the MD5 and the decoded text is handed to `parse_bms(path, text=…)`, rather than reading the whole file separately for each purpose.

`should_stop` is an optional predicate polled at coarse points, so the application can cut a long scan short when it is closing. Work already parsed is committed, so the next scan resumes from there. An interrupted scan deliberately **skips the vanished-chart cleanup**: only part of the tree was walked, so every unvisited chart would otherwise look missing and be removed from the cache.

The walk holds only a set of on-disk paths to keep memory bounded on very large libraries. The reported total is the raw count of files matching the scanned extensions. (Note: charts whose paths exceed the Windows 260-character limit may fail to stat and be skipped.)

## Persistence & table fetching
`load_config`/`save_config`, `load_tables_file`/`save_tables_file`, and the playlist CRUD functions persist to the paths above. `load_config` is non-destructive if the config file is corrupt: a file that exists but won't parse is moved aside to `bms_config.json.bad` rather than being treated as empty, so a later save can't overwrite and wipe the user's settings. For difficulty tables, `_http_get` retries with an unverified SSL context on certificate errors (some table hosts have expired certs), and `fetch_table(url)` prepends `https://` when the scheme is missing and follows the standard chain (`<meta name="bmstable">` → `header.json` → `data.json`), resolving entries to library charts by MD5.

## Audio rendering
`render_bms(path, log)` returns a float32 stereo numpy array at `SR` plus the parsed header, using the timing model above. Long notes sound only at the start; `#LNOBJ`-marked notes are silent; identical `(sample position, keysound)` pairs are de-duplicated so a sound at a given instant plays once (genuine layering — the same sound at different times, or different sounds together — is preserved); and the mix is peak-normalised to within [-1, 1]. The peak is found by comparing the maximum against the negated minimum rather than by taking the absolute value of the whole mix, which would allocate a full second copy of the audio (tens of megabytes per render, multiplied by every worker process in a parallel batch) purely to read one number. Keysounds whose sample rate differs from the output rate are resampled with anti-aliasing via `soxr` (with a plain linear-interpolation fallback only if soxr isn't installed); same-rate keysounds are used as-is.

The encoders write a temporary WAV and invoke ffmpeg. Each accepts an optional quality argument that defaults to the original behaviour: OGG `-q:a 6`, FLAC `-compression_level 8`, MP3 320 kbps CBR. `write_tags_to_file` embeds metadata and cover art via mutagen; `process_cover` normalises an image to embeddable bytes.

## Offline render jobs
These run in worker processes, so they are top-level and picklable, and each restores the library root and calls `assert_safe_output`.

`render_one_job(job)` — `job = (in_path, out_path, fmt, tags, cover_bytes, ffmpeg, library_root [, quality [, progress_path]])`. The 8th element overrides encoder defaults (`{"flac_level":…}`, `{"ogg_q":…}`, or `{"mp3_vbr_q":…}`); the optional 9th is a path this job writes its 0–1 progress fraction to. Returns `(out_path, title, None | error)`.

`render_bms(path, log, progress=None)` accepts an optional progress callback. It is invoked once per keysound *file* while decoding and once per distinct clip while mixing — deliberately coarse hook points, never per sample, so there is no measurable overhead. It defaults to `None`, so the live-playback path is unaffected.

`bga_timeline(path)` returns `(events, total_seconds, missing, layer_events)` — the base-layer and overlay-layer event lists, each `[(seconds, image_path_or_None), …]`, using the same accumulator as `render_bms`.

`_bga_encode_args(opts, out_path)` translates BGA export options into ffmpeg arguments and the actual output path. With no options (or video and audio both "default") it reproduces the original H.264 4:2:0 + AAC 192k in an MP4; any other combination outputs to MKV for broad codec compatibility. Video codecs: x264 (software/NVENC), HEVC (software/NVENC), AV1, VP9. Audio: AAC, WAV (PCM), FLAC, OGG (VBR), MP3 (VBR). Opus is intentionally omitted (it requires 48 kHz; resampling is out of scope). The audio half is factored into `_audio_encode_args(opts)` and shared with the muxed/remux path, so every audio codec behaves identically no matter which render path produced the video.

`render_bga_video_job(job)` — `job = (in_path, out_path, ffmpeg, library_root, fps, size [, encode_opts [, n_workers [, progress_path]]])` (where `size` is a `(width, height)` tuple). Because this path pipes frames itself, it knows exactly how many it has written and reports true progress essentially for free (capped just short of 100% while ffmpeg flushes and finalises the file). It renders the audio, builds the base and overlay BGA timelines, composites the overlay (channel 07) over the base (channel 04), and pipes raw frames to ffmpeg with the audio muxed in. The returned output path reflects the actual file written (which may be `.mkv`). If an NVENC (hardware) video encode fails — most commonly because consumer NVIDIA GPUs limit the number of simultaneous NVENC sessions, so a large parallel batch exhausts them and the excess encoders produce no output — the job automatically retries once with the equivalent software encoder, so those charts still render (on CPU) instead of failing.

The compositing follows beatoraja's layer model. If an overlay image carries its own alpha channel, that alpha is used as-is — some layers define their shape entirely in alpha (and may store inverted colour underneath), so re-deriving transparency from the colour data would corrupt them. If an overlay has no alpha (a plain BMP or JPEG), black is the transparent colour and every non-black pixel is fully opaque — a simple binary key. The black threshold is kept tight (only true, near-pure black keys out) because BMS layers use pure black as the transparent colour and dark-but-coloured content sits just above it: a looser threshold would wrongly erase things like dark-brown hair. It deliberately does **not** scale opacity by pixel brightness: a layer's translucency is part of its artwork, and deriving it from brightness would wash out bright, fully-opaque overlays. Transparency is keyed at the source resolution and scaled together with the image so edges stay clean without dark fringing. Base images are likewise composited over black using their alpha when present, so a transparent base (for example white title text on transparency) shows its content over black rather than being flattened. Scaling, keying, and compositing are each cached per unique image and per unique base/overlay pair, so the work happens once rather than per output frame. These caches are **memory-bounded** (least-recently-used, under a byte cap) rather than unbounded: a long chart can reference on the order of a thousand distinct BGA frames, and holding every scaled image and composited frame as raw pixels would grow to several gigabytes and exhaust memory during parallel renders (which manifested as a killed worker and ffmpeg reporting it received no packets — a zero-byte output). The caps also **scale down with the render's worker count**, so the total memory across all worker processes stays bounded no matter how many run at once — without this, a heavy chart that renders fine on its own could still exhaust memory when many workers each cache a large BGA simultaneously. Because playback is sequential, only a small working set is live at any moment, so the LRU keeps the per-frame speedup while capping memory; an evicted frame simply recomposites identically the next time it is needed.

## Video-BGA rendering (mux and lossless remux)

`render_bga_muxed_job(job)` — `job = (in_path, out_path, ffmpeg, library_root, size [, encode_opts [, n_workers [, progress_path]]])`. It handles charts whose BGA **is** a video file, by muxing that video with freshly rendered chart audio. It returns a **4-tuple**, `(out_path, title, None | error, warnings)` — note the extra slot versus the image-sequence job, carrying per-chart warnings (timing offsets, overlay layers, fallbacks) so edge cases surface in the log instead of failing the render.

Behaviour:
- The video is delayed to its chart-triggered start time, so it stays in sync with the audio rather than assuming it starts at t=0.
- Output length follows the **audio** — the song is the point. If the video ends first, the tail goes black.
- The video's own embedded audio track (some WMVs carry one) is dropped in favour of the rendered chart audio.
- Dimensions are forced even, an H.264 requirement.

**Lossless remux** (`opts["remux"]`) copies the source video stream verbatim (`-c:v copy`) instead of re-encoding, preserving the original picture exactly. It forces `.mkv`, which accepts the native codecs BMS BGAs actually use (wmv2, vc1, h264 and friends — several of which cannot go in an `.mp4` at all). Two details matter:

- The start offset is applied with `-itsoffset`, a timestamp shift, so the stream stays a copy. `-fflags +genpts` is required alongside it: some sources emit packets with unset timestamps, which the mkv muxer rejects outright — the symptom is ffmpeg's generic "Conversion failed!" with the real cause buried above it.
- A video shorter than the song needs no padding. Its stream simply ends and players render black while the audio continues — an "organic" black tail with zero re-encoding.

**Remux applies only to video BGAs.** An image-sequence BGA has no stream to copy and must still be encoded, so in a mixed batch the video-codec settings remain live and apply to those charts. The worker strips the `remux` flag from image-sequence jobs for exactly this reason.

If a stream copy fails for any reason, the job does **not** fail the chart: it falls back to re-encoding that one and records why in its warnings. That fallback still writes `.mkv`, matching the container the export dialog predicted when lossless remux was selected.

Both video-BGA jobs write a temporary WAV of the rendered audio before encoding, and remove it in a `finally` block, so a failure part-way through cannot leave a full-length scratch file behind in the output folder.

## Progress reporting

`_Progress` writes a job's 0–1 fraction to a file, throttled so a tight loop doesn't hammer the disk. `_run_ffmpeg_progress(cmd, total_seconds, prog)` runs an ffmpeg command with `-progress pipe:1` and translates the reported output timestamp into that fraction — this is what makes a slow single-file encode (AV1 especially) show continuous movement. `_ffmpeg_error_reason(stderr)` extracts the meaningful error line from ffmpeg's output instead of its generic trailing "Conversion failed!".

## Known limitation
Only `.bms/.bme/.bml/.pms` are scanned and parsed; `.bmson` is unsupported and would need a dedicated parser.
