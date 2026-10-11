# Development Notes

## Running
- Run the app: `python bms_renderer_qt.py`
- Run the engine tests: `python3 -m unittest test_bms_core`
- Smoke-test the GUI without a display (e.g. in CI): `QT_QPA_PLATFORM=offscreen python3 -c "import bms_renderer_qt as M; from PySide6.QtWidgets import QApplication; app=QApplication([]); M.MainWindow()"`

## Dependencies
Required: PySide6, numpy, soundfile, mutagen. In-app playback additionally needs sounddevice. Optional: soxr (high-quality anti-aliased resampling — required for release-quality audio; without it resampling falls back to plain linear interpolation), pypresence (Discord Rich Presence), Pillow (cover-art handling). `ffmpeg` is required for OGG/MP3/FLAC encoding and all BGA video; it is found on PATH **or** beside the program (binary releases ship it next to the executable).

### PortAudio on Linux
In-app playback uses PortAudio through sounddevice. On Linux, `pip install sounddevice` does not bundle PortAudio (it bundles it only on Windows and macOS), so the system library must be installed separately: `sudo apt install libportaudio2` (Debian/Ubuntu), `sudo pacman -S portaudio` (Arch), or `sudo dnf install portaudio` (Fedora). A "playback unavailable" message almost always means this library is missing, not that there is no audio device — PipeWire and PulseAudio both work through PortAudio. Offline rendering works without it; only in-app playback requires it.

## Testing notes
The unit tests are the only caller of a few engine helpers, so a symbol that looks unreferenced from the four shipped modules may not be dead — check the test suite before removing anything.

The engine is designed to be tested without an audio device or display. Audio and BGA encoding can be verified end-to-end with ffmpeg using synthetic assets (sine-wave WAVs, solid-colour PNGs). GUI construction can be verified headlessly with the offscreen Qt platform. Anything involving actual sound output, the on-screen appearance, or real chart renders (whose keysound and image assets must be present on disk) needs a real environment to confirm.

## Behaviours that are intentional (not bugs)
- **Keysound de-duplication:** the same keysound at the same instant plays once. A long sample retriggering and overlapping itself at *different* times is intended and preserved.
- **BGA overlay compositing:** channel 07 is composited over channel 04 with black treated as transparent; charts that animate on 07 over a static base are common.
- **Image-sequence preference:** a chart defining both a video BGA and an image fallback sequence renders the image sequence.
- **Default-preserving options:** unchanged encode/quality settings reproduce the exact prior output. Keep this property when adding options.
- **Container switching:** only the all-default codec combination stays `.mp4`; anything else uses `.mkv`, which accepts any codec pairing. This is deliberate — several combinations (Ogg audio, for one) cannot be muxed into `.mp4` at all, and others only work in recent mp4 extensions that not every player supports.
- **Remux is video-BGA only:** an image-sequence BGA has no video stream to copy, so it is always encoded even when lossless remux is on. In a mixed batch the video-codec settings therefore still apply to those charts.
- **Frozen first frame on an offset remux:** when a copied video starts partway in, players hold its first frame over the gap rather than showing black. Filling that gap with real black frames would require encoding them, which would defeat a lossless copy. Sync is unaffected; the trade is deliberate.
- **BGA detection trusts the chart:** a declared-but-missing or placeholder video is still classified as `video`. A hardening pass was written and deliberately reverted; it is deferred.

## What cannot be verified without real hardware
Be explicit when a change depends on any of these, because a sandbox or CI run cannot confirm them:
- **NVIDIA/NVENC hardware.** An earlier probe used a 64×64 test frame and reported "no GPU" on a working card — NVENC enforces a minimum width (145 for H.264 on Turing and later). Always probe at a realistic resolution.
- **Audio output.** Headless environments have no sound device, so playback paths need confirming on real hardware.
- **Pixel-exact layout.** Offscreen renders have repeatedly disagreed with the real desktop. Prefer runtime-measured geometry over guessed constants, and confirm layout changes against a real screenshot.

## Cross-platform notes
- **Filename case:** charts authored on Windows reference filenames case-insensitively and often name a `.wav` that ships as `.ogg`. File lookups resolve case-insensitively and across extensions so charts render correctly on case-sensitive filesystems. New file lookups should use the same approach.
- **Long paths:** very long file paths (over 260 characters) can fail on Windows and cause charts to be skipped during scanning.

## Theming
The app uses the Fusion style application-wide and switches themes by swapping the application palette only — never by switching the style at runtime, and never by assigning palettes to individual widgets (which detaches them from the application palette). New muted/grey labels should be styled through the application stylesheet rather than with hardcoded colours, so they adapt to both themes.

## Performance
The target library is very large (on the order of 126,000 charts). Anything that touches scanning, list population, or per-row work should be written to scale to that size — for example, the now-playing marker tracks only the marked rows rather than iterating the whole list.

The BGA video renderer's per-image and per-frame caches are memory-bounded (LRU under a byte cap) rather than unbounded: a long chart can reference around a thousand distinct BGA frames, and caching every one as raw pixels would exhaust memory during parallel renders. Keep them bounded when modifying that path.

**Measure allocation, not just elapsed time.** Several hot paths here run once per worker process, so a transient copy is multiplied by the render thread count. Two concrete cases: the render's peak-normalisation compares max against negated min instead of taking `abs()` of the whole mix, avoiding a full duplicate of the audio; and the moodbar's batched FFT is done in fixed-size chunks, because processing the whole track at once was marginally faster but allocated an order of magnitude more memory for a purely cosmetic widget.

**Beware silently-swallowed fallbacks around optimisations.** Where a fast path is wrapped in a broad `except` with a slower but correct fallback, breaking the fast path produces no symptom other than being slow — one such fast path was inoperative for an entire Qt binding migration. Prefer logging when a fallback is taken.

## Progress reporting
Render jobs run in separate processes, so progress crosses the boundary by **file**: each job writes its 0–1 fraction to a small file in a temporary directory and the progress dialog polls it. This was chosen over a manager/queue because it is simple and spawn-safe, and a job that reports nothing simply falls back to per-item counting. Hook points inside `render_bms` are deliberately coarse — once per keysound file while decoding, once per distinct clip while mixing — never per sample. The video paths get their fraction from ffmpeg's own `-progress` stream.

## Process pool
The offline render job functions must remain top-level and picklable, and each must re-establish the library root in its worker process and call the output-safety guard. The entry point initialises multiprocessing support before constructing the window. The pools use the **spawn** start method (not fork): on Linux, forking duplicates the Qt/X11 application state into workers and causes segfaults on teardown. Because spawn re-imports the main module, all executable startup code stays under the `if __name__ == "__main__"` guard. Aborting a render terminates the worker processes immediately rather than letting in-flight jobs run to completion.

## References
- BMS command specification: `https://hitkey.nekokan.dyndns.info/cmds.htm`
- Reference implementations: bmx2wav (the timing model this engine follows), beatoraja, jbms-parser

## Diagnosing a low chart count
If the scanner reports fewer charts than expected, the usual cause is `.bmson` files, which are not supported. Very long paths (over 260 characters on Windows) can also cause charts to be skipped.
