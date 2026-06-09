# BMS Renderer — Manual Test Checklist (Windows)

The automated suite (`test_bms_core.py`, 47 tests) covers all the pure logic:
parsing, the render engine, the cache, config, playlists, table files, the
read-only safety guard, and tag writing. Run it any time with:

```
python -m unittest test_bms_core
```

This document covers everything the automated tests **can't** reach — the GUI,
audio playback, Windows multiprocessing, and ffmpeg — which must be checked by
hand on a real Windows machine. Work through it before calling a build 1.0.

Mark each: ✅ pass / ❌ fail (note what happened) / ⏭ skipped.

---

## 0. Startup & environment
- [ ] App launches with `python bms_renderer.py` (and via `Run_BMS_Renderer.bat`).
- [ ] `bms_core.py` and `player.py` are sitting next to `bms_renderer.py` (the app
      won't start without them).
- [ ] No console errors on launch. Window opens at a sensible size.
- [ ] Close and relaunch — the window reopens at the **same size/position** as you
      left it.

## 1. Library scan & cache
- [ ] Point it at your real BMS library. First scan completes; progress shows in
      the log. (Expect this to be slow once — it builds the cache.)
- [ ] Close and relaunch — second launch is **fast** (reads cache, doesn't re-parse).
- [ ] Search/filter the library; sort by each column (title, artist, BPM, notes).
- [ ] Add a new chart folder to your library, relaunch — only the new charts are
      parsed; everything else is reused.
- [ ] **Library is never modified:** confirm no new files appear inside your library
      folder, and `bms_cache.db` / `bms_config.json` are created **next to the
      program**, not in the library.

## 2. Playback (audio)
- [ ] Double-click a song in the Library → it renders, then plays through speakers.
- [ ] Play/pause (▶/⏸) toggles correctly; Stop (■) stops.
- [ ] The timeline advances; dragging it seeks (silent while dragging, jumps on
      release).
- [ ] Volume slider changes loudness in real time.
- [ ] The `[♪]` marker shows on the now-playing row, and the title scrolls in the
      bottom-right if it's long.
- [ ] Transport glyphs render correctly in your font — **⏮ ▶ ⏸ ■ ⏭** should look
      like media buttons, NOT empty boxes/tofu. (If they're boxes, report it — we'll
      swap to plainer characters.)

## 3. Queue, tags & export
- [ ] Add charts to the Queue. Each shows in the Queue tab.
- [ ] Select a queued song → edit its tags (Title/Artist/Genre/BPM). Edits persist
      when you click away and come back.
- [ ] On the **Library** and **Tables** tabs, the tag fields are **greyed/read-only**
      (you can't type in them).
- [ ] Set an album-art image; confirm the preview shows.
- [ ] **Render All in Queue** → renders to your output folder. Each song leaves the
      queue as it finishes. Failures stay with an error in the log.
- [ ] Open a rendered FLAC in a player/tag editor: tags are present, Album = "BMS",
      cover art embedded, and **it sounds correct** (this is the real audio check the
      automated tests can't do).

## 4. Parallel rendering (Windows multiprocessing — HIGH RISK, test carefully)
- [ ] Queue a **small** batch (say 5 songs). Set Threads to 2. Render. Confirm all
      succeed and the app stays responsive.
- [ ] Watch for: pickling errors, workers that never start, the app hanging, or a
      flood of console errors. None should occur.
- [ ] Increase Threads to your CPU core count, queue a **larger** batch (50+). Confirm
      it's noticeably faster than Threads=1 and still completes cleanly.
- [ ] Only after the above pass: try a **big** batch (hundreds). Watch CPU/RAM. It
      should saturate cores during the render and release afterward.
- [ ] **Safety under load:** confirm nothing was written inside your library during
      any batch render.

## 5. Near-gapless playback
- [ ] In the **Queue**, play a song and let it finish → the next starts with little to
      no gap.
- [ ] Same in a **playlist** and in a **Table** (it should advance down the list).
- [ ] Skip forward (⏭) before a song ends → next song starts quickly (it was
      pre-rendered).
- [ ] Skip backward (⏮) → previous song plays.
- [ ] Right after a song starts, you may see a brief CPU/disk bump (neighbors
      pre-rendering) — that's expected; the app should stay responsive.

## 6. Shuffle & Loop
- [ ] **Loop ON** → the current song repeats endlessly. Turning it off lets playback
      advance/stop normally at the end.
- [ ] **Shuffle ON** in the Queue → the next track is random, AND the list scrolls to
      show the newly-playing row.
- [ ] Shuffle in a Table and a playlist behaves the same.
- [ ] On the **Library** tab, Shuffle/Loop have no unexpected effect (Library plays a
      single song to the end).

## 7. Difficulty Tables
- [ ] **Add table by URL…** → paste a real difficulty-table link. It fetches, names
      itself automatically, appears in the dropdown, and loads.
- [ ] The added table persists in `tables.json` and is still there after relaunch.
- [ ] Try a **bad URL** → it fails gracefully (status message, no crash).
- [ ] Try adding the **same URL twice** → it's rejected as a duplicate.
- [ ] Charts grouped by level; owned charts normal, **missing** ones greyed.
- [ ] Double-click an owned chart → plays. Right-click → Play / Add to Queue work.
- [ ] Right-click a **level header** → "Add all in this level to Queue" adds that
      level's songs (one per folder).
- [ ] **Add all owned to Queue** queues the whole table (one chart per song).

## 8. Custom Playlists
- [ ] Create a playlist; right-click songs → Add to playlist.
- [ ] Each playlist is its own file in a `Playlists` folder next to the program.
- [ ] Rename and delete playlists; confirm the files update/disappear.
- [ ] **Portability:** copy one playlist `.json` to a friend's setup (or simulate by
      renaming a file) → owned charts resolve by hash; un-owned show greyed.
- [ ] Render a playlist → outputs land in a subfolder named after the playlist.

## 9. Formats & ffmpeg
- [ ] With **ffmpeg installed and on PATH:** the OGG option appears in the format
      dropdown; an OGG render produces a playable, tagged .ogg.
- [ ] FLAC render with ffmpeg present → log says "level 8"; without ffmpeg → "level 5".
- [ ] Without ffmpeg: OGG is absent from the dropdown; FLAC/WAV still work.
- [ ] If you have `.ogg` **keysounds** that previously failed to decode, note whether
      they still fail (this is a libsndfile issue, tracked separately).

## 10. Robustness / edge cases
- [ ] Open a malformed or empty `.bms` → the app logs an error and stays running (no
      crash).
- [ ] A chart whose audio files are missing → renders what it can, logs the missing
      sounds.
- [ ] Very large library (100k+ charts) → the list caps at 2000 shown with a note;
      searching narrows it.
- [ ] Minimize and restore the window during playback → no crash; timeline catches up.

---

### Reporting
For any ❌, copy the text from the in-app log box and the console window (it stays
open on error) and send it along with what you did right before.
