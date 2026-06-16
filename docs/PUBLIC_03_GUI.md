# GUI Reference (`bms_renderer_qt.py`)

The PyQt5 frontend: the main window, dialogs, custom widgets, background workers, and the entry point.

## Startup
`main()` enables multiprocessing support, sets the **Fusion style once** (theming depends on this), installs the application stylesheet (button padding and muted-label colour), and shows the main window. The logo and app icon are embedded as base64. The player and Discord modules are imported defensively, so the app runs if either is unavailable.

## Custom widgets
- `CollapsibleAlbumView` — the sectioned, collapsible album grid used in several tabs.
- `MarqueeLabel` — the now-playing title; it inherits the palette text colour (so it reads correctly in both light and dark themes).
- `WaveformBar` — a click-to-seek waveform under the transport.
- `VolumeTriangle` — a triangular volume control.
- `_CodecPerfGraphic` — a three-bar codec guide (Quality / Efficiency / Speed), where a longer bar is always better; the middle bar is size *efficiency*, so a long bar means smaller files.

## Background workers
All are `QThread` subclasses that run at low priority and emit signals back to the GUI:
- `ScanWorker` — runs the library scan.
- `RenderWorker` — offline audio render; builds job tuples and runs them in a process pool; resolves cover art per item.
- `BGAWorker` — offline BGA video render; filters to charts with a renderable image BGA and reports a corrected total once filtering is done.
- `PlayWorker` — renders one chart to a buffer for in-app playback.
- `ThumbWorker` — loads grid thumbnails.
- A BGA-detection worker — runs `detect_bga` off the GUI thread for the right-panel indicator.
- A table-fetch worker — runs `fetch_table` off-thread.

`_apply_process_priority` sets the render process priority (Windows `SetPriorityClass` / POSIX `nice`); High and Above-Normal may require elevation on Windows.

The offline render workers create their process pool with the **spawn** start method. On Linux the default fork method duplicates the application's Qt/X11 state into each worker, which causes segfaults when the pool or the app tears down; spawn uses fresh interpreters and avoids this. Aborting a render terminates the worker processes rather than waiting for in-flight jobs to finish.

## Dialogs
- **BGA export** (opened by "Render All BGA in Queue") — video and audio codec dropdowns, bitrate sliders mirrored to manual spin boxes, the codec guide graphic, and a process-priority dropdown. Selecting FLAC switches the audio control to a compression-level slider (0–8, default 8); WAV disables it. OGG gets a wider bitrate range (about 45–500 kbps) than the MP3/AAC range (64–320), reflecting what Vorbis can actually encode. "Default" shows the baseline value but locks it as an indicator. Returns the encode options and priority.
- **Audio export** (opened by "Render All in Queue" and "Render Playlist") — format, thread count, a format-specific quality control, and process priority. FLAC uses compression 0–8 (default 8); OGG uses VBR quality 0–10 (default 6); MP3 runs left = smallest file to right = best (320 kbps CBR, the default at the far right); WAV has no quality control. It returns quality only when the settings differ from the defaults, so unchanged settings produce identical output.
- **Render progress** — a modal dialog with a green progress bar, used by the audio, playlist, and BGA renders. It includes an **Abort** button that stops the render: it cancels queued work and terminates the worker processes already running (an aborted render may leave a partial output file, since it is stopped mid-write).
- **Art viewer** — a resizable window opened by clicking the right-panel art preview. A scaling dropdown in the top-left offers smooth, nearest-neighbour, and 1:1 actual-size modes; the choice auto-saves. The image scales to the window (1:1 shows true pixels in a scrollable view), and the window remembers its size and position between sessions. It is modeless, so playback continues while it is open.

## Main window
A five-tab layout — Library, Discovery, Tables, Custom Playlists, Queue — with the dark-mode toggle in the top-right corner of the tab bar. The right panel holds the tag editor, the BMS-information panel, the per-song art picker with a BGA indicator, and the whole-queue cover controls. The transport bar holds the playback controls, audio-device button, waveform, volume control, and shuffle/loop toggles, with a log pane below.

### Theming
The app uses the Fusion style application-wide, set once at startup; **the style is never switched at runtime** — light and dark are a palette swap only. Applying a theme swaps the application palette and then re-polishes every widget (`unpolish`/`polish`); it does **not** assign palettes to individual widgets, because doing so permanently detaches a widget from the application palette. Muted/grey labels are styled through the application stylesheet so they remain readable in both themes. Buttons share a common padding, minimum height, and icon size so text-only and icon buttons match; the compact transport/art icon buttons opt out via an object name.

### Playback and media keys
The transport methods start, stop, advance, and seek playback, honour shuffle/loop, and poll for auto-advance. Media keys are bound through application-scoped shortcuts (they fire when the app has focus, not system-wide): play/pause, stop, next/previous, volume up/down, and mute, with Ctrl-based fallbacks including Ctrl+Space for play/pause. The now-playing marker tracks only the marked rows rather than scanning the whole list, which matters at large library sizes.

### Tabs
The Library tab is a flat sortable table; the numeric columns (BPM, Notes) sort by value rather than as text. Discovery is an art grid. Tables shows difficulty tables; in album view each level is a separate grid, and both the "Add selected" button and the right-click menu gather the selection across **all** level grids so multi-selection spans levels. Custom Playlists and Queue manage their respective collections; their render buttons open the audio-export dialog and show the progress dialog. Thread-count and format selection live in the export dialogs.

Because the Custom Playlists table is sortable, each row remembers which playlist entry it represents rather than relying on its visual position — after a sort, row order no longer matches the saved playlist order, so selection, play, render, and remove all look the entry up by that stored identity. This keeps the info panel showing the selected song and ensures remove deletes the right entry. The "?" context-help button is disabled application-wide, so it no longer appears in dialog title bars on Windows.

### Tags and art
Tag fields auto-save edits. The per-song art picker cycles through a folder's images, supports assigning a plain black square as a cover, and (when clicked) opens the art viewer. The BGA indicator shows whether the selected chart has a renderable sequence BGA. Cover art can be set per song or for the whole queue.
