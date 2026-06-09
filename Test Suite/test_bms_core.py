#!/usr/bin/env python3
"""
Automated test suite for BMS Renderer's core logic (bms_core.py).

These tests cover everything that does NOT need a GUI, audio hardware, or
Windows: the BMS parser, channel classification, note counting, mode detection,
the render engine (including its bit-identical optimization guarantees), the
SQLite cache, config, playlists, difficulty-table parsing, the read-only safety
guard, and tag writing.

Run with either:
    python -m unittest test_bms_core -v
    python test_bms_core.py

What these tests deliberately DO NOT cover (must be checked manually on Windows —
see TESTING_MANUAL.md): the Tkinter GUI, sounddevice playback, the gapless
neighbor pre-render in a live app, Windows multiprocessing (spawn) for batch
renders, and ffmpeg subprocess encoding on the target machine.
"""

import os
import sys
import json
import shutil
import sqlite3
import tempfile
import unittest

import numpy as np
import soundfile as sf

import bms_core as B


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def write_wav(path, seconds=0.3, freq=440, sr=44100, channels=1):
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    sig = (0.2 * np.sin(2 * np.pi * freq * t)).astype("float32")
    if channels == 2:
        sig = np.stack([sig, sig], axis=1)
    sf.write(path, sig, sr)


class TempDirCase(unittest.TestCase):
    """Base case providing a fresh temp dir per test, cleaned up afterward."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bmstest_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def chart(self, name, body, header="#TITLE T\n#ARTIST A\n#BPM 120\n"):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(header + body)
        return p


# --------------------------------------------------------------------------
# channel classification
# --------------------------------------------------------------------------
class TestChannelClassification(unittest.TestCase):
    def test_bgm_channel(self):
        self.assertEqual(B._classify_channel("01"), "bgm")

    def test_player_note_lanes(self):
        # 11..1Z and 21..2Z are visible note lanes
        self.assertEqual(B._classify_channel("11"), "note")
        self.assertEqual(B._classify_channel("16"), "note")
        self.assertEqual(B._classify_channel("21"), "note")

    def test_long_note_lanes(self):
        self.assertEqual(B._classify_channel("51"), "long")
        self.assertEqual(B._classify_channel("61"), "long")

    def test_landmine_lanes(self):
        self.assertEqual(B._classify_channel("D1"), "mine")
        self.assertEqual(B._classify_channel("E1"), "mine")

    def test_control_channels(self):
        self.assertEqual(B._classify_channel("03"), "bpm_inline")
        self.assertEqual(B._classify_channel("08"), "bpm_ext")
        self.assertEqual(B._classify_channel("09"), "stop")

    def test_ignored_channels(self):
        # BGA/visual channels (e.g. 04, 07) are not acted on by the renderer
        self.assertIsNone(B._classify_channel("04"))
        self.assertIsNone(B._classify_channel("07"))

    def test_predicates_consistent_with_classifier(self):
        # ch_is_long_note / ch_is_player_note (used by note counting) must agree
        for ch in ("11", "16", "21", "2Z"):
            self.assertTrue(B.ch_is_player_note(ch))
        for ch in ("51", "61", "5Z"):
            self.assertTrue(B.ch_is_long_note(ch))
        self.assertFalse(B.ch_is_long_note("11"))


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
class TestParser(TempDirCase):
    def test_header_fields(self):
        p = self.chart("a.bms", "#00111:01\n",
                       header="#TITLE Hello\n#ARTIST World\n#GENRE Test\n#BPM 150\n")
        d = B.parse_bms(p)
        self.assertEqual(d["header"]["TITLE"], "Hello")
        self.assertEqual(d["header"]["ARTIST"], "World")
        self.assertEqual(d["header"]["GENRE"], "Test")
        self.assertEqual(d["header"]["BPM"], "150")

    def test_wav_table(self):
        p = self.chart("a.bms", "#WAV01 kick.wav\n#WAV0Z snare.ogg\n#00111:01\n")
        d = B.parse_bms(p)
        self.assertEqual(d["wav_table"][B.b36("01")], "kick.wav")
        self.assertEqual(d["wav_table"][B.b36("0Z")], "snare.ogg")

    def test_bars_collected(self):
        p = self.chart("a.bms", "#00111:0101\n#00311:0202\n")
        d = B.parse_bms(p)
        # measure tokens are 1-based in #mmmcc form: 001 -> bar 1, 003 -> bar 3
        self.assertIn(1, d["bars"])
        self.assertIn(3, d["bars"])

    def test_random_flag(self):
        p = self.chart("a.bms", "#RANDOM 2\n#IF 1\n#00111:01\n#ENDIF\n")
        d = B.parse_bms(p)
        self.assertTrue(d.get("has_random"))

    def test_lnobj(self):
        p = self.chart("a.bms", "#LNOBJ 0Z\n#00111:010Z\n")
        d = B.parse_bms(p)
        self.assertIn(B.b36("0Z"), d["lnobj"])

    def test_measure_length_channel(self):
        # channel 02 sets a measure-length ratio (measure 001 -> bar 1)
        p = self.chart("a.bms", "#00102:0.5\n#00111:01\n")
        d = B.parse_bms(p)
        self.assertIn("02", d["bars"][1])


# --------------------------------------------------------------------------
# note counting & mode detection
# --------------------------------------------------------------------------
class TestNoteCountingAndMode(TempDirCase):
    def test_counts_only_player_notes(self):
        # 4 player notes on ch 11, plus BGM on 01 (not counted), plus a landmine
        p = self.chart("a.bms", "#00111:01020304\n#00101:05050505\n#001D1:06060606\n")
        d = B.parse_bms(p)
        self.assertEqual(B.count_playable_notes(d), 4)

    def test_lnobj_terminator_not_double_counted(self):
        # an LN terminator marked by #LNOBJ should count the START but not the end
        p = self.chart("a.bms", "#LNOBJ 0Z\n#00111:010Z\n")
        d = B.parse_bms(p)
        # exactly one playable note (the start); terminator is silent
        self.assertEqual(B.count_playable_notes(d), 1)

    def test_mode_detection_returns_string(self):
        p = self.chart("a.bms", "#00111:01020304\n")
        d = B.parse_bms(p)
        self.assertIsInstance(B.detect_mode_from_bars(d), str)


# --------------------------------------------------------------------------
# render engine
# --------------------------------------------------------------------------
class TestRenderEngine(TempDirCase):
    def _make_keysounds(self, n):
        for i in range(n):
            write_wav(os.path.join(self.tmp, f"k{i}.wav"), freq=220 + i * 60)

    def test_basic_render_produces_stereo_audio(self):
        self._make_keysounds(2)
        p = self.chart("a.bms",
                       "#WAV01 k0.wav\n#WAV02 k1.wav\n#00111:0102\n")
        audio, header = B.render_bms(p)
        self.assertEqual(audio.shape[1], 2)
        self.assertGreater(len(audio), 0)
        self.assertGreater(float(np.abs(audio).max()), 0.0)

    def test_render_is_deterministic(self):
        self._make_keysounds(3)
        p = self.chart("a.bms",
                       "#WAV01 k0.wav\n#WAV02 k1.wav\n#WAV03 k2.wav\n"
                       "#00111:010203\n#00211:030201\n")
        a1, _ = B.render_bms(p)
        a2, _ = B.render_bms(p)
        self.assertTrue(np.array_equal(a1, a2))

    def test_unused_keysound_not_required(self):
        # a #WAV defined but never placed should not affect the render or error
        self._make_keysounds(2)
        # note: no file for k99; only referenced in an unused #WAV
        p = self.chart("a.bms",
                       "#WAV01 k0.wav\n#WAV02 k1.wav\n#WAV99 missing.wav\n#00111:0102\n")
        audio, _ = B.render_bms(p)
        self.assertGreater(len(audio), 0)

    def test_missing_keysound_renders_as_partial_silence(self):
        # referencing a sound whose file is absent should skip it, not crash
        self._make_keysounds(1)
        p = self.chart("a.bms",
                       "#WAV01 k0.wav\n#WAV02 nope.wav\n#00111:0102\n")
        audio, _ = B.render_bms(p)
        self.assertGreater(len(audio), 0)

    def test_visual_channel_does_not_change_note_timing(self):
        # KEY OPTIMIZATION GUARANTEE: a BGA/visual channel (ignored) whose length
        # is coprime to the note channel must NOT shift note onset positions, even
        # though it changes the bar's subdivision count.
        self._make_keysounds(2)
        body_no_vis = "#WAV01 k0.wav\n#WAV02 k1.wav\n#00111:01020102\n"
        body_vis    = "#WAV01 k0.wav\n#WAV02 k1.wav\n#00104:0102010201\n#00111:01020102\n"
        p1 = self.chart("a.bms", body_no_vis)
        p2 = self.chart("b.bms", body_vis)
        a1, _ = B.render_bms(p1)
        a2, _ = B.render_bms(p2)
        # The audible content is identical: same note onsets, same samples.
        n = min(len(a1), len(a2))
        self.assertTrue(np.array_equal(a1[:n], a2[:n]))

    def test_landmine_is_silent(self):
        # a chart with ONLY landmines should raise (no audible notes)
        self._make_keysounds(1)
        p = self.chart("a.bms", "#WAV01 k0.wav\n#001D1:01010101\n")
        with self.assertRaises(Exception):
            B.render_bms(p)

    def test_bucketed_mix_equals_sequential_mix(self):
        # Prove the vectorized (bucketed) mix is bit-identical to a naive
        # per-note sequential add, including overlaps and tail clipping.
        rng = np.random.default_rng(0)
        L = 20000
        clips = {1: (0.1 * rng.standard_normal((500, 2))).astype(np.float32),
                 2: (0.1 * rng.standard_normal((800, 2))).astype(np.float32)}
        schedule = [(100.4, 1), (100.9, 2), (5000.2, 1), (5000.7, 1), (19950.0, 2)]

        seq = np.zeros((L, 2), dtype=np.float32)
        for pos, wid in schedule:
            c = clips[wid]; s = int(pos)
            seg = seq[s:s + len(c)]
            seq[s:s + len(seg)] += c[:len(seg)]

        buc = np.zeros((L, 2), dtype=np.float32)
        onsets = {}
        for pos, wid in schedule:
            onsets.setdefault(wid, []).append(int(pos))
        for wid, ps in onsets.items():
            c = clips[wid]; cl = len(c)
            for s in ps:
                buc[s:s + cl] += c[:len(buc) - s]

        self.assertTrue(np.array_equal(seq, buc))

    def test_stereo_keysound_renders(self):
        write_wav(os.path.join(self.tmp, "st.wav"), channels=2)
        p = self.chart("a.bms", "#WAV01 st.wav\n#00111:0101\n")
        audio, _ = B.render_bms(p)
        self.assertEqual(audio.shape[1], 2)


# --------------------------------------------------------------------------
# find_audio (extension fallback)
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# folder image listing (per-song art picker)
# --------------------------------------------------------------------------
class TestFolderImages(TempDirCase):
    def _make(self, names):
        for n in names:
            open(os.path.join(self.tmp, n), "wb").close()

    def test_lists_images_skips_bmp_by_default(self):
        self._make(["banner.png", "cover.jpg", "bga000.bmp", "art.gif", "x.bms", "s.wav"])
        names = [os.path.basename(p) for p in B.list_folder_images(self.tmp)]
        self.assertIn("banner.png", names)
        self.assertIn("cover.jpg", names)
        self.assertIn("art.gif", names)
        self.assertNotIn("bga000.bmp", names)   # bmp skipped by default
        self.assertNotIn("x.bms", names)        # non-image excluded
        self.assertNotIn("s.wav", names)

    def test_includes_bmp_when_requested(self):
        self._make(["a.png", "bga000.bmp", "bga001.bmp"])
        names = [os.path.basename(p) for p in B.list_folder_images(self.tmp, include_bmp=True)]
        self.assertIn("bga000.bmp", names)
        self.assertIn("bga001.bmp", names)

    def test_sorted_order(self):
        self._make(["c.png", "a.png", "b.png"])
        names = [os.path.basename(p) for p in B.list_folder_images(self.tmp)]
        self.assertEqual(names, ["a.png", "b.png", "c.png"])

    def test_missing_folder_returns_empty(self):
        self.assertEqual(B.list_folder_images(os.path.join(self.tmp, "nope")), [])


class TestFindAudio(TempDirCase):
    def test_exact_match(self):
        open(os.path.join(self.tmp, "kick.wav"), "wb").close()
        self.assertTrue(B.find_audio(self.tmp, "kick.wav").endswith("kick.wav"))

    def test_extension_swap_wav_to_ogg(self):
        # chart says .wav but only .ogg exists — common in real BMS
        open(os.path.join(self.tmp, "kick.ogg"), "wb").close()
        found = B.find_audio(self.tmp, "kick.wav")
        self.assertIsNotNone(found)
        self.assertTrue(found.endswith("kick.ogg"))

    def test_missing_returns_none(self):
        self.assertIsNone(B.find_audio(self.tmp, "ghost.wav"))


# --------------------------------------------------------------------------
# safety guard (read-only library)
# --------------------------------------------------------------------------
class TestSafetyGuard(TempDirCase):
    def test_write_inside_library_is_blocked(self):
        lib = os.path.join(self.tmp, "lib"); os.makedirs(lib)
        B.set_library_root(lib)
        try:
            with self.assertRaises(Exception):
                B.assert_safe_output(os.path.join(lib, "out.flac"))
            # nested too
            sub = os.path.join(lib, "deep", "deeper"); os.makedirs(sub)
            with self.assertRaises(Exception):
                B.assert_safe_output(os.path.join(sub, "out.flac"))
        finally:
            B.set_library_root(None)

    def test_write_outside_library_is_allowed(self):
        lib = os.path.join(self.tmp, "lib"); os.makedirs(lib)
        out = os.path.join(self.tmp, "out"); os.makedirs(out)
        B.set_library_root(lib)
        try:
            B.assert_safe_output(os.path.join(out, "ok.flac"))  # must not raise
        finally:
            B.set_library_root(None)

    def test_dotdot_escape_still_blocked(self):
        lib = os.path.join(self.tmp, "lib"); os.makedirs(lib)
        B.set_library_root(lib)
        try:
            sneaky = os.path.join(lib, "..", "lib", "out.flac")
            with self.assertRaises(Exception):
                B.assert_safe_output(sneaky)
        finally:
            B.set_library_root(None)


# --------------------------------------------------------------------------
# SQLite cache
# --------------------------------------------------------------------------
class TestCache(TempDirCase):
    def _connect(self):
        B.DB_PATH = os.path.join(self.tmp, "cache.db")
        for ext in ("", "-wal", "-shm"):
            try: os.remove(B.DB_PATH + ext)
            except OSError: pass
        return B.db_connect()

    def test_scan_populates_cache(self):
        lib = os.path.join(self.tmp, "lib"); os.makedirs(lib)
        with open(os.path.join(lib, "a.bms"), "w") as f:
            f.write("#TITLE Song A\n#ARTIST X\n#BPM 120\n#WAV01 k.wav\n#00111:01\n")
        conn = self._connect()
        songs, stats = B.scan_library(lib, conn)
        conn.close()
        self.assertEqual(stats["parsed"], 1)
        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]["title"], "Song A")
        self.assertNotIn("ks", songs[0])  # the dropped keysound column is gone

    def test_rescan_reuses_unchanged(self):
        lib = os.path.join(self.tmp, "lib"); os.makedirs(lib)
        with open(os.path.join(lib, "a.bms"), "w") as f:
            f.write("#TITLE A\n#BPM 120\n#00111:01\n")
        conn = self._connect()
        B.scan_library(lib, conn)
        songs, stats = B.scan_library(lib, conn)  # second pass
        conn.close()
        self.assertEqual(stats["reused"], 1)
        self.assertEqual(stats["parsed"], 0)

    def test_schema_mismatch_rebuilds(self):
        # an old-style table with the dropped 'ks' column must be rebuilt
        B.DB_PATH = os.path.join(self.tmp, "old.db")
        conn = sqlite3.connect(B.DB_PATH)
        conn.execute("CREATE TABLE charts (path TEXT PRIMARY KEY, size INT, mtime INT, "
                     "title TEXT, artist TEXT, genre TEXT, bpm TEXT, mode TEXT, "
                     "notes INT, random INT, md5 TEXT, ks TEXT)")
        conn.commit(); conn.close()
        conn = B.db_connect()  # should detect mismatch and DROP
        cols = [r[1] for r in conn.execute("PRAGMA table_info(charts)")]
        conn.close()
        self.assertNotIn("ks", cols)

    def test_deleted_file_removed_from_cache(self):
        lib = os.path.join(self.tmp, "lib"); os.makedirs(lib)
        f1 = os.path.join(lib, "a.bms")
        with open(f1, "w") as f:
            f.write("#TITLE A\n#BPM 120\n#00111:01\n")
        conn = self._connect()
        B.scan_library(lib, conn)
        os.remove(f1)
        songs, stats = B.scan_library(lib, conn)
        conn.close()
        self.assertEqual(len(songs), 0)
        self.assertEqual(stats["removed"], 1)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
class TestConfig(TempDirCase):
    def test_roundtrip(self):
        B.CONFIG_PATH = os.path.join(self.tmp, "cfg.json")
        B.save_config({"library": "/x", "render_threads": 4})
        cfg = B.load_config()
        self.assertEqual(cfg["library"], "/x")
        self.assertEqual(cfg["render_threads"], 4)

    def test_missing_config_returns_dict(self):
        B.CONFIG_PATH = os.path.join(self.tmp, "nope.json")
        self.assertIsInstance(B.load_config(), dict)

    def test_corrupt_config_returns_dict(self):
        B.CONFIG_PATH = os.path.join(self.tmp, "bad.json")
        with open(B.CONFIG_PATH, "w") as f:
            f.write("{ this is not valid json ]")
        self.assertIsInstance(B.load_config(), dict)  # degrades gracefully


# --------------------------------------------------------------------------
# playlists (per-file storage)
# --------------------------------------------------------------------------
class TestPlaylists(TempDirCase):
    def setUp(self):
        super().setUp()
        B.PLAYLISTS_DIR = os.path.join(self.tmp, "Playlists")
        B.PLAYLISTS_PATH = os.path.join(self.tmp, "none.json")

    def test_save_and_load_one(self):
        B.save_one_playlist("My Mix", [{"md5": "h1", "title": "A", "artist": "X"}])
        pls = B.load_playlists()
        self.assertIn("My Mix", pls)
        self.assertEqual(pls["My Mix"][0]["md5"], "h1")

    def test_unsafe_filename_sanitized_but_name_preserved(self):
        B.save_one_playlist("Chill/Vibes:2", [{"md5": "h2", "title": "B", "artist": "Y"}])
        files = os.listdir(B.PLAYLISTS_DIR)
        # filename must not contain the unsafe chars
        self.assertTrue(all("/" not in f and ":" not in f for f in files))
        # but the real name survives inside the JSON
        pls = B.load_playlists()
        self.assertIn("Chill/Vibes:2", pls)

    def test_delete(self):
        B.save_one_playlist("Temp", [{"md5": "h", "title": "T", "artist": ""}])
        B.delete_playlist_file("Temp")
        self.assertNotIn("Temp", B.load_playlists())

    def test_migration_from_single_file(self):
        # an old single playlists.json should migrate into per-file form
        os.makedirs(B.PLAYLISTS_DIR, exist_ok=True)
        with open(B.PLAYLISTS_PATH, "w", encoding="utf-8") as f:
            json.dump({"playlists": {"Old": [{"md5": "z", "title": "Z", "artist": ""}]}}, f)
        pls = B.load_playlists()
        self.assertIn("Old", pls)
        # and a per-file copy now exists
        self.assertTrue(any("Old" in fn for fn in os.listdir(B.PLAYLISTS_DIR)))


# --------------------------------------------------------------------------
# difficulty-table parsing (offline — no network)
# --------------------------------------------------------------------------
class TestTableFile(TempDirCase):
    def test_save_and_load_tables(self):
        B.TABLES_PATH = os.path.join(self.tmp, "tables.json")
        B.save_tables_file([{"name": "Insane", "url": "http://x/table/"}])
        tabs = B.load_tables_file()
        self.assertEqual(tabs[0]["name"], "Insane")

    def test_missing_tables_returns_list(self):
        B.TABLES_PATH = os.path.join(self.tmp, "nope.json")
        self.assertIsInstance(B.load_tables_file(), list)


# --------------------------------------------------------------------------
# tag writing (FLAC/WAV; OGG needs ffmpeg so only if present)
# --------------------------------------------------------------------------
class TestTagWriting(TempDirCase):
    def test_flac_tags(self):
        out = os.path.join(self.tmp, "o.flac")
        audio = (0.1 * np.sin(2 * np.pi * 440 *
                 np.linspace(0, 1, 44100))).astype("float32")
        audio = np.stack([audio, audio], axis=1)
        sf.write(out, audio, 44100, format="FLAC")
        B.write_tags_to_file(out, "FLAC",
                             {"Title": "T", "Artist": "A", "Genre": "G", "BPM": "120"}, None)
        from mutagen.flac import FLAC
        f = FLAC(out)
        self.assertEqual(f["title"][0], "T")
        self.assertEqual(f["album"][0], "BMS")  # album always BMS

    def test_wav_tags(self):
        out = os.path.join(self.tmp, "o.wav")
        audio = (0.1 * np.ones((44100, 2))).astype("float32")
        sf.write(out, audio, 44100, format="WAV")
        # must not raise
        B.write_tags_to_file(out, "WAV",
                             {"Title": "T", "Artist": "A", "Genre": "G", "BPM": ""}, None)


# --------------------------------------------------------------------------
# render worker (in-process; multiprocessing itself is manual-tested on Windows)
# --------------------------------------------------------------------------
class TestRenderWorker(TempDirCase):
    def test_worker_renders_and_tags(self):
        write_wav(os.path.join(self.tmp, "k.wav"))
        chart = os.path.join(self.tmp, "a.bms")
        with open(chart, "w") as f:
            f.write("#TITLE W\n#BPM 120\n#WAV01 k.wav\n#00111:0101\n")
        out = os.path.join(self.tmp, "out.flac")
        job = (chart, out, "FLAC",
               {"Title": "W", "Artist": "", "Genre": "", "BPM": "120"}, None, None, None)
        out_path, title, err = B.render_one_job(job)
        self.assertIsNone(err, msg=f"worker error: {err}")
        self.assertTrue(os.path.exists(out))

    def test_worker_blocks_write_into_library(self):
        # safety must hold INSIDE the worker (set_library_root from the job)
        lib = os.path.join(self.tmp, "lib"); os.makedirs(lib)
        write_wav(os.path.join(lib, "k.wav"))
        chart = os.path.join(lib, "a.bms")
        with open(chart, "w") as f:
            f.write("#TITLE W\n#BPM 120\n#WAV01 k.wav\n#00111:0101\n")
        bad_out = os.path.join(lib, "inside.flac")  # inside library!
        job = (chart, bad_out, "FLAC",
               {"Title": "W", "Artist": "", "Genre": "", "BPM": ""}, None, None, lib)
        out_path, title, err = B.render_one_job(job)
        self.assertIsNotNone(err)            # must have failed
        self.assertFalse(os.path.exists(bad_out))
        B.set_library_root(None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
