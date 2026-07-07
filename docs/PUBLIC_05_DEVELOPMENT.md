# Development Notes

## Running
- Run the app: `python bms_renderer_qt.py`
- Run the engine tests: `python3 -m unittest test_bms_core`
- Smoke-test the GUI without a display (e.g. in CI): `QT_QPA_PLATFORM=offscreen python3 -c "import bms_renderer_qt as M; from PyQt5.QtWidgets import QApplication; app=QApplication([]); M.MainWindow()"`

## Dependencies
Required: PyQt5, numpy, soundfile, mutagen. In-app playback additionally needs sounddevice. Optional: soxr (high-quality anti-aliased resampling — required for release-quality audio; without it resampling falls back to plain linear interpolation), pypresence (Discord Rich Presence), Pillow (cover-art handling). `ffmpeg` must be on PATH for OGG/MP3/FLAC encoding and all BGA video.

### PortAudio on Linux
In-app playback uses PortAudio through sounddevice. On Linux, `pip install sounddevice` does not bundle PortAudio (it bundles it only on Windows and macOS), so the system library must be installed separately: `sudo apt install libportaudio2` (Debian/Ubuntu), `sudo pacman -S portaudio` (Arch), or `sudo dnf install portaudio` (Fedora). A "playback unavailable" message almost always means this library is missing, not that there is no audio device — PipeWire and PulseAudio both work through PortAudio. Offline rendering works without it; only in-app playback requires it.

## Testing notes
The engine is designed to be tested without an audio device or display. Audio and BGA encoding can be verified end-to-end with ffmpeg using synthetic assets (sine-wave WAVs, solid-colour PNGs). GUI construction can be verified headlessly with the offscreen Qt platform. Anything involving actual sound output, the on-screen appearance, or real chart renders (whose keysound and image assets must be present on disk) needs a real environment to confirm.

## Behaviours that are intentional (not bugs)
- **Keysound de-duplication:** the same keysound at the same instant plays once. A long sample retriggering and overlapping itself at *different* times is intended and preserved.
- **BGA overlay compositing:** channel 07 is composited over channel 04 with black treated as transparent; charts that animate on 07 over a static base are common.
- **Image-sequence preference:** a chart defining both a video BGA and an image fallback sequence renders the image sequence.
- **Default-preserving options:** unchanged encode/quality settings reproduce the exact prior output. Keep this property when adding options.

## Cross-platform notes
- **Filename case:** charts authored on Windows reference filenames case-insensitively and often name a `.wav` that ships as `.ogg`. File lookups resolve case-insensitively and across extensions so charts render correctly on case-sensitive filesystems. New file lookups should use the same approach.
- **Long paths:** very long file paths (over 260 characters) can fail on Windows and cause charts to be skipped during scanning.

## Theming
The app uses the Fusion style application-wide and switches themes by swapping the application palette only — never by switching the style at runtime, and never by assigning palettes to individual widgets (which detaches them from the application palette). New muted/grey labels should be styled through the application stylesheet rather than with hardcoded colours, so they adapt to both themes.

## Performance
The target library is very large (on the order of 126,000 charts). Anything that touches scanning, list population, or per-row work should be written to scale to that size — for example, the now-playing marker tracks only the marked rows rather than iterating the whole list.

The BGA video renderer's per-image and per-frame caches are memory-bounded (LRU under a byte cap) rather than unbounded: a long chart can reference around a thousand distinct BGA frames, and caching every one as raw pixels would exhaust memory during parallel renders. Keep them bounded when modifying that path.

## Process pool
The offline render job functions must remain top-level and picklable, and each must re-establish the library root in its worker process and call the output-safety guard. The entry point initialises multiprocessing support before constructing the window. The pools use the **spawn** start method (not fork): on Linux, forking duplicates the Qt/X11 application state into workers and causes segfaults on teardown. Because spawn re-imports the main module, all executable startup code stays under the `if __name__ == "__main__"` guard. Aborting a render terminates the worker processes immediately rather than letting in-flight jobs run to completion.

## References
- BMS command specification: `https://hitkey.nekokan.dyndns.info/cmds.htm`
- Reference implementations: bmx2wav (the timing model this engine follows), beatoraja, jbms-parser
- The original Tkinter implementation (`bms_renderer.py`) is kept as a behavioural reference.

## Utilities
`scan_diagnostic.py` reports a per-extension breakdown of chart files on disk and flags files the scanner ignores or cannot read — useful for diagnosing a chart-count that is lower than expected (commonly caused by `.bmson` files, which are not supported).
