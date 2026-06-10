#!/usr/bin/env python3
"""
BMS Renderer -- a desktop app for Windows.

Workflow:
  1. Pick your BMS library root (your large folder of song folders).
  2. The app scans for .bms / .bme / .bml / .pms files and lists them with
     title/artist/BPM/notes, caching results for fast relaunch.
  3. Browse the Library, build a Queue, organize charts against online difficulty
     tables, or make portable playlists. Double-click anything to play it.
  4. Edit a queued song's tags, then "Render All in Queue" to write tagged
     FLAC/WAV (or OGG when ffmpeg is present) to your output folder.

The library is treated as strictly read-only; nothing is ever written inside it.
You do not need to edit this file. If something breaks, copy the error text
from the log box at the bottom and send it back for a fix.

All pure logic (parser, render engine, cache, playlists, config, tables) lives in
bms_core.py; this file is the Tkinter GUI on top of it.
"""

import os, sys, threading, traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Everything non-GUI comes from the shared core module (single source of truth,
# also used by the test suite).
from bms_core import *
import bms_core
# Bring the names the GUI uses directly into module scope (import * skips some
# because of leading underscores or just to be explicit about dependencies).
from bms_core import (
    APP_VERSION, CHANGELOG, SR, BMS_EXTS, AUDIO_EXTS,
    program_dir, ffmpeg_path,
    DB_PATH, CACHE_PATH, CONFIG_PATH, TABLES_PATH, PLAYLISTS_PATH, PLAYLISTS_DIR,
    load_config, save_config,
    set_library_root, assert_safe_output,
    db_connect, scan_library, file_md5,
    parse_bms, count_playable_notes, detect_mode_from_bars,
    render_bms, render_one_job, write_tags_to_file, process_cover, list_folder_images,
    pick_playable_chart, pick_discovery_art,
    load_tables_file, save_tables_file, fetch_table,
    load_playlists, save_one_playlist, delete_playlist_file,
    _migrate_old_cache, _PIL_OK, _num,
    Player, _SD_OK,
)

# ---------------------------------------------------------------- the app

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"BMS Renderer  v{APP_VERSION}")
        self._set_window_icon()
        saved_geo = load_config().get("window_geometry")
        self.geometry(saved_geo if saved_geo else "1600x1000")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_fonts()
        self.songs = []          # all charts
        self.filtered = []       # library subset currently shown
        self.queue = []          # list of dicts: {path, tags:{...}, title}
        self.selected_kind = None  # 'library' or 'queue' (which list drives Tags)
        self.selected_index = None
        self.art_path = None       # album art is per-session only; never persisted
        self._search_job = None
        self._reverse_lookup = False
        # playback state
        self.player = Player(samplerate=SR, on_finished=None) if _SD_OK else None
        self._playing_ctx = None   # ('library'|'queue', index, path) of current song
        self._render_cache = {}    # path -> rendered buffer (instant replay/seek)
        self._render_cache_order = []   # LRU order of paths in _render_cache
        self._render_cache_max = 6      # current + neighbors + a little slack
        self._prefetching = set()  # paths currently being pre-rendered in the background
        self._seeking = False      # True while user drags the timeline
        self._build()
        self._load_song_art_for(None)   # picker starts disabled until a queued song is selected
        self.after(200, self.autostart)

    def _set_window_icon(self):
        """Set the window icon from the PNG embedded in bms_core (base64) — no
        external file required. Best-effort: any failure is ignored so the app
        always launches."""
        try:
            self._icon_img = tk.PhotoImage(data=bms_core._ICON_PNG_B64)
            self.iconphoto(True, self._icon_img)
        except tk.TclError:
            pass

    def _setup_fonts(self):
        """Use a Japanese-capable font app-wide so CJK titles render cleanly.
        Picks the first installed family from the preference list."""
        import tkinter.font as tkfont
        prefs = ["Meiryo UI", "Yu Gothic UI", "Meiryo", "MS Gothic", "Segoe UI"]
        try:
            available = set(tkfont.families(self))
        except tk.TclError:
            return
        family = next((f for f in prefs if f in available), None)
        if family is None:
            return  # keep Tk default if none found (e.g. non-Windows)
        size = 10
        for named in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                      "TkHeadingFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(named).configure(family=family, size=size)
            except tk.TclError:
                pass
        try:
            style = ttk.Style(self)
            style.configure(".", font=(family, size))
            style.configure("Treeview", font=(family, size))
            style.configure("Treeview.Heading", font=(family, size))
        except tk.TclError:
            pass
        self._ui_font = (family, size)

    def _path_entry(self, parent, placeholder):
        """A read-only, selectable, full-width entry for showing a folder path."""
        e = ttk.Entry(parent)
        e.insert(0, placeholder)
        e.config(state="readonly")
        e.pack(side="left", fill="x", expand=True, padx=8)
        return e

    def _set_path(self, entry, text):
        entry.config(state="normal")
        entry.delete(0, "end")
        entry.insert(0, text)
        entry.config(state="readonly")

    # ------------------------------------------------------------------ build
    def _build(self):
        top = ttk.Frame(self); top.pack(fill="x", padx=8, pady=(6,0))
        ttk.Label(top, text=f"BMS Renderer v{APP_VERSION}").pack(side="right", anchor="n")

        librow = ttk.Frame(top); librow.pack(fill="x")
        ttk.Button(librow, text="Choose library folder…", width=20,
                   command=self.choose_lib).pack(side="left")
        self.lib_lbl = self._path_entry(librow, "(no library)")

        outrow = ttk.Frame(top); outrow.pack(fill="x", pady=(4,0))
        ttk.Button(outrow, text="Choose output folder…", width=20,
                   command=self.choose_output).pack(side="left")
        self.out_lbl = self._path_entry(outrow, "(no output folder)")

        mid = ttk.Frame(self)   # packed LAST (after bottom widgets) so the
                                # log + transport bar always keep their space
        self._mid = mid

        # ---- right panel FIRST so pack reserves its fixed width before the
        #      notebook claims the rest (prevents the tree from squishing it) ----
        right = ttk.Frame(mid, width=340); right.pack(side="right", fill="y", padx=(8,0))
        right.pack_propagate(False)
        self._right = right

        # ---- left: notebook with the tabs ----
        self.nb = ttk.Notebook(mid); self.nb.pack(side="left", fill="both", expand=True)

        # Library tab
        lib_tab = ttk.Frame(self.nb); self.nb.add(lib_tab, text="Library")
        self.lib_tab = lib_tab
        fbar = ttk.Frame(lib_tab); fbar.pack(fill="x", pady=(4,2))
        self.songs_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(fbar, text="Songs only", variable=self.songs_only,
                        command=self.apply_filter).pack(side="left", padx=(0,10))
        ttk.Label(fbar, text="Search:").pack(side="left")
        self.search = ttk.Entry(fbar)
        self.search.pack(side="left", fill="x", expand=True, padx=(4,12))
        self.search.bind("<KeyRelease>", self.on_search_key)
        ttk.Label(fbar, text="Type:").pack(side="left")
        self.mode_filter = ttk.Combobox(fbar, state="readonly", width=10,
            values=["All", "7K SP", "5K SP", "DP", "PMS"])
        self.mode_filter.set("7K SP")
        self.mode_filter.pack(side="left")
        self.mode_filter.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())

        lf = ttk.Frame(lib_tab); lf.pack(fill="both", expand=True)
        cols = ("title", "artist", "bpm", "mode", "notes")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings")
        widths = {"title":300, "artist":190, "bpm":55, "mode":65, "notes":60}
        for c in cols:
            self.tree.heading(c, text=c.title(),
                              command=lambda col=c: self.sort_library(col))
            anchor = "w"
            self.tree.column(c, width=widths[c], anchor=anchor)
        self._sort_col = None; self._sort_desc = False
        # rows for charts containing #RANDOM are shown in red
        self.tree.tag_configure("random", foreground="#c0392b")
        vsb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_library_select)
        self.tree.bind("<Double-Button-1>", self.on_library_activate)
        self.tree.bind("<Button-3>", self._lib_rightclick)
        self.tree.bind("<<TreeviewOpen>>", self._lib_group_open)
        libbtns = ttk.Frame(lib_tab); libbtns.pack(fill="x", pady=4)
        ttk.Button(libbtns, text="Add selected to Queue →",
                   command=self.add_to_queue).pack(side="left", fill="x", expand=True)
        self.exit_lookup_btn = ttk.Button(libbtns, text="Exit lookup",
                                          command=self.exit_lookup)
        # not packed until 'show all charts' is active

        # Queue tab
        q_tab = ttk.Frame(self.nb); self.nb.add(q_tab, text="Queue (0)")
        self.q_tab = q_tab
        qf = ttk.Frame(q_tab); qf.pack(fill="both", expand=True, pady=(4,2))
        qcols = ("title", "artist", "mode", "notes")
        self.qtree = ttk.Treeview(qf, columns=qcols, show="headings")
        for c, w in zip(qcols, (320, 200, 65, 60)):
            self.qtree.heading(c, text=c.title())
            self.qtree.column(c, width=w, anchor=("e" if c == "notes" else "w"))
        qvsb = ttk.Scrollbar(qf, orient="vertical", command=self.qtree.yview)
        self.qtree.configure(yscrollcommand=qvsb.set)
        qvsb.pack(side="right", fill="y")
        self.qtree.pack(side="left", fill="both", expand=True)
        self.qtree.bind("<<TreeviewSelect>>", self.on_queue_select)
        self.qtree.bind("<Double-Button-1>", self.on_queue_activate)
        self.qtree.bind("<Button-3>", self._queue_rightclick)
        qbtns = ttk.Frame(q_tab); qbtns.pack(fill="x", pady=4)
        ttk.Button(qbtns, text="Remove from Queue", command=self.remove_from_queue).pack(side="left")
        ttk.Button(qbtns, text="Clear Queue", command=self.clear_queue).pack(side="left", padx=6)
        self.render_btn = ttk.Button(qbtns, text="▶ Render All in Queue", command=self.render_all)
        self.render_btn.pack(side="right")
        # render controls, packed left-to-right in reading order, before the button
        ctrls = ttk.Frame(qbtns); ctrls.pack(side="right", padx=(0,8))
        cpu = os.cpu_count() or 4
        ttk.Label(ctrls, text="Threads:").pack(side="left", padx=(0,4))
        self.threads_var = tk.StringVar(value=str(self._render_worker_count()))
        self.threads_spin = ttk.Spinbox(ctrls, from_=1, to=cpu*2, width=4,
                                        textvariable=self.threads_var,
                                        command=self._on_threads_change)
        self.threads_spin.pack(side="left", padx=(0,12))
        ttk.Label(ctrls, text="Format:").pack(side="left", padx=(0,4))
        _fmts = ["FLAC", "WAV"] + (["OGG"] if ffmpeg_path() else [])
        self.fmt_pick = ttk.Combobox(ctrls, state="readonly", width=7, values=_fmts)
        self.fmt_pick.set("FLAC")
        self.fmt_pick.pack(side="left")

        # Tables tab
        t_tab = ttk.Frame(self.nb); self.nb.insert(1, t_tab, text="Tables")
        self.t_tab = t_tab
        tbar = ttk.Frame(t_tab); tbar.pack(fill="x", pady=(4,2))
        ttk.Label(tbar, text="Table:").pack(side="left")
        self.table_pick = ttk.Combobox(tbar, state="readonly", width=30, values=[])
        self.table_pick.pack(side="left", padx=4)
        self.table_pick.bind("<<ComboboxSelected>>", lambda e: self.on_table_pick())
        ttk.Button(tbar, text="Add table by URL…", command=self.add_table_by_url).pack(side="left", padx=6)
        ttk.Button(tbar, text="↻ Refresh", command=self.refresh_current_table).pack(side="left")
        ttk.Button(tbar, text="Add all owned to Queue",
                   command=self.add_table_to_queue).pack(side="left", padx=(12,0))
        self.table_status = ttk.Label(tbar, text="", foreground="#666")
        self.table_status.pack(side="left", padx=8)
        tf = ttk.Frame(t_tab); tf.pack(fill="both", expand=True)
        tcols = ("title", "artist", "owned", "notes")
        self.ttree = ttk.Treeview(tf, columns=tcols, show="tree headings")
        self.ttree.heading("#0", text="Level")
        self.ttree.column("#0", width=160, stretch=False)
        for c, w in zip(tcols, (320, 170, 70, 60)):
            self.ttree.heading(c, text=c.title(),
                               command=lambda col=c: self.sort_table(col))
            self.ttree.column(c, width=w, anchor=("e" if c in ("notes",) else "w"))
        tvsb = ttk.Scrollbar(tf, orient="vertical", command=self.ttree.yview)
        self.ttree.configure(yscrollcommand=tvsb.set)
        tvsb.pack(side="right", fill="y")
        self.ttree.pack(side="left", fill="both", expand=True)
        self.ttree.tag_configure("missing", foreground="#999")  # grey = not owned
        self.ttree.bind("<<TreeviewSelect>>", self.on_table_select)
        self.ttree.bind("<Double-Button-1>", self.on_table_activate)
        # right-click menu
        self.ttree.bind("<Button-3>", self._table_rightclick)
        self._tmenu = tk.Menu(self, tearoff=0)
        self._tmenu.add_command(label="Play", command=self._tbl_play)
        self._tmenu.add_command(label="Add to Queue", command=self._tbl_add_queue)
        self._tmenu.add_command(label="Show all charts for this song", command=self._tbl_show_all)
        self._tbl_plmenu = tk.Menu(self._tmenu, tearoff=0)
        self._tmenu.add_cascade(label="Add to playlist", menu=self._tbl_plmenu)
        # menu shown when right-clicking a level header row
        self._levelmenu = tk.Menu(self, tearoff=0)
        self._levelmenu.add_command(label="Add all in this level to Queue",
                                    command=self.add_level_to_queue)
        self._ctx_level = None
        self._table_cache = {}     # name -> fetched table (lazy, cached per session)
        self._table_defs = []

        # Custom Playlists tab (between Tables and Queue)
        p_tab = ttk.Frame(self.nb); self.nb.insert(2, p_tab, text="Custom Playlists")
        self.p_tab = p_tab
        pbar = ttk.Frame(p_tab); pbar.pack(fill="x", pady=(4,2))
        ttk.Label(pbar, text="Playlist:").pack(side="left")
        self.pl_pick = ttk.Combobox(pbar, state="readonly", width=26, values=[])
        self.pl_pick.pack(side="left", padx=4)
        self.pl_pick.bind("<<ComboboxSelected>>", lambda e: self.show_playlist())
        ttk.Button(pbar, text="New…", command=self.new_playlist).pack(side="left", padx=(6,0))
        ttk.Button(pbar, text="Rename…", command=self.rename_playlist).pack(side="left", padx=4)
        ttk.Button(pbar, text="Delete", command=self.delete_playlist).pack(side="left")
        self.pl_status = ttk.Label(pbar, text="", foreground="#666")
        self.pl_status.pack(side="left", padx=8)
        pf = ttk.Frame(p_tab); pf.pack(fill="both", expand=True)
        pcols = ("title", "artist", "mode", "notes")
        self.pltree = ttk.Treeview(pf, columns=pcols, show="headings")
        for c, w in zip(pcols, (320, 200, 65, 60)):
            self.pltree.heading(c, text=c.title(),
                                command=lambda col=c: self.sort_playlist(col))
            self.pltree.column(c, width=w, anchor=("e" if c == "notes" else "w"))
        plvsb = ttk.Scrollbar(pf, orient="vertical", command=self.pltree.yview)
        self.pltree.configure(yscrollcommand=plvsb.set)
        plvsb.pack(side="right", fill="y")
        self.pltree.pack(side="left", fill="both", expand=True)
        self.pltree.tag_configure("missing", foreground="#999")  # grey if file is gone
        self.pltree.bind("<<TreeviewSelect>>", self.on_playlist_select)
        self.pltree.bind("<Double-Button-1>", self.on_playlist_activate)
        self.pltree.bind("<Button-3>", self._pl_rightclick)
        plbtns = ttk.Frame(p_tab); plbtns.pack(fill="x", pady=4)
        ttk.Button(plbtns, text="Remove from playlist", command=self.remove_from_playlist).pack(side="left")
        self.pl_render_btn = ttk.Button(plbtns, text="▶ Render playlist", command=self.render_playlist)
        self.pl_render_btn.pack(side="right")
        # right-click menu for playlist items
        self._plmenu = tk.Menu(self, tearoff=0)
        self._plmenu.add_command(label="Play", command=self._pl_play)
        self._plmenu.add_command(label="Add to Queue", command=self._pl_add_queue)
        self._pl_plmenu = tk.Menu(self._plmenu, tearoff=0)
        self._plmenu.add_cascade(label="Add to playlist", menu=self._pl_plmenu)
        self._plmenu.add_command(label="Remove from playlist", command=self.remove_from_playlist)
        self._playlists = {}       # name -> [path, ...]
        self._pl_rows = []         # song dicts currently shown for the selected playlist

        # shared right-click menu for Library and Queue trees
        self._lqmenu = tk.Menu(self, tearoff=0)
        self._lqmenu.add_command(label="Play", command=self._ctx_play)
        self._lqmenu.add_command(label="Add to Queue", command=self._ctx_add_queue)
        self._lqmenu.add_command(label="Show all charts for this song", command=self._ctx_show_all)
        self._lq_plmenu = tk.Menu(self._lqmenu, tearoff=0)
        self._lqmenu.add_cascade(label="Add to playlist", menu=self._lq_plmenu)
        self._ctx_song = None      # song dict the context menu currently targets

        # ---- Discovery tab: a virtualized art grid (view recycling) ----
        d_tab = ttk.Frame(self.nb); self.nb.insert(1, d_tab, text="\u2726 Discovery \u2726")
        self.disc_tab = d_tab
        dbar = ttk.Frame(d_tab); dbar.pack(fill="x", pady=(4,2))
        self.disc_status = ttk.Label(dbar, text="scroll through your library -- "
                                                "double-click a tile to listen",
                                     foreground="#666")
        self.disc_status.pack(side="left", padx=10)
        dwrap = ttk.Frame(d_tab); dwrap.pack(fill="both", expand=True)
        self.disc_canvas = tk.Canvas(dwrap, highlightthickness=0, bg="#f0f0f0")
        self.disc_vsb = ttk.Scrollbar(dwrap, orient="vertical",
                                      command=self._disc_yview)
        self.disc_canvas.configure(yscrollcommand=self.disc_vsb.set)
        self.disc_vsb.pack(side="right", fill="y")
        self.disc_canvas.pack(side="left", fill="both", expand=True)
        self.disc_canvas.bind("<Configure>", self._disc_on_resize)
        self.disc_canvas.bind_all("<MouseWheel>", self._disc_mousewheel)
        # virtualization state
        self._disc_order = []      # full shuffled library, one (rep, charts) per song
        self._disc_pool = []       # reusable tile widgets (count ~ visible+buffer)
        self._disc_pool_map = {}   # pool slot -> song index currently shown there
        self._disc_thumbs = {}     # song index -> PhotoImage (LRU-capped cache)
        self._disc_thumb_order = []# insertion order for the LRU cache
        self._disc_pending = set() # song indices whose thumbnail is being decoded
        self._disc_cols = 1
        self._disc_scroll = 0      # current scroll offset in pixels
        self._disc_token = 0       # cancels stale thumbnail decodes
        self._disc_loaded = False  # first layout happens when the tab is first opened
        self._DISC_TILE_H = self._DISC_TILE + 56  # art(170+2 border) + title + artist
        self._DISC_ROW_H = self._DISC_TILE + 76   # row stride (gap below artist)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)


        # ---- right panel: Tags, BMS information, Album art (top to bottom) ----
        # 1) Tags (editable)
        tags_box = ttk.LabelFrame(right, text="Tags"); tags_box.pack(fill="x")
        self.fields = {}
        for key in ("Title", "Artist", "Album", "Genre", "BPM"):
            row = ttk.Frame(tags_box); row.pack(fill="x", padx=8, pady=3)
            ttk.Label(row, text=key, width=8).pack(side="left")
            e = ttk.Entry(row); e.pack(side="left", fill="x", expand=True)
            e.bind("<KeyRelease>", self.on_tag_edit)
            self.fields[key] = e
        self.tag_hint = ttk.Label(tags_box, text="Select a song to edit its tags.",
                                  wraplength=300, foreground="#666")
        self.tag_hint.pack(padx=8, pady=6, anchor="w")

        # 2) BMS information (read-only but selectable, from the chart header)
        info_box = ttk.LabelFrame(right, text="BMS information"); info_box.pack(fill="x", pady=(8,0))
        self.info_labels = {}
        info_keys = ["Title", "Artist", "Genre", "BPM", "Notes", "Play type", "File", "MD5"]
        for key in info_keys:
            row = ttk.Frame(info_box); row.pack(fill="x", padx=8, pady=1)
            ttk.Label(row, text=key + ":", width=9).pack(side="left", anchor="n")
            # read-only Entry so the value can be selected/copied (like the path fields)
            val = ttk.Entry(row)
            val.insert(0, "—")
            val.config(state="readonly")
            val.pack(side="left", fill="x", expand=True)
            self.info_labels[key] = val

        # 3) Per-song art picker — scroll through images in the selected QUEUED
        #    song's own folder; whatever is shown becomes that song's cover (used
        #    only when no global 'whole queue' art is set).
        pic = ttk.LabelFrame(right, text="Song folder art (this song)")
        pic.pack(fill="x", pady=(8,0))
        prow = ttk.Frame(pic); prow.pack(fill="x", padx=6, pady=(6,2))
        # Center group: [◀] [fixed preview] [▶] packed together and centered, so the
        # arrows sit a constant distance apart regardless of any text width elsewhere.
        center = ttk.Frame(prow); center.pack(anchor="center")
        self.song_art_prev = ttk.Button(center, text="◀", width=3)
        self.song_art_prev.pack(side="left")
        self._bind_art_hold(self.song_art_prev, -1)
        # fixed-size container holds the preview image so its width never changes
        art_hold = ttk.Frame(center, width=self._ART_BOX, height=self._ART_BOX)
        art_hold.pack(side="left", padx=8)
        art_hold.pack_propagate(False)   # don't let the child image resize this frame
        self.song_art_canvas = ttk.Label(art_hold, text="—", anchor="center",
                                         foreground="#666")
        self.song_art_canvas.pack(fill="both", expand=True)
        self.song_art_next = ttk.Button(center, text="▶", width=3)
        self.song_art_next.pack(side="left")
        self._bind_art_hold(self.song_art_next, 1)
        self._art_repeat_job = None   # pending auto-repeat timer, if any
        brow = ttk.Frame(pic); brow.pack(fill="x", padx=6, pady=(0,6))
        # fixed width + clipping so a long filename can't change the panel width
        self.song_art_status = ttk.Label(brow, text="", foreground="#666",
                                         width=24, anchor="w")
        self.song_art_status.pack(side="left")
        self.ignore_bmp = tk.BooleanVar(value=True)
        ttk.Checkbutton(brow, text="ignore .bmp", variable=self.ignore_bmp,
                        command=self._reload_song_art).pack(side="right")
        # type-in jump: enter an image number and press Enter to go straight to it
        self.song_art_jump = ttk.Entry(brow, width=5, justify="center")
        self.song_art_jump.pack(side="right", padx=(0,6))
        self.song_art_jump.bind("<Return>", lambda e: self._song_art_goto())
        ttk.Label(brow, text="Go to #:").pack(side="right")
        # per-song picker state
        self._song_art_files = []   # image paths in the current song's folder
        self._song_art_idx = 0      # which one is shown/selected
        self._song_art_thumb = None # keep a ref so Tk doesn't GC the preview

        # 4) Album art (one cover for the whole queue) — overrides per-song art
        art = ttk.LabelFrame(right, text="Album art (whole queue)"); art.pack(fill="x", pady=(8,4))
        btns = ttk.Frame(art); btns.pack(fill="x", padx=6, pady=(6,2))
        ttk.Button(btns, text="Choose…", command=self.choose_art).pack(side="left")
        ttk.Button(btns, text="Clear", command=self.clear_art).pack(side="left", padx=6)
        self.art_lbl = ttk.Label(art, text="(none — FLACs will have no cover)",
                                 wraplength=300, foreground="#666")
        self.art_lbl.pack(padx=6, pady=(0,6), anchor="w")

        # ---- transport bar (anchored to the very bottom) ----
        tp = ttk.Frame(self); tp.pack(side="bottom", fill="x", padx=8, pady=(0,8))
        self._rendering_play = False   # True while a play-render is in progress
        self.prev_btn = ttk.Button(tp, text="⏮", width=3, command=self.prev_track)
        self.prev_btn.pack(side="left")
        self.play_btn = ttk.Button(tp, text="▶", width=3, command=self.toggle_play)
        self.play_btn.pack(side="left", padx=(4,0))
        ttk.Button(tp, text="■", width=3, command=self.stop_play).pack(side="left", padx=(4,0))
        self.next_btn = ttk.Button(tp, text="⏭", width=3, command=self.next_track)
        self.next_btn.pack(side="left", padx=(4,12))
        self.time_lbl = ttk.Label(tp, text="0:00 / 0:00", width=14)
        self.time_lbl.pack(side="left")
        self.seek = ttk.Scale(tp, from_=0, to=1000, orient="horizontal")
        self.seek.pack(side="left", fill="x", expand=True, padx=8)
        # volume control (bottom-right); items pack right-to-left
        self.vol = ttk.Scale(tp, from_=0, to=100, orient="horizontal", length=110,
                             command=self._on_volume)
        self.vol.set(100)
        self.vol.pack(side="right", padx=(0,4))
        # shuffle / loop toggles (left of volume). Checkbuttons read as on/off.
        self.loop_on = tk.BooleanVar(value=False)
        self.shuffle_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(tp, text="Loop", variable=self.loop_on).pack(side="right", padx=(0,8))
        ttk.Checkbutton(tp, text="Shuffle", variable=self.shuffle_on).pack(side="right", padx=(8,4))
        self.now_lbl = ttk.Label(tp, text="", foreground="#2a7", width=22)
        self.now_lbl.pack(side="right", padx=(0,12))
        # seek interactions: drag freely (no sound), seek on release
        self.seek.bind("<ButtonPress-1>", self._seek_grab)
        self.seek.bind("<ButtonRelease-1>", self._seek_release)
        if not _SD_OK:
            self.play_btn.config(state="disabled")
            self.prev_btn.config(state="disabled")
            self.next_btn.config(state="disabled")
            self.now_lbl.config(text="(playback unavailable)", foreground="#999")

        # ---- log (anchored to the bottom, just above the transport bar) ----
        self.log_box = tk.Text(self, height=6)
        self.log_box.pack(side="bottom", fill="x", padx=8, pady=(6,2))

        # Pack the main area LAST so it fills whatever's left above the log/transport.
        self._mid.pack(fill="both", expand=True, padx=8, pady=(4,0))
        # Minimum size chosen so the right panel (Tags + BMS information + Album art)
        # always fits without clipping.
        self.minsize(980, 720)

        self.log(f"BMS Renderer v{APP_VERSION} ready.")
        self._now_full = ""
        if _SD_OK:
            self.after(100, self._tick)        # playback UI updater
            self.after(350, self._scroll_now_label)  # now-playing marquee
        # Mitigation for a known Tk-on-Wayland/Linux issue where widgets don't
        # repaint until the cursor moves over them. Periodically nudging Tk to
        # process idle redraws helps on affected setups; it's a no-op cost elsewhere.
        if sys.platform.startswith("linux"):
            self.after(200, self._wayland_redraw_nudge)

    def _wayland_redraw_nudge(self):
        try:
            self.update_idletasks()
        except tk.TclError:
            return
        self.after(200, self._wayland_redraw_nudge)

    def log(self, msg):
        # Thread-safe: if called from a worker thread, marshal to the main thread.
        if threading.current_thread() is not threading.main_thread():
            self.after(0, self.log, msg)
            return
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        # NOTE: no update_idletasks() here — forcing synchronous redraws was a
        # contributor to the freezing. Tk repaints on its own between events.

    # ------------------------------------------------------------- start / cfg
    def autostart(self):
        cfg = load_config()
        out = cfg.get("output")
        if out:
            self._set_path(self.out_lbl, out)
        if not _PIL_OK:
            self.art_lbl.config(text="(album art unavailable — Pillow not installed)")
        self.load_playlists_into_ui()
        folder = cfg.get("library")
        if folder and os.path.isdir(folder):
            self.log(f"Reopening last library: {folder}")
            self.begin_scan(folder)
        else:
            self.log("Choose your library folder to begin.")

    def _set_art_label(self, path):
        self.art_lbl.config(text=f"Art: {os.path.basename(path)}")

    def choose_art(self):
        if not _PIL_OK:
            messagebox.showerror("Album art unavailable",
                "Pillow isn't installed, so the app can't process images.\n"
                "Install it once with:  pip install Pillow")
            return
        path = filedialog.askopenfilename(
            title="Select album art (JPEG or PNG)",
            filetypes=[("Images", "*.jpg *.jpeg *.png"), ("All files", "*.*")])
        if not path:
            return
        # validate + report what it'll become; store in memory only (not config)
        try:
            data, size, q = process_cover(path)
            over = " (still over 500KB — will embed anyway)" if len(data) > 500_000 else ""
            self.art_path = path
            self._set_art_label(path)
            self.log(f"Album art set: {os.path.basename(path)} → "
                     f"{size[0]}x{size[1]} JPEG, {len(data)//1024} KB{over}  "
                     f"(resets when you close the app)")
        except Exception as e:
            messagebox.showerror("Couldn't read that image",
                f"That file couldn't be processed as album art:\n{e}")

    def clear_art(self):
        self.art_path = None
        self.art_lbl.config(text="(none — FLACs will have no cover)")
        self.log("Album art cleared.")

    # ---------------- per-song folder art picker ----------------
    def _load_song_art_for(self, song_path, queue_item=None):
        """Populate the picker preview from a song's folder. Works for ANY selected
        song (Library/Tables/Queue/playlist) so you can always see the folder art.
        `queue_item` is the queue dict when the song is queued — only then can the
        arrows change the saved cover; elsewhere it's a read-only preview.
        Pass song_path=None to clear the picker entirely (nothing selected)."""
        self._song_art_qitem = queue_item     # None ⇒ read-only preview
        if not song_path:
            self._song_art_files = []
            self._song_art_idx = 0
            self._song_art_folder = None
            self._set_song_art_enabled(False)
            self._render_song_art()
            return
        folder = os.path.dirname(song_path)
        self._song_art_folder = folder
        self._song_art_files = list_folder_images(folder, include_bmp=not self.ignore_bmp.get())
        # pick the index: a queued song's saved cover, else the first image
        chosen = queue_item.get("art") if queue_item else None
        if chosen and chosen in self._song_art_files:
            self._song_art_idx = self._song_art_files.index(chosen)
        else:
            self._song_art_idx = 0
            # for a queued song, auto-select the first image as its cover
            if queue_item is not None and self._song_art_files:
                queue_item["art"] = self._song_art_files[0]
        # arrows work on any tab when there's more than one image to scroll; for a
        # queued song stepping also saves the cover, elsewhere it just previews
        self._set_song_art_enabled(_PIL_OK and len(self._song_art_files) > 1)
        self._render_song_art()

    def _reload_song_art(self):
        """Re-scan the current folder (e.g. after toggling ignore .bmp). Works for
        both a queued song and a read-only preview."""
        folder = getattr(self, "_song_art_folder", None)
        if not folder:
            return
        qi = self._song_art_qitem
        if qi is not None:
            qi.pop("art", None)        # folder set changed; reset the saved pick
        # re-scan by pointing at any file in the folder (dirname recovers it)
        self._load_song_art_for(os.path.join(folder, "_"), qi)

    def _bind_art_hold(self, button, direction):
        """Make an arrow button auto-repeat while held: one step on press, then a
        short pause, then accelerating repeats until release."""
        button.bind("<ButtonPress-1>", lambda e: self._art_hold_start(direction))
        button.bind("<ButtonRelease-1>", lambda e: self._art_hold_stop())
        # leaving the button (mouse slips off) also stops the repeat
        button.bind("<Leave>", lambda e: self._art_hold_stop())

    def _art_hold_start(self, direction):
        if str(self.song_art_next["state"]) == "disabled":
            return                      # arrows disabled (≤1 image) → ignore
        self._art_hold_stop()           # clear any prior timer
        self._song_art_step(direction)  # immediate first step on press
        # first repeat after a deliberate pause, so a normal click steps just once
        self._art_repeat_job = self.after(400, lambda: self._art_hold_tick(direction, 120))

    def _art_hold_tick(self, direction, interval):
        self._song_art_step(direction)
        # accelerate toward a floor so a long hold speeds through the folder
        nxt = max(35, interval - 10)
        self._art_repeat_job = self.after(interval,
                                          lambda: self._art_hold_tick(direction, nxt))

    def _art_hold_stop(self):
        if self._art_repeat_job is not None:
            try:
                self.after_cancel(self._art_repeat_job)
            except Exception:
                pass
            self._art_repeat_job = None

    def _song_art_step(self, delta):
        if not self._song_art_files:
            return
        self._song_art_idx = (self._song_art_idx + delta) % len(self._song_art_files)
        qi = self._song_art_qitem
        if qi is not None:
            qi["art"] = self._song_art_files[self._song_art_idx]   # showing = selected
        self._render_song_art()

    def _song_art_goto(self):
        """Jump to the image number typed in the 'Go to #' box (1-based, matching the
        '2 / 7' display). Ignores junk / out-of-range input."""
        n = len(self._song_art_files)
        if n == 0:
            return
        raw = self.song_art_jump.get().strip()
        try:
            idx = int(raw) - 1            # display is 1-based
        except ValueError:
            return
        idx = max(0, min(idx, n - 1))     # clamp into range
        self._song_art_idx = idx
        qi = self._song_art_qitem
        if qi is not None:
            qi["art"] = self._song_art_files[idx]
        self.song_art_jump.delete(0, "end")   # clear the box after jumping
        self._render_song_art()

    _ART_BOX = 200   # fixed square size for the preview (keeps the layout from shifting)

    def _render_song_art(self):
        """Draw the current preview onto a fixed-size square so the surrounding
        buttons never jump as image sizes change."""
        n = len(self._song_art_files)
        box = self._ART_BOX
        if n == 0:
            self._song_art_thumb = self._blank_art_square(box)
            self.song_art_canvas.config(image=self._song_art_thumb,
                                        text="(no images)", compound="center")
            self.song_art_status.config(text="")
            return
        path = self._song_art_files[self._song_art_idx]
        name = os.path.basename(path)
        if len(name) > 16:
            name = name[:13] + "…"
        self.song_art_status.config(
            text=f"{self._song_art_idx+1} / {n}  ·  {name}")
        try:
            from PIL import Image
            import io as _io, base64 as _b64
            img = Image.open(path).convert("RGBA")
            img.thumbnail((box, box), Image.LANCZOS)   # fit within the square, keep aspect
            # paste centered onto a fixed box×box transparent canvas (letterbox)
            canvas = Image.new("RGBA", (box, box), (0, 0, 0, 0))
            canvas.paste(img, ((box - img.width) // 2, (box - img.height) // 2), img)
            buf = _io.BytesIO(); canvas.save(buf, format="PNG")
            self._song_art_thumb = tk.PhotoImage(data=_b64.b64encode(buf.getvalue()).decode("ascii"))
            self.song_art_canvas.config(image=self._song_art_thumb, text="", compound="center")
        except Exception:
            self._song_art_thumb = self._blank_art_square(box)
            self.song_art_canvas.config(image=self._song_art_thumb,
                                        text="(can't preview)", compound="center")

    def _blank_art_square(self, box):
        """A fixed-size fully-transparent square, so the preview area always occupies
        the same space even when there's no image. Cached (it never changes)."""
        cached = getattr(self, "_blank_thumb", None)
        if cached is not None:
            return cached
        try:
            from PIL import Image
            import io as _io, base64 as _b64
            blank = Image.new("RGBA", (box, box), (0, 0, 0, 0))
            buf = _io.BytesIO(); blank.save(buf, format="PNG")
            self._blank_thumb = tk.PhotoImage(data=_b64.b64encode(buf.getvalue()).decode("ascii"))
            return self._blank_thumb
        except Exception:
            return None

    def _set_song_art_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.song_art_prev.config(state=state)
        self.song_art_next.config(state=state)
        self.song_art_jump.config(state=state)

    def _update_cfg(self, **kw):
        cfg = load_config(); cfg.update(kw); save_config(cfg)

    def choose_lib(self):
        folder = filedialog.askdirectory(title="Select BMS library root")
        if folder:
            self.begin_scan(folder)

    def choose_output(self):
        folder = filedialog.askdirectory(title="Select output folder for FLACs")
        if not folder:
            return
        # don't allow output inside the read-only library
        try:
            assert_safe_output(os.path.join(folder, "_probe.flac"))
        except PermissionError:
            messagebox.showerror("Invalid output folder",
                "That folder is inside your read-only library. Pick a different location.")
            return
        self._set_path(self.out_lbl, folder)
        self._update_cfg(output=folder)
        self.log(f"Output folder: {folder}")

    def begin_scan(self, folder):
        set_library_root(folder)
        self._update_cfg(library=folder)
        self._set_path(self.lib_lbl, folder)
        self.log(f"Library: {folder}  (READ-ONLY for this app)")
        threading.Thread(target=self.scan, args=(folder,), daemon=True).start()

    def scan(self, folder):
        # Runs on a WORKER thread. It must not touch widgets directly.
        # self.log is already thread-safe; results are handed back via self.after.
        t0 = time.time()
        _migrate_old_cache()   # remove any leftover JSON cache from older versions
        self.log(f"Cache database: {DB_PATH}")
        # SQLite connections are per-thread; open one here on the worker thread.
        conn = db_connect()
        try:
            prior = conn.execute("SELECT COUNT(*) FROM charts").fetchone()[0]
            self.log(f"Cache has {prior} charts. Checking for changes…"
                     if prior else "Empty cache — first full scan.")
            songs, stats = scan_library(
                folder, conn, log=self.log,
                progress=lambda d, t: self.log(f"  …{d}/{t}") if d and d % 5000 == 0 else None)
        finally:
            conn.close()
        for s in songs:
            s["search"] = (s["title"] + " " + (s["artist"] or "")).lower()
        self.log(f"Done in {time.time()-t0:.1f}s — {stats['total']} charts "
                 f"({stats['reused']} cached, {stats['parsed']} parsed, "
                 f"{stats['removed']} removed).")
        # Hand the finished data back to the MAIN thread to update state + UI.
        self.after(0, self._scan_finished, songs)

    def _scan_finished(self, songs):
        # Runs on the MAIN thread.
        self.songs = songs
        # index by md5 for fast table matching
        self._md5_index = {}
        for s in songs:
            m = s.get("md5")
            if m:
                self._md5_index.setdefault(m, s)
        self.apply_filter()
        self.reload_tables()
        # now that the library + md5 index exist, migrate any leftover path entries
        # to hash-based, and refresh the open playlist view
        self._migrate_playlists()
        if self.pl_pick.get():
            self.show_playlist()

    # ------------------------------------------------------------- tables tab
    def add_table_by_url(self):
        """Prompt for a table URL, fetch it to learn its real name, then add it to
        tables.json and the dropdown automatically (no manual name entry needed)."""
        from tkinter import simpledialog
        url = simpledialog.askstring("Add table",
                                     "Paste the difficulty-table URL:", parent=self)
        if not url or not url.strip():
            return
        url = url.strip()
        if any(t.get("url") == url for t in getattr(self, "_table_defs", [])):
            self.table_status.config(text="That table URL is already in the list.")
            return
        self.table_status.config(text="Fetching table…")
        threading.Thread(target=self._add_table_worker, args=(url,), daemon=True).start()

    def _add_table_worker(self, url):
        try:
            self.log(f"Adding table from {url} …")
            tbl = fetch_table(url, log=self.log)
            name = tbl.get("name") or url
            entries = tbl.get("entries", [])
        except Exception as e:
            self.log(f"  could not add table: {e}")
            self.after(0, lambda: self.table_status.config(text="Couldn't fetch that table."))
            return
        def finish():
            # de-dupe by name: if the fetched name already exists, keep it unique
            existing = {t.get("name") for t in self._table_defs}
            final = name; n = 2
            while final in existing:
                final = f"{name} ({n})"; n += 1
            self._table_defs.append({"name": final, "url": url})
            save_tables_file(self._table_defs)
            self._table_cache[final] = tbl
            tbl["_defname"] = final
            self.table_pick.config(values=[t["name"] for t in self._table_defs])
            self.table_pick.set(final)
            self.show_table()
            self.log(f"  added '{final}' ({len(entries)} entries)")
            self.table_status.config(text=f"Added {final}")
        self.after(0, finish)

    def reload_tables(self):
        """Read tables.json (list of {name,url}); create a template if absent.
        Populates the dropdown with table names; selecting one auto-loads it."""
        if not os.path.exists(TABLES_PATH):
            save_tables_file([])
            self.log("No tables yet — click 'Add table by URL…' to add one.")
        self._table_defs = load_tables_file()
        self._table_cache = {}     # name -> fetched table (so re-selecting is instant)
        names = [t.get("name", t.get("url","?")) for t in self._table_defs]
        self.table_pick.config(values=names)
        if names:
            self.table_status.config(text=f"{len(names)} table(s) — pick one to load")
        else:
            self.table_status.config(text="No tables — use 'Add table by URL…'")

    def on_table_pick(self):
        """Dropdown selection: load that table (from cache, or fetch it once)."""
        name = self.table_pick.get()
        if not name:
            return
        if name in getattr(self, "_table_cache", {}):
            self.show_table(); return
        tdef = next((t for t in self._table_defs
                     if t.get("name", t.get("url")) == name), None)
        if not tdef or "example.com" in tdef.get("url",""):
            self.table_status.config(text="Edit tables.json with a real URL first.")
            return
        self.table_status.config(text=f"Loading {name}…")
        threading.Thread(target=self._fetch_one_worker,
                         args=(name, tdef.get("url","")), daemon=True).start()

    def refresh_current_table(self):
        """Force a re-fetch of the currently selected table."""
        name = self.table_pick.get()
        if name and getattr(self, "_table_cache", {}).pop(name, None) is not None:
            pass
        self.on_table_pick()

    def _fetch_one_worker(self, name, url):
        try:
            self.log(f"Fetching table: {name} …")
            tbl = fetch_table(url, log=self.log)
            tbl["_defname"] = name
            self.log(f"  got {len(tbl['entries'])} entries")
        except Exception as e:
            self.log(f"  failed to fetch {url}: {e}")
            self.after(0, lambda: self.table_status.config(text=f"Failed to load {name}"))
            return
        def done():
            self._table_cache[name] = tbl
            self.show_table()
        self.after(0, done)

    def show_table(self):
        name = self.table_pick.get()
        tbl = getattr(self, "_table_cache", {}).get(name)
        self.ttree.delete(*self.ttree.get_children())
        self._row_by_iid = {}
        self._tbl_play_order = []
        self._tbl_by_level = None     # cached level→entries for re-sorting
        self._tbl_sort_col = None
        self._tbl_sort_desc = False
        if not tbl:
            return
        idx = getattr(self, "_md5_index", {})
        by_level = {}
        for e in tbl["entries"]:
            by_level.setdefault(e["level"], []).append(e)
        self._tbl_by_level = by_level
        self._tbl_symbol = tbl.get("symbol", "")
        self._tbl_name = tbl["name"]
        self._tbl_total = len(tbl["entries"])
        self._render_table_rows()

    def _render_table_rows(self):
        """(Re)draw the level-grouped table from self._tbl_by_level, applying the
        current within-level sort if one is set."""
        self.ttree.delete(*self.ttree.get_children())
        self._row_by_iid = {}
        self._tbl_play_order = []
        idx = getattr(self, "_md5_index", {})
        def level_key(lv):
            try: return (0, float(lv))
            except ValueError: return (1, lv)
        # builds the display value for a column so sorting matches what's shown
        def sortkey(col):
            def k(e):
                song = idx.get(e["md5"])
                owned = song is not None
                if col == "title":
                    return ((song["title"] if owned else e["title"]) or "").lower()
                if col == "artist":
                    return ((song["artist"] if owned else e.get("artist","")) or "").lower()
                if col == "owned":
                    return 0 if owned else 1          # owned first
                if col == "notes":
                    return _num(song.get("notes")) if owned else -1
                return ""
            return k
        owned_count = 0; rid = 0
        sym = getattr(self, "_tbl_symbol", "")
        for lv in sorted(self._tbl_by_level, key=level_key):
            entries = list(self._tbl_by_level[lv])
            if self._tbl_sort_col:
                entries.sort(key=sortkey(self._tbl_sort_col), reverse=self._tbl_sort_desc)
            owned_here = sum(1 for e in entries if e["md5"] in idx)
            parent = self.ttree.insert(
                "", "end", iid=f"L{lv}", open=True,
                text=f"{sym}{lv}  ({owned_here}/{len(entries)})",
                values=("", "", "", ""))
            for e in entries:
                song = idx.get(e["md5"])
                owned = song is not None
                if owned:
                    owned_count += 1
                iid = f"r{rid}"; rid += 1
                self._row_by_iid[iid] = {"entry": e, "song": song, "owned": owned}
                if owned:
                    self._tbl_play_order.append(song)
                title = (song["title"] if owned else e["title"]) or e["title"] or "(unknown)"
                artist = (song["artist"] if owned else e["artist"]) if owned else e["artist"]
                notes = song.get("notes","") if owned else ""
                self.ttree.insert(parent, "end", iid=iid, text="",
                                  tags=() if owned else ("missing",),
                                  values=(title, artist, "✓" if owned else "missing", notes))
        # header arrows
        for c in ("title", "artist", "owned", "notes"):
            arrow = (" ▼" if self._tbl_sort_desc else " ▲") if c == self._tbl_sort_col else ""
            self.ttree.heading(c, text=c.title() + arrow)
        self.table_status.config(
            text=f"{getattr(self,'_tbl_name','')}: you own {owned_count} of {getattr(self,'_tbl_total',0)}")

    def sort_table(self, col):
        """Sort songs within each level group by a column (toggles asc/desc)."""
        if not getattr(self, "_tbl_by_level", None):
            return
        if self._tbl_sort_col == col:
            self._tbl_sort_desc = not self._tbl_sort_desc
        else:
            self._tbl_sort_col, self._tbl_sort_desc = col, False
        self._render_table_rows()

    def _table_row_at(self, iid):
        return getattr(self, "_row_by_iid", {}).get(iid)

    def on_table_select(self, _):
        sel = self.ttree.selection()
        if not sel:
            return
        row = self._table_row_at(sel[0])
        if row and row["owned"]:
            s = row["song"]
            self._update_info(s)
            self._show_tags(self._default_tags(s), editable=False,
                            hint="From a difficulty table (read-only) — add to Queue to edit tags.")
            self._load_song_art_for(s["path"], None)   # read-only preview

    def _table_rightclick(self, event):
        iid = self.ttree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.ttree.selection():
            self.ttree.selection_set(iid)
        # Level header rows have iids like "L<level>" and aren't in _row_by_iid.
        if iid.startswith("L") and iid not in getattr(self, "_row_by_iid", {}):
            self._ctx_level = iid[1:]   # the level string
            self._levelmenu.tk_popup(event.x_root, event.y_root)
            return
        row = self._table_row_at(iid)
        owned = bool(row and row["owned"])
        state = "normal" if owned else "disabled"
        # entries: 0 Play, 1 Add to Queue, 2 Show all, 3 Add to playlist (cascade)
        for i in range(4):
            self._tmenu.entryconfig(i, state=state)
        if owned:
            self._fill_playlist_submenu(self._tbl_plmenu, self._tbl_selected_song)
        self._tmenu.tk_popup(event.x_root, event.y_root)

    def add_level_to_queue(self):
        """Queue all owned charts in the right-clicked level (skips duplicates)."""
        level = getattr(self, "_ctx_level", None)
        if level is None:
            return
        # collect owned songs whose entry level matches, one per folder (lowest notes)
        owned = [r["song"] for r in self._row_by_iid.values()
                 if r.get("owned") and r.get("song") and r["entry"].get("level") == level]
        if not owned:
            self.log(f"No owned charts in level {level}."); return
        best = {}
        for s in owned:
            folder = os.path.dirname(os.path.normcase(os.path.abspath(s["path"])))
            cur = best.get(folder)
            if cur is None or _num(s.get("notes")) < _num(cur.get("notes")):
                best[folder] = s
        queued = {q["path"] for q in self.queue}
        added = 0
        for s in best.values():
            if s["path"] in queued:
                continue
            self.queue.append(self._make_queue_item(s))
            added += 1
        self.refresh_queue()
        self.log(f"Added {added} song(s) from level {level} to the queue "
                 f"({len(owned)-added} skipped).")

    def _tbl_selected_song(self):
        sel = self.ttree.selection()
        if not sel:
            return None
        row = self._table_row_at(sel[0])
        return row["song"] if (row and row["owned"]) else None

    def _tbl_start(self, s):
        """Start playing a table song, carrying its index in the table's owned-song
        order so playback auto-advances down the list (like the Queue)."""
        if not (s and _SD_OK):
            return
        order = getattr(self, "_tbl_play_order", [])
        idx = next((i for i, o in enumerate(order) if o["path"] == s["path"]), -1)
        self._start_song("table", idx, s["path"], s["title"])

    def _tbl_play(self):
        self._tbl_start(self._tbl_selected_song())

    def on_table_activate(self, event):
        # double-click → play. Resolve the row under the cursor directly (Treeview's
        # .selection() may not be updated yet on the double-click, and level-header
        # rows must be ignored), mirroring how the right-click handler works.
        if not _SD_OK:
            return
        iid = self.ttree.identify_row(event.y)
        if not iid:
            return
        self.ttree.selection_set(iid)
        row = self._table_row_at(iid)
        if row and row["owned"]:
            self._tbl_start(row["song"])

    def _tbl_add_queue(self):
        s = self._tbl_selected_song()
        if not s:
            return
        if any(q["path"] == s["path"] for q in self.queue):
            self.log(f"Already in queue: {s['title']}"); return
        self.queue.append(self._make_queue_item(s))
        self.refresh_queue()
        self.log(f"Queued: {s['title']}")

    def add_table_to_queue(self):
        """Queue every owned chart in the current table, one per song (folder),
        keeping the lowest-notecount chart and skipping anything already queued."""
        if not getattr(self, "_row_by_iid", None):
            self.log("Load a table first."); return
        owned = [r["song"] for r in self._row_by_iid.values() if r.get("owned") and r.get("song")]
        if not owned:
            self.log("No owned charts in this table to queue."); return
        # one per folder: keep the chart with the fewest notes
        best = {}
        for s in owned:
            folder = os.path.dirname(os.path.normcase(os.path.abspath(s["path"])))
            cur = best.get(folder)
            if cur is None or _num(s.get("notes")) < _num(cur.get("notes")):
                best[folder] = s
        queued_paths = {q["path"] for q in self.queue}
        added = 0
        for s in best.values():
            if s["path"] in queued_paths:
                continue
            self.queue.append(self._make_queue_item(s))
            added += 1
        self.refresh_queue()
        skipped = len(owned) - added
        self.log(f"Added {added} song(s) to the queue from the table "
                 f"({skipped} skipped as duplicates/already-queued).")

    def _tbl_show_all(self):
        """Filter the Library to every chart that's the same song (keysound overlap)."""
        self._show_all_for_song(self._tbl_selected_song())

    def _show_all_for_song(self, s):
        """Filter Library to every chart in the same folder as `s` (same-song = same
        directory). Folders almost always hold all difficulties of a song together."""
        if not s or not s.get("path"):
            return
        target_dir = os.path.dirname(os.path.normcase(os.path.abspath(s["path"])))
        matches = [o for o in self.songs
                   if os.path.dirname(os.path.normcase(os.path.abspath(o["path"]))) == target_dir]
        matches.sort(key=lambda o: _num(o.get("notes")))
        self.songs_only.set(False)   # show-all lists individual charts, not grouped
        self.filtered = matches
        self._reverse_lookup = True
        self.exit_lookup_btn.pack(side="left", padx=(6,0))  # show the exit button
        self.nb.select(self.lib_tab)
        self._populate_tree(matches)
        self.log(f"Showing {len(matches)} chart(s) in the same folder as "
                 f"“{s.get('title','?')}”. Click 'Exit lookup' to return.")

    def exit_lookup(self):
        """Leave 'show all charts' mode and restore the full library view."""
        self._reverse_lookup = False
        self.exit_lookup_btn.pack_forget()
        self.apply_filter()

    # --- right-click on Library / Queue trees ---
    def _lib_rightclick(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        # Keep an existing multi-selection if the clicked row is part of it;
        # otherwise select just the clicked row (standard file-manager behaviour).
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self._ctx_song = self._lib_song_at(iid)
        if not self._ctx_song:
            return
        self._popup_lqmenu(event)

    def _queue_rightclick(self, event):
        iid = self.qtree.identify_row(event.y)
        if not iid:
            return
        self.qtree.selection_set(iid)
        try:
            q = self.queue[int(iid)]
        except (ValueError, IndexError):
            return
        # resolve to the full song record for keysound/show-all
        self._ctx_song = next((s for s in self.songs if s["path"] == q["path"]), q)
        self._popup_lqmenu(event)

    def _popup_lqmenu(self, event):
        # "Add to Queue" is disabled when the Queue tab is active
        try:
            on_queue_tab = (self.nb.index(self.nb.select()) == self.nb.index(self.q_tab))
        except tk.TclError:
            on_queue_tab = False
        self._lqmenu.entryconfig(1, state="disabled" if on_queue_tab else "normal")
        # Play disabled if audio unavailable
        self._lqmenu.entryconfig(0, state="normal" if _SD_OK else "disabled")
        self._fill_playlist_submenu(self._lq_plmenu, lambda: self._ctx_song)
        self._lqmenu.tk_popup(event.x_root, event.y_root)

    def _ctx_play(self):
        s = self._ctx_song
        if not (s and _SD_OK):
            return
        # advance correctly for the tab the menu fired from
        try:
            cur = self.nb.index(self.nb.select())
        except tk.TclError:
            cur = -1
        if cur == self.nb.index(self.q_tab):
            i = next((j for j, q in enumerate(self.queue) if q["path"] == s["path"]), -1)
            self._start_song("queue", i, s["path"], s.get("title", ""))
        else:  # Library: play this one, no auto-advance
            self._start_song("library", -1, s["path"], s["title"])

    def _ctx_add_queue(self, songs=None):
        """Add songs to the queue. If `songs` is given (from a specific context menu),
        queue exactly those. Otherwise this was invoked from the Library context menu /
        button, so use the Library's current (possibly multi-) selection, falling back
        to the single right-clicked row."""
        if songs is None:
            songs = self._selected_library_songs()
            if not songs and self._ctx_song:
                songs = [self._ctx_song]
        self._enqueue_songs(songs)

    def _enqueue_songs(self, songs):
        if not songs:
            return
        queued = {q["path"] for q in self.queue}
        added = 0
        for s in songs:
            if s["path"] in queued:
                continue
            self.queue.append(self._make_queue_item(s))
            queued.add(s["path"])
            added += 1
        self.refresh_queue()
        if added == 1:
            self.log(f"Queued: {songs[0]['title']}")
        elif added > 1:
            self.log(f"Queued {added} songs ({len(songs) - added} already in queue).")
        else:
            self.log("All selected songs are already in the queue.")

    def _selected_library_songs(self):
        """All currently-selected Library rows as song dicts (in display order)."""
        out, seen = [], set()
        for iid in self.tree.selection():
            s = self._lib_song_at(iid)
            if s and s["path"] not in seen:
                seen.add(s["path"])
                out.append(s)
        return out

    def _populate_grouped(self, rows, token):
        """Songs-only mode: one parent row per song folder (expandable to its
        charts). Grouping collapses the list enough that no row cap is needed."""
        self.tree.config(show="tree headings")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=28, minwidth=28, stretch=False)
        self.tree.heading("notes", text="Chart count")
        groups = {}                      # folder -> [charts in filtered order]
        order = []
        for s in rows:
            folder = os.path.dirname(s["path"])
            if folder not in groups:
                groups[folder] = []
                order.append(folder)
            groups[folder].append(s)
        # rep = lowest-notecount chart; charts listed simplest-first when expanded
        self._lib_groups = []
        for folder in order:
            charts = sorted(groups[folder], key=lambda s: _num(s.get("notes")))
            self._lib_groups.append(charts)

        def insert_batch(start):
            if token != self._fill_token:
                return
            end = min(start + self.BATCH, len(self._lib_groups))
            for gi in range(start, end):
                charts = self._lib_groups[gi]
                rep = charts[0]
                tags = ("random",) if rep.get("random") else ()
                self.tree.insert("", "end", iid=f"g{gi}", tags=tags,
                                 values=(rep["title"], rep["artist"], rep["bpm"],
                                         rep.get("mode","?"),
                                         f"{len(charts)} chart{'s' if len(charts)!=1 else ''}"))
                # dummy child so the expander (+) shows; filled on first open
                self.tree.insert(f"g{gi}", "end", iid=f"g{gi}_dummy", values=("...","","","",""))
            if end < len(self._lib_groups):
                self.after(1, insert_batch, end)
            else:
                self.log(f"Showing {len(self._lib_groups):,} songs "
                         f"({len(rows):,} charts grouped by folder).")

        if self._lib_groups:
            insert_batch(0)
        else:
            self.log(f"Showing 0 of {len(self.songs):,} charts.")

    def _lib_group_open(self, _):
        """Fill in a group's real chart rows the first time it's expanded."""
        if not self.songs_only.get() or not getattr(self, "_lib_groups", None):
            return
        iid = self.tree.focus()
        if not (iid.startswith("g") and "_" not in iid):
            return
        dummy = f"{iid}_dummy"
        if not self.tree.exists(dummy):
            return                       # already filled
        self.tree.delete(dummy)
        gi = int(iid[1:])
        for j, s in enumerate(self._lib_groups[gi]):
            tags = ("random",) if s.get("random") else ()
            self.tree.insert(iid, "end", iid=f"c{gi}_{j}", tags=tags,
                             values=(s["title"], s["artist"], s["bpm"],
                                     s.get("mode","?"), s.get("notes","")))

    def _lib_song_at(self, iid):
        """Resolve a library tree iid to a song dict in both flat and grouped
        modes. Group parents resolve to the chart double-click would play."""
        try:
            if iid.startswith("c") and "_" in iid:          # grouped child = a chart
                gi, j = iid[1:].split("_")
                return self._lib_groups[int(gi)][int(j)]
            if iid.startswith("g"):                          # group parent = the song
                return pick_playable_chart(self._lib_groups[int(iid[1:])])
            return self.filtered[int(iid)]                   # flat row
        except (ValueError, IndexError, AttributeError, TypeError):
            return None

    # ------------------------------------------------------------- Discovery
    _DISC_TILE = 170     # art square size (px)
    _DISC_PAD  = 14      # gap between tiles

    def _on_tab_changed(self, _):
        try:
            if self.nb.select() == str(self.disc_tab) and not self._disc_loaded:
                self._disc_loaded = True
                self.discovery_reset()
        except tk.TclError:
            pass

    # ---- Virtualized grid: a fixed pool of tile widgets is reused as you scroll;
    #      only the songs currently in view (plus a small buffer) are ever drawn,
    #      so the library can be huge without thousands of live widgets. ----
    def discovery_reset(self):
        import random
        if not self.songs:
            self.disc_status.config(text="scan a library first -- then come back to explore it")
            return
        self._disc_token += 1
        groups = {}
        for s in self.songs:
            groups.setdefault(os.path.dirname(s["path"]), []).append(s)
        order = []
        for charts in groups.values():
            charts = sorted(charts, key=lambda s: _num(s.get("notes")))
            order.append((charts[0], charts))
        random.shuffle(order)
        self._disc_order = order
        self._disc_thumbs.clear(); self._disc_thumb_order.clear()
        self._disc_pending.clear()
        self._disc_pool_map.clear()
        self._disc_scroll = 0
        self.disc_status.config(text=f"{len(order):,} songs -- scroll to explore, "
                                     "double-click to listen")
        self.after_idle(lambda: self._disc_relayout(rebuild_pool=True))

    def _disc_cols_for_width(self, w):
        return max(1, w // (self._DISC_TILE + self._DISC_PAD * 2))

    def _disc_content_height(self):
        if not self._disc_order:
            return 0
        rows = (len(self._disc_order) + self._disc_cols - 1) // self._disc_cols
        return rows * self._DISC_ROW_H + self._DISC_PAD

    def _disc_relayout(self, rebuild_pool=False):
        """Recompute columns, size the tile pool to the viewport, and redraw."""
        cw = self.disc_canvas.winfo_width()
        ch = self.disc_canvas.winfo_height()
        if cw <= 1 or ch <= 1 or not self._disc_order:
            return
        self._disc_cols = self._disc_cols_for_width(cw)
        # how many rows fit, plus 2 buffer rows above/below
        vis_rows = ch // self._DISC_ROW_H + 3
        need = vis_rows * self._disc_cols
        if rebuild_pool or need > len(self._disc_pool):
            for slot in range(len(self._disc_pool), need):
                self._disc_pool.append(self._disc_new_tile(slot))
        # clamp scroll to content
        max_scroll = max(0, self._disc_content_height() - ch)
        self._disc_scroll = max(0, min(self._disc_scroll, max_scroll))
        self._disc_redraw()

    def _disc_new_tile(self, slot):
        tile = ttk.Frame(self.disc_canvas, width=self._DISC_TILE,
                         height=self._DISC_TILE_H)
        tile.grid_propagate(False)
        tile.grid_columnconfigure(0, weight=1)
        art = tk.Label(tile, bg="#000000", bd=1, relief="solid")
        art.grid(row=0, column=0)
        t_lbl = tk.Label(tile, anchor="center", width=1)
        t_lbl.grid(row=1, column=0, sticky="ew", pady=(3,0))
        a_lbl = tk.Label(tile, anchor="center", fg="#777", width=1)
        a_lbl.grid(row=2, column=0, sticky="ew")
        t = {"frame": tile, "art": art, "title": t_lbl, "artist": a_lbl,
             "idx": None, "win": None, "hover": 0}
        for w in (tile, art, t_lbl, a_lbl):
            w.bind("<Button-1>", lambda e, t=t: self._disc_click(t))
            w.bind("<Double-Button-1>", lambda e, t=t: self._disc_dblclick(t))
            w.bind("<Button-3>", lambda e, t=t: self._disc_menu(e, t))
            w.bind("<Enter>", lambda e, t=t: self._disc_hover(t, True))
            w.bind("<Leave>", lambda e, t=t: self._disc_hover(t, False))
        return t

    def _disc_redraw(self):
        """Place pool tiles over exactly the song cells now visible."""
        ch = self.disc_canvas.winfo_height()
        cols = self._disc_cols
        first_row = self._disc_scroll // self._DISC_ROW_H
        first = first_row * cols
        slot = 0
        y0 = -(self._disc_scroll % self._DISC_ROW_H) if False else 0
        # absolute y of a row = row*ROW_H - scroll; place tiles by canvas window
        used = set()
        i = first
        while slot < len(self._disc_pool) and i < len(self._disc_order):
            row, col = divmod(i, cols)
            x = self._DISC_PAD + col * (self._DISC_TILE + self._DISC_PAD * 2)
            y = self._DISC_PAD + row * self._DISC_ROW_H - self._disc_scroll
            if y > ch:
                break
            t = self._disc_pool[slot]
            self._disc_fill_tile(t, i)
            if t["win"] is None:
                t["win"] = self.disc_canvas.create_window(x, y, anchor="nw",
                                                          window=t["frame"])
            else:
                self.disc_canvas.coords(t["win"], x, y)
                self.disc_canvas.itemconfigure(t["win"], state="normal")
            used.add(slot)
            slot += 1; i += 1
        # hide any unused pool tiles
        for s in range(len(self._disc_pool)):
            if s not in used and self._disc_pool[s]["win"] is not None:
                self.disc_canvas.itemconfigure(self._disc_pool[s]["win"], state="hidden")
        self._disc_update_scrollbar()

    def _disc_fill_tile(self, t, idx):
        if t["idx"] == idx:
            return
        t["idx"] = idx
        rep, charts = self._disc_order[idx]
        full = rep["title"] or "(unknown)"
        self._marquee_stop(t["title"], full, None)   # clear any marquee from prior song
        t["title"].config(text=self._ellipsize(full, 22))
        t["_fulltitle"] = full
        t["artist"].config(text=self._ellipsize(rep["artist"] or "", 24))
        img = self._disc_thumbs.get(idx)
        if img is not None:
            t["art"].config(image=img)
            self._disc_touch_cache(idx)
        else:
            t["art"].config(image=self._disc_placeholder())
            self._disc_request_thumb(idx)

    def _disc_hover(self, t, entering):
        full = t.get("_fulltitle", "")
        if len(full) <= 22:
            return
        if entering:
            # cancel any pending stop from crossing between the tile's children
            pend = t.get("_hover_stop")
            if pend:
                try: self.after_cancel(pend)
                except Exception: pass
                t["_hover_stop"] = None
            if not getattr(t["title"], "_marquee_job", None):
                self._marquee_start(t["title"], full, 22)
        else:
            # defer the stop briefly; a re-Enter on a sibling widget cancels it
            def stop():
                t["_hover_stop"] = None
                self._marquee_stop(t["title"], full, 22)
            t["_hover_stop"] = self.after(60, stop)

    @staticmethod
    def _ellipsize(s, n):
        return s if len(s) <= n else s[:n-1] + "\u2026"

    def _marquee_start(self, label, full, n):
        self._marquee_stop(label, full, None)
        pad = full + "    "
        def step(pos=0):
            label.config(text=(pad[pos:] + pad[:pos])[:n])
            label._marquee_job = self.after(180, step, (pos + 1) % len(pad))
        step()

    def _marquee_stop(self, label, full, n):
        job = getattr(label, "_marquee_job", None)
        if job:
            try: self.after_cancel(job)
            except Exception: pass
            label._marquee_job = None
        if n is not None:
            label.config(text=self._ellipsize(full, n))

    def _disc_placeholder(self):
        cached = getattr(self, "_disc_ph_img", None)
        if cached is not None:
            return cached
        try:
            from PIL import Image
            import io as _io, base64 as _b64
            img = Image.new("RGB", (self._DISC_TILE, self._DISC_TILE), (25, 25, 25))
            buf = _io.BytesIO(); img.save(buf, format="PNG")
            self._disc_ph_img = tk.PhotoImage(data=_b64.b64encode(buf.getvalue()).decode("ascii"))
        except Exception:
            self._disc_ph_img = None
        return self._disc_ph_img

    def _disc_request_thumb(self, idx):
        if idx in self._disc_pending or idx in self._disc_thumbs:
            return
        self._disc_pending.add(idx)
        token = self._disc_token
        threading.Thread(target=self._disc_decode, args=(token, idx), daemon=True).start()

    def _disc_decode(self, token, idx):
        try:
            from PIL import Image
            import io as _io, base64 as _b64
        except Exception:
            return
        if token != self._disc_token:
            return
        rep, charts = self._disc_order[idx]
        data = None
        try:
            art = pick_discovery_art(rep["path"])
            if art:
                img = Image.open(art).convert("RGB")
                img.thumbnail((self._DISC_TILE - 2, self._DISC_TILE - 2), Image.LANCZOS)
                canvas = Image.new("RGB", (self._DISC_TILE, self._DISC_TILE), (0, 0, 0))
                canvas.paste(img, ((self._DISC_TILE - img.width) // 2,
                                   (self._DISC_TILE - img.height) // 2))
                buf = _io.BytesIO(); canvas.save(buf, format="PNG")
                data = _b64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            data = None
        self.after(0, self._disc_thumb_ready, token, idx, data)

    def _disc_thumb_ready(self, token, idx, data):
        self._disc_pending.discard(idx)
        if token != self._disc_token or data is None:
            return
        try:
            img = tk.PhotoImage(data=data)
        except tk.TclError:
            return
        self._disc_thumbs[idx] = img
        self._disc_thumb_order.append(idx)
        # cap the cache so memory stays bounded on a huge library
        while len(self._disc_thumb_order) > 600:
            old = self._disc_thumb_order.pop(0)
            if old != idx:
                self._disc_thumbs.pop(old, None)
        # if this song is currently on a visible tile, show it now
        for t in self._disc_pool:
            if t["idx"] == idx and t["win"] is not None:
                t["art"].config(image=img)
                break

    def _disc_touch_cache(self, idx):
        try:
            self._disc_thumb_order.remove(idx)
            self._disc_thumb_order.append(idx)
        except ValueError:
            pass

    def _disc_update_scrollbar(self):
        total = self._disc_content_height()
        ch = self.disc_canvas.winfo_height()
        if total <= 0:
            self.disc_vsb.set(0, 1); return
        top = self._disc_scroll / total
        bot = min(1.0, (self._disc_scroll + ch) / total)
        self.disc_vsb.set(top, bot)

    def _disc_yview(self, *args):
        total = self._disc_content_height()
        ch = self.disc_canvas.winfo_height()
        max_scroll = max(0, total - ch)
        if args[0] == "moveto":
            self._disc_scroll = int(float(args[1]) * total)
        elif args[0] == "scroll":
            amt = int(args[1])
            unit = self._DISC_ROW_H if args[2] == "pages" else 60
            self._disc_scroll += amt * unit
        self._disc_scroll = max(0, min(self._disc_scroll, max_scroll))
        self._disc_redraw()

    def _disc_on_resize(self, event):
        self._disc_relayout()

    def _disc_mousewheel(self, event):
        try:
            if self.nb.select() != str(self.disc_tab):
                return
            self._disc_scroll -= (event.delta // 120) * 90
            total = self._disc_content_height()
            ch = self.disc_canvas.winfo_height()
            self._disc_scroll = max(0, min(self._disc_scroll, max(0, total - ch)))
            self._disc_redraw()
        except tk.TclError:
            pass

    def _disc_click(self, t):
        if t["idx"] is None: return
        rep, charts = self._disc_order[t["idx"]]
        self.selected_kind = "library"; self.selected_index = None
        self._update_info(rep)
        self._show_tags(self._default_tags(rep), editable=False,
                        hint="Discovery preview (read-only) -- add to Queue to edit tags.")
        self._load_song_art_for(rep["path"], None)

    def _disc_dblclick(self, t):
        if t["idx"] is None or not _SD_OK: return
        rep, charts = self._disc_order[t["idx"]]
        s = pick_playable_chart(charts)
        if s:
            self._start_song("library", 0, s["path"], s["title"])

    def _disc_menu(self, event, t):
        if t["idx"] is None: return
        self._disc_click(t)
        rep, charts = self._disc_order[t["idx"]]
        s = pick_playable_chart(charts)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Play", command=lambda: self._disc_dblclick(t))
        menu.add_command(label="Add to Queue",
                         command=lambda: self._enqueue_songs([s] if s else []))
        menu.add_command(label="Show all charts for this song",
                         command=lambda: self._show_all_for_song(rep))
        plmenu = tk.Menu(menu, tearoff=0)
        self._fill_playlist_submenu(plmenu, lambda: rep)
        menu.add_cascade(label="Add to playlist", menu=plmenu)
        menu.tk_popup(event.x_root, event.y_root)

    def _ctx_show_all(self):
        self._show_all_for_song(self._ctx_song)

    # ----------------------------------------------------------- playlists tab
    def _fill_playlist_submenu(self, menu, get_song):
        """Rebuild an 'Add to playlist' submenu: one entry per playlist + New…."""
        menu.delete(0, "end")
        for name in sorted(self._playlists):
            menu.add_command(label=name,
                             command=lambda n=name: self._add_song_to_playlist(get_song(), n))
        if self._playlists:
            menu.add_separator()
        menu.add_command(label="New playlist…",
                         command=lambda: self._add_song_to_new_playlist(get_song()))

    def _add_song_to_playlist(self, song, name):
        if not song:
            return
        md5 = song.get("md5", "")
        if not md5:
            self.log(f"Can't add '{song.get('title','?')}' — no MD5 available "
                     f"(rescan the library so it has a hash)."); return
        entries = self._playlists.setdefault(name, [])
        if any(isinstance(e, dict) and e.get("md5") == md5 for e in entries):
            self.log(f"Already in '{name}': {song['title']}"); return
        entries.append({"md5": md5, "title": song.get("title",""),
                        "artist": song.get("artist","")})
        save_one_playlist(name, entries)
        self.log(f"Added to '{name}': {song['title']}")
        if self.pl_pick.get() == name:
            self.show_playlist()

    def _add_song_to_new_playlist(self, song):
        if not song:
            return
        from tkinter import simpledialog
        name = simpledialog.askstring("New playlist", "Playlist name:", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        self._playlists.setdefault(name, [])
        self._add_song_to_playlist(song, name)
        self.refresh_playlist_picker(select=name)

    def load_playlists_into_ui(self):
        self._playlists = load_playlists()
        self._migrate_playlists()
        self.refresh_playlist_picker()

    def _migrate_playlists(self):
        """Convert any old path-based entries (plain strings) to hash-based
        {md5,title,artist} dicts using the library cache. Silent; runs as needed."""
        changed = False
        by_path = {s["path"]: s for s in getattr(self, "songs", [])}
        for name, entries in list(self._playlists.items()):
            new_entries = []
            for e in entries:
                if isinstance(e, dict) and e.get("md5"):
                    new_entries.append(e); continue       # already hash-based
                # old format (bare path string) or an unresolved entry with _path
                path = e if isinstance(e, str) else (e.get("_path") if isinstance(e, dict) else None)
                song = by_path.get(path) if path else None
                if song and song.get("md5"):
                    new_entries.append({"md5": song["md5"],
                                        "title": song.get("title", os.path.basename(path or "")),
                                        "artist": song.get("artist","")})
                    changed = True
                elif path:
                    new_entries.append({"md5": "", "title": os.path.basename(path),
                                        "artist": "", "_path": path})
                    changed = True
                elif isinstance(e, dict):
                    new_entries.append(e)                  # keep label-only entry
            self._playlists[name] = new_entries
        if changed:
            for name, entries in self._playlists.items():
                save_one_playlist(name, entries)

    def refresh_playlist_picker(self, select=None):
        names = sorted(self._playlists)
        self.pl_pick.config(values=names)
        if select and select in names:
            self.pl_pick.set(select); self.show_playlist()
        elif names and not self.pl_pick.get():
            self.pl_pick.set(names[0]); self.show_playlist()
        elif not names:
            self.pl_pick.set(""); self.pltree.delete(*self.pltree.get_children())
            self._pl_rows = []
            self.pl_status.config(text="No playlists yet — right-click a song → Add to playlist → New.")

    def new_playlist(self):
        from tkinter import simpledialog
        name = simpledialog.askstring("New playlist", "Playlist name:", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        self._playlists.setdefault(name, [])
        save_one_playlist(name, self._playlists[name])
        self.refresh_playlist_picker(select=name)

    def rename_playlist(self):
        old = self.pl_pick.get()
        if not old:
            return
        from tkinter import simpledialog
        new = simpledialog.askstring("Rename playlist", "New name:", initialvalue=old, parent=self)
        if not new or not new.strip() or new.strip() == old:
            return
        new = new.strip()
        self._playlists[new] = self._playlists.pop(old)
        delete_playlist_file(old)               # remove the old-named file
        save_one_playlist(new, self._playlists[new])
        self.refresh_playlist_picker(select=new)

    def delete_playlist(self):
        name = self.pl_pick.get()
        if not name:
            return
        if messagebox.askyesno("Delete playlist", f"Delete the playlist '{name}'?\n"
                               "(Your BMS files are not touched.)"):
            self._playlists.pop(name, None)
            delete_playlist_file(name)
            self.pl_pick.set("")
            self.refresh_playlist_picker()

    def show_playlist(self):
        name = self.pl_pick.get()
        self._pl_rows = []
        self._pl_sort_col = None       # reset sort when switching playlists
        self._pl_sort_desc = False
        if not name or name not in self._playlists:
            self.pltree.delete(*self.pltree.get_children())
            return
        idx = getattr(self, "_md5_index", {})
        missing = 0
        for entry in self._playlists[name]:
            md5 = entry.get("md5", "") if isinstance(entry, dict) else ""
            song = idx.get(md5) if md5 else None
            if song is None:
                # not in this library — show the stored label, greyed; can't play/render
                label = entry.get("title", "(unknown)") if isinstance(entry, dict) else "(unknown)"
                artist = entry.get("artist", "") if isinstance(entry, dict) else ""
                song = {"path": None, "title": label, "artist": artist,
                        "mode": "?", "notes": "", "_missing": True}
                missing += 1
            self._pl_rows.append(song)
        self._pl_missing = missing
        self._render_playlist_rows()

    def _render_playlist_rows(self):
        """(Re)draw the playlist tree from self._pl_rows (after load or sort)."""
        self.pltree.delete(*self.pltree.get_children())
        for i, song in enumerate(self._pl_rows):
            self.pltree.insert("", "end", iid=str(i),
                               tags=("missing",) if song.get("_missing") else (),
                               values=(song["title"], song.get("artist",""),
                                       song.get("mode","?"), song.get("notes","")))
        missing = getattr(self, "_pl_missing", 0)
        self.pl_status.config(text=f"{len(self._pl_rows)} songs"
                              + (f" — {missing} not in your library" if missing else ""))

    def sort_playlist(self, col):
        """Sort the current playlist view by a column (toggles asc/desc)."""
        if not self._pl_rows:
            return
        if getattr(self, "_pl_sort_col", None) == col:
            self._pl_sort_desc = not self._pl_sort_desc
        else:
            self._pl_sort_col, self._pl_sort_desc = col, False
        key_map = {"title":  lambda s: s["title"].lower(),
                   "artist": lambda s: s.get("artist","").lower(),
                   "mode":   lambda s: s.get("mode",""),
                   "notes":  lambda s: _num(s.get("notes"))}
        keyf = key_map.get(col, lambda s: s["title"].lower())
        self._pl_rows.sort(key=keyf, reverse=self._pl_sort_desc)
        for c in ("title", "artist", "mode", "notes"):
            arrow = (" ▼" if self._pl_sort_desc else " ▲") if c == col else ""
            self.pltree.heading(c, text=c.title() + arrow)
        self._render_playlist_rows()

    def _pl_selected_song(self):
        sel = self.pltree.selection()
        if not sel:
            return None
        s = self._pl_rows[int(sel[0])]
        return None if s.get("_missing") else s

    def on_playlist_select(self, _):
        sel = self.pltree.selection()
        if not sel:
            return
        s = self._pl_rows[int(sel[0])]
        self.selected_kind = "playlist"; self.selected_index = int(sel[0])
        self._update_info(s)
        self._show_tags(self._default_tags(s), editable=False,
                        hint="Playlist preview (read-only) — add to Queue to edit tags.")
        # owned songs have a real path → show their folder art preview; missing ones clear it
        self._load_song_art_for(s["path"] if not s.get("_missing") else None, None)

    def on_playlist_activate(self, _):
        sel = self.pltree.selection()
        if not sel or not _SD_OK:
            return
        i = int(sel[0]); s = self._pl_rows[i]
        if s.get("_missing"):
            return
        self._start_song("playlist", i, s["path"], s["title"])

    def _pl_rightclick(self, event):
        iid = self.pltree.identify_row(event.y)
        if not iid:
            return
        # Only change the selection if right-clicking a different row — re-selecting
        # the current row would reload the art picker and lose a scrolled-to image.
        if iid not in self.pltree.selection():
            self.pltree.selection_set(iid)
        s = self._pl_rows[int(iid)]
        owned = not s.get("_missing")
        # entries: 0 Play, 1 Add to Queue, 2 Add to playlist (cascade), 3 Remove
        for idx in (0, 1, 2):
            self._plmenu.entryconfig(idx, state="normal" if owned else "disabled")
        if owned:
            self._fill_playlist_submenu(self._pl_plmenu, self._pl_selected_song)
        self._plmenu.tk_popup(event.x_root, event.y_root)

    def _pl_play(self):
        s = self._pl_selected_song()
        if s and _SD_OK:
            sel = self.pltree.selection()
            self._start_song("playlist", int(sel[0]), s["path"], s["title"])

    def _pl_add_queue(self):
        s = self._pl_selected_song()
        if s:
            self._ctx_add_queue([s])

    def remove_from_playlist(self):
        name = self.pl_pick.get()
        sel = self.pltree.selection()
        if not name or not sel:
            return
        i = int(sel[0])
        try:
            del self._playlists[name][i]
        except (KeyError, IndexError):
            return
        save_one_playlist(name, self._playlists[name])
        self.show_playlist()

    def render_playlist(self):
        name = self.pl_pick.get()
        if not name or not self._playlists.get(name):
            messagebox.showinfo("Empty playlist", "This playlist has no songs."); return
        out_dir = load_config().get("output")
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showinfo("No output folder", "Choose an output folder first."); return
        # render into a subfolder named after the playlist
        sub = os.path.join(out_dir, self._safe_filename(name))
        try:
            os.makedirs(sub, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Couldn't create folder", str(e)); return
        # build queue-like items from the playlist's owned songs (resolved by MD5)
        idx = getattr(self, "_md5_index", {})
        items = []
        for entry in self._playlists[name]:
            md5 = entry.get("md5", "") if isinstance(entry, dict) else ""
            s = idx.get(md5) if md5 else None
            if s:
                items.append({"path": s["path"], "tags": self._default_tags(s),
                              "title": s["title"]})
        if not items:
            messagebox.showinfo("Nothing to render",
                "None of this playlist's songs are in your current library."); return
        fmt = self.fmt_pick.get().upper()
        self.pl_render_btn.config(state="disabled")
        threading.Thread(target=self._render_items,
                         args=(items, sub, fmt, self.pl_render_btn), daemon=True).start()

    # ------------------------------------------------------------- library tab
    def on_search_key(self, _):
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(180, self.apply_filter)

    def apply_filter(self):
        # Compute the filtered list (fast, pure data — fine on main thread).
        if self._reverse_lookup:
            self._reverse_lookup = False
            self.exit_lookup_btn.pack_forget()
        q = self.search.get().lower().strip(); terms = q.split()
        mode = self.mode_filter.get()
        out = []
        for s in self.songs:
            if mode != "All" and s.get("mode", "?") != mode:
                continue
            if terms and not all(t in s["search"] for t in terms):
                continue
            out.append(s)
        self.filtered = out
        self._populate_tree(out)

    # Maximum rows to actually draw at once. Inserting tens of thousands of rows
    # is what froze the UI. The full list is still searchable — narrow with the
    # search box / type filter to see more specific results.
    ROW_CAP = 2000
    BATCH = 300   # rows inserted per UI tick

    def _populate_tree(self, rows):
        # cancel any in-progress population
        self._fill_token = getattr(self, "_fill_token", 0) + 1
        token = self._fill_token
        self.tree.delete(*self.tree.get_children())
        if self.songs_only.get():
            self._populate_grouped(rows, token)
            return
        self.tree.config(show="headings")
        self.tree.heading("notes", text="Notes")
        self.tree.column("title", width=300)
        self.tree.column("#0", width=0, minwidth=0, stretch=False)
        self._lib_groups = None
        capped = rows[:self.ROW_CAP]

        def insert_batch(start):
            # if another filter started, abandon this one
            if token != self._fill_token:
                return
            end = min(start + self.BATCH, len(capped))
            for i in range(start, end):
                s = capped[i]
                tags = ("random",) if s.get("random") else ()
                self.tree.insert("", "end", iid=str(i), tags=tags,
                                 values=(s["title"], s["artist"], s["bpm"],
                                         s.get("mode","?"), s.get("notes","")))
            if end < len(capped):
                self.after(1, insert_batch, end)   # yield to the UI, then continue
            else:
                shown = len(capped)
                extra = (f"  (showing first {shown:,} of {len(rows):,} — "
                         f"narrow your search to see the rest)"
                         if len(rows) > shown else "")
                self.log(f"Showing {shown:,} of {len(self.songs):,} charts.{extra}")

        if capped:
            insert_batch(0)
        else:
            self.log(f"Showing 0 of {len(self.songs):,} charts.")

    def sort_library(self, col):
        # Toggle ascending/descending when clicking the same header again.
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, False
        key_map = {"title": lambda s: s["title"].lower(),
                   "artist": lambda s: s["artist"].lower(),
                   "mode": lambda s: s.get("mode",""),
                   "bpm": lambda s: _num(s.get("bpm")),
                   "notes": lambda s: _num(s.get("notes"))}
        keyf = key_map.get(col, lambda s: s["title"].lower())
        self.filtered.sort(key=keyf, reverse=self._sort_desc)
        # reflect arrow in header text
        for c in ("title","artist","bpm","mode","notes"):
            arrow = ""
            if c == col:
                arrow = " ▼" if self._sort_desc else " ▲"
            self.tree.heading(c, text=c.title() + arrow)
        self._populate_tree(self.filtered)

    def _default_tags(self, s):
        # Album is ALWAYS "BMS". Genre comes from the chart; album never does.
        return {"Title": s["title"], "Artist": s["artist"], "Album": "BMS",
                "Genre": s.get("genre", ""), "BPM": str(s.get("bpm", ""))}

    def _make_queue_item(self, s):
        """Build a queue item from a song dict. If this song is the one currently
        shown in the art picker, carry that previewed image over as its cover so a
        pick made before queueing isn't lost."""
        item = {"path": s["path"], "title": s["title"], "artist": s["artist"],
                "mode": s.get("mode", "?"), "notes": s.get("notes", ""),
                "tags": self._default_tags(s)}
        # carry over the previewed art if the picker is currently showing this song
        if (getattr(self, "_song_art_folder", None) == os.path.dirname(s["path"])
                and self._song_art_files
                and 0 <= self._song_art_idx < len(self._song_art_files)):
            item["art"] = self._song_art_files[self._song_art_idx]
        return item

    def on_library_select(self, _):
        sel = self.tree.selection()
        if not sel:
            return
        s = self._lib_song_at(sel[0])
        if not s:
            return
        self.selected_kind = "library"; self.selected_index = None
        self._update_info(s)
        self._show_tags(self._default_tags(s), editable=False,
                        hint="Library preview (read-only) — add to Queue to edit tags.")
        self._load_song_art_for(s["path"], None)   # read-only preview

    def on_library_activate(self, _):
        # double-click → play
        sel = self.tree.selection()
        if not sel or not _SD_OK:
            return
        s = self._lib_song_at(sel[0])
        if not s:
            return
        try:
            idx = self.filtered.index(s)
        except ValueError:
            idx = 0
        self._start_song("library", idx, s["path"], s["title"])

    def add_to_queue(self):
        if not self.tree.selection():
            messagebox.showinfo("Pick a song", "Select one or more songs in the Library first.")
            return
        self._ctx_song = None
        self._ctx_add_queue()   # shared path: queues every selected row, de-duped

    # --------------------------------------------------------------- queue tab
    def refresh_queue(self):
        self.qtree.delete(*self.qtree.get_children())
        for i, q in enumerate(self.queue):
            self.qtree.insert("", "end", iid=str(i),
                              values=(q["tags"]["Title"], q["tags"]["Artist"],
                                      q["mode"], q.get("notes","")))
        idx = self.nb.index(self.q_tab)
        self.nb.tab(idx, text=f"Queue ({len(self.queue)})")

    def on_queue_select(self, _):
        sel = self.qtree.selection()
        if not sel:
            return
        i = int(sel[0]); q = self.queue[i]
        self.selected_kind = "queue"; self.selected_index = i
        # look up the full song record (genre/bpm/md5) by path for complete info
        full = next((s for s in self.songs if s["path"] == q["path"]), None) or q
        self._update_info(full)
        self._show_tags(q["tags"], editable=True,
                        hint="Double-click to play. Edits are saved automatically.")
        self._load_song_art_for(q["path"], q)   # editable picker for the queued song

    def on_queue_activate(self, _):
        # double-click → play
        sel = self.qtree.selection()
        if not sel or not _SD_OK:
            return
        i = int(sel[0]); q = self.queue[i]
        self._start_song("queue", i, q["path"], q["tags"]["Title"])

    def remove_from_queue(self):
        sel = self.qtree.selection()
        if not sel:
            return
        i = int(sel[0]); removed = self.queue.pop(i)
        self.refresh_queue()
        self.log(f"Removed from queue: {removed['tags']['Title']}")
        self._clear_tags()

    def clear_queue(self):
        if self.queue and messagebox.askyesno("Clear queue", "Remove all songs from the queue?"):
            self.queue.clear(); self.refresh_queue(); self._clear_tags()

    # ----------------------------------------------------------- shared tags
    def _show_tags(self, tags, editable=True, hint=""):
        for k, e in self.fields.items():
            e.config(state="normal")                 # writable so we can set the text
            e.delete(0, "end"); e.insert(0, tags.get(k, ""))
            # Only queued songs persist edits; elsewhere the fields are read-only
            # (greyed) so it's clear editing here won't save.
            e.config(state=("normal" if editable else "readonly"))
        self.tag_hint.config(text=hint)

    def _update_info(self, song):
        """Fill the BMS information panel from a song dict (read-only)."""
        if not song:
            self._clear_info(); return
        vals = {
            "Title": song.get("title", "—") or "—",
            "Artist": song.get("artist", "—") or "—",
            "Genre": song.get("genre", "—") or "—",
            "BPM": str(song.get("bpm", "") or "—"),
            "Notes": str(song.get("notes", "") if song.get("notes", "") != "" else "—"),
            "Play type": song.get("mode", "—") or "—",
            "File": song.get("path", "—") or "—",
            "MD5": song.get("md5", "—") or "—",
        }
        for k, ent in self.info_labels.items():
            self._set_path(ent, vals.get(k, "—"))

    def _clear_info(self):
        for ent in self.info_labels.values():
            self._set_path(ent, "—")

    def _clear_tags(self):
        for e in self.fields.values():
            e.config(state="normal")
            e.delete(0, "end")
        self.selected_kind = None; self.selected_index = None
        self._clear_info()
        self.tag_hint.config(text="Select a song to edit its tags.")
        self._load_song_art_for(None)   # picker is queue-only

    def on_tag_edit(self, _):
        # live-save edits back to the selected queue item
        if self.selected_kind == "queue" and self.selected_index is not None:
            i = self.selected_index
            q = self.queue[i]
            for k, e in self.fields.items():
                q["tags"][k] = e.get()
            # update just this one row's display (no full rebuild)
            try:
                self.qtree.item(str(i), values=(q["tags"]["Title"],
                                                q["tags"]["Artist"], q["mode"],
                                                q.get("notes","")))
            except tk.TclError:
                pass

    # --------------------------------------------------------------- rendering
    def render_all(self):
        if not self.queue:
            messagebox.showinfo("Queue empty", "Add songs to the queue first."); return
        out_dir = load_config().get("output")
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showinfo("No output folder", "Choose an output folder first."); return
        self.render_btn.config(state="disabled")
        fmt = self.fmt_pick.get().upper()
        threading.Thread(target=self._render_queue, args=(out_dir, fmt), daemon=True).start()

    def _safe_filename(self, name):
        bad = '<>:"/\\|?*'
        cleaned = "".join("_" if c in bad else c for c in name).strip()
        return cleaned or "untitled"

    # ----------------------------------------------------------- playback
    def _start_song(self, kind, index, path, title):
        """Stop whatever's playing and start this song (render first if needed)."""
        if not _SD_OK:
            return
        self.player.stop()
        self._playing_ctx = (kind, index, path)
        self._update_now_playing(title)
        # instant if we've rendered it before this session
        cached = self._cache_get(path)
        if cached is not None:
            self._begin_playback(cached)
            self._prefetch_neighbors(kind, index)
            return
        self._rendering_play = True
        self._refresh_play_btn()
        self.log(f"Rendering for playback: {title} …")
        threading.Thread(target=self._render_for_play,
                         args=(path, self._playing_ctx), daemon=True).start()

    def _cache_get(self, path):
        """Fetch a rendered buffer and mark it most-recently-used."""
        if path in self._render_cache:
            try:
                self._render_cache_order.remove(path)
            except ValueError:
                pass
            self._render_cache_order.append(path)
            return self._render_cache[path]
        return None

    def _cache_put(self, path, audio):
        """Store a rendered buffer, evicting least-recently-used beyond the cap."""
        self._render_cache[path] = audio
        try:
            self._render_cache_order.remove(path)
        except ValueError:
            pass
        self._render_cache_order.append(path)
        while len(self._render_cache_order) > self._render_cache_max:
            old = self._render_cache_order.pop(0)
            self._render_cache.pop(old, None)

    def _prefetch_neighbors(self, kind, index):
        """Pre-render the previous and next song (queue/playlist/table) so advancing
        is gapless. Bounded by the LRU cache; runs quietly in the background."""
        if kind not in ("queue", "playlist", "table") or index is None or index < 0:
            return
        neighbors = []
        if kind == "queue":
            for j in (index - 1, index + 1):
                if 0 <= j < len(self.queue):
                    neighbors.append(self.queue[j]["path"])
        elif kind == "playlist":
            for j in (index - 1, index + 1):
                if 0 <= j < len(self._pl_rows):
                    s = self._pl_rows[j]
                    if not s.get("_missing"):
                        neighbors.append(s["path"])
        else:  # table
            order = getattr(self, "_tbl_play_order", [])
            for j in (index - 1, index + 1):
                if 0 <= j < len(order):
                    neighbors.append(order[j]["path"])
        for p in neighbors:
            if p in self._render_cache or p in self._prefetching:
                continue
            self._prefetching.add(p)
            threading.Thread(target=self._prefetch_one, args=(p,), daemon=True).start()

    def _prefetch_one(self, path):
        try:
            audio, _ = render_bms(path)
        except Exception:
            audio = None
        def done():
            self._prefetching.discard(path)
            if audio is not None and path not in self._render_cache:
                self._cache_put(path, audio)
        self.after(0, done)

    def _render_for_play(self, path, ctx):
        try:
            audio, _ = render_bms(path, log=self.log)
        except Exception:
            self.log("Playback render failed:\n" + traceback.format_exc())
            def fail():
                self._rendering_play = False; self._refresh_play_btn()
            self.after(0, fail)
            return
        def finish():
            self._rendering_play = False
            if self._playing_ctx == ctx:
                self._cache_put(path, audio)
                self._begin_playback(audio)
                self._prefetch_neighbors(ctx[0], ctx[1])
            else:
                self._refresh_play_btn()
        self.after(0, finish)

    def _begin_playback(self, audio):
        self.player.load(audio)
        self.player.play()
        self._refresh_play_btn()

    def _refresh_play_btn(self):
        """Single source of truth for the play button's symbol."""
        if not _SD_OK:
            return
        if self._rendering_play:
            txt = "…"
        elif self.player is not None and self.player.state == "playing":
            txt = "⏸"
        else:
            txt = "▶"
        self.play_btn.config(text=txt)

    def toggle_play(self):
        if not _SD_OK or self.player is None:
            return
        # Nothing loaded yet → start whatever's selected on the current tab.
        if self.player.state == "stopped" and self._playing_ctx is None:
            self._play_selected()
            return
        self.player.toggle()
        self._refresh_play_btn()

    def _play_selected(self):
        """Start playback of the currently selected row on the active tab."""
        try:
            cur = self.nb.index(self.nb.select())
        except tk.TclError:
            return
        if cur == self.nb.index(self.q_tab):
            sel = self.qtree.selection()
            if sel:
                i = int(sel[0]); q = self.queue[i]
                self._start_song("queue", i, q["path"], q["tags"]["Title"]); return
        elif cur == self.nb.index(self.p_tab):
            s = self._pl_selected_song()
            if s:
                sel = self.pltree.selection()
                self._start_song("playlist", int(sel[0]), s["path"], s["title"]); return
        elif cur == self.nb.index(self.t_tab):
            s = self._tbl_selected_song()
            if s:
                self._tbl_start(s); return
            self.log("Select an owned chart in the table first (greyed = you don't have it)."); return
        else:  # Library
            sel = self.tree.selection()
            if sel:
                s = self._lib_song_at(sel[0])
                if s:
                    try:
                        idx = self.filtered.index(s)
                    except ValueError:
                        idx = 0
                    self._start_song("library", idx, s["path"], s["title"]); return
        self.log("Select a song first, then press Play (or double-click a song).")

    def stop_play(self):
        if not _SD_OK or self.player is None:
            return
        self.player.stop()
        self._rendering_play = False
        self._refresh_play_btn()
        self.seek.set(0)
        self.time_lbl.config(text="0:00 / 0:00")
        self._playing_ctx = None
        self._update_now_playing(None)

    # seek: drag freely with no sound, jump on release
    def _seek_grab(self, _):
        self._seeking = True

    def _seek_release(self, _):
        if self.player is None or self.player.duration_seconds() <= 0:
            self._seeking = False
            return
        frac = self.seek.get() / 1000.0
        self.player.seek_seconds(frac * self.player.duration_seconds())
        self._seeking = False

    def _on_volume(self, val):
        if self.player is not None:
            try:
                self.player.set_volume(float(val) / 100.0)
            except (ValueError, TypeError):
                pass

    @staticmethod
    def _fmt(sec):
        sec = int(sec); return f"{sec//60}:{sec%60:02d}"

    def _tick(self):
        # Runs ~10x/sec. Only touches widgets while a song is actively PLAYING;
        # when idle/paused it does nothing, so it can't compete with text-field
        # input (which previously made the caret feel sluggish). Still polls for
        # song-end so auto-advance keeps working.
        p = self.player
        if p is not None and p.state == "playing":
            try:
                if self.state() != "iconic" and not self._seeking:
                    dur = p.duration_seconds(); pos = p.position_seconds()
                    if dur > 0:
                        self.seek.set(int(pos / dur * 1000))
                        self.time_lbl.config(text=f"{self._fmt(pos)} / {self._fmt(dur)}")
            except tk.TclError:
                pass
            if p.poll_finished():
                self._on_song_end()
        self.after(120, self._tick)

    def _next_index(self, cur, count, is_playable=None):
        """Pick the next index given Shuffle. `is_playable(i)` lets a list (playlists)
        skip missing entries. Returns an index, or None at the end of the list.
        (Loop is handled separately as repeat-current, so there's no wrap here.)"""
        if count <= 0:
            return None
        playable = [i for i in range(count) if (is_playable is None or is_playable(i))]
        if not playable:
            return None
        if self.shuffle_on.get():
            import random
            choices = [i for i in playable if i != cur] or playable
            return random.choice(choices)
        for i in range(cur + 1, count):
            if is_playable is None or is_playable(i):
                return i
        return None

    def _on_song_end(self):
        self._refresh_play_btn()
        ctx = self._playing_ctx
        if not ctx:
            self.stop_play(); return
        # Loop = repeat the current song (works in every context, Library included)
        if self.loop_on.get():
            cached = self._cache_get(ctx[2])
            if cached is not None:
                self._begin_playback(cached)
                return
        if not self._advance(+1):
            self.stop_play()

    def next_track(self):
        """Skip to the next song (Next button). Respects Shuffle; ignores Loop."""
        if self._playing_ctx and not self._advance(+1):
            self.stop_play()

    def prev_track(self):
        """Go to the previous song (Prev button)."""
        if self._playing_ctx:
            self._advance(-1)

    def _advance(self, direction):
        """Move to the next (+1) or previous (-1) song within the current list
        context. Returns True if a song was started, False if there's nowhere to go."""
        ctx = self._playing_ctx
        if not ctx:
            return False
        kind, index = ctx[0], ctx[1]
        if kind == "queue":
            nxt = self._step_index(index, len(self.queue), direction)
            if nxt is not None:
                q = self.queue[nxt]
                self._select_row(self.qtree, nxt)
                self._start_song("queue", nxt, q["path"], q["tags"]["Title"])
                return True
        elif kind == "playlist":
            rows = self._pl_rows
            nxt = self._step_index(index, len(rows), direction,
                                   is_playable=lambda i: not rows[i].get("_missing"))
            if nxt is not None:
                s = rows[nxt]
                self._select_row(self.pltree, nxt)
                self._start_song("playlist", nxt, s["path"], s["title"])
                return True
        elif kind == "table":
            order = getattr(self, "_tbl_play_order", [])
            nxt = self._step_index(index, len(order), direction)
            if nxt is not None:
                s = order[nxt]
                self._tbl_select_by_index(nxt)
                self._start_song("table", nxt, s["path"], s["title"])
                return True
        return False

    def _step_index(self, cur, count, direction, is_playable=None):
        """Forward/back step. Forward respects Shuffle (random next); backward is
        always the previous playable song. Returns index or None at a boundary."""
        if direction > 0:
            return self._next_index(cur, count, is_playable)
        # backward: previous playable index
        for i in range(cur - 1, -1, -1):
            if is_playable is None or is_playable(i):
                return i
        return None

    def _select_row(self, tree, i):
        try:
            tree.selection_set(str(i)); tree.see(str(i))
        except tk.TclError:
            pass

    def _tbl_select_by_index(self, play_idx):
        """Select + scroll-to the table row matching the given play-order index."""
        order = getattr(self, "_tbl_play_order", [])
        if not (0 <= play_idx < len(order)):
            return
        target = order[play_idx]["path"]
        for iid, row in getattr(self, "_row_by_iid", {}).items():
            s = row.get("song")
            if s and s["path"] == target:
                try:
                    self.ttree.selection_set(iid); self.ttree.see(iid)
                except tk.TclError:
                    pass
                return

    def _update_now_playing(self, title):
        """Show the [♪] marker. If the title is too long for the label, scroll it."""
        self._now_full = (f"[♪] {title}" if title else "")
        self._now_scroll_pos = 0
        self._mark_now_row()
        self._render_now_label()

    _NOW_WIDTH = 22   # visible characters in the now-playing label

    def _render_now_label(self):
        text = getattr(self, "_now_full", "")
        if len(text) <= self._NOW_WIDTH:
            self.now_lbl.config(text=text)   # fits — no scrolling
            return
        # marquee: show a window into "text + gap + text" that advances each tick
        pad = text + "    "
        pos = self._now_scroll_pos % len(pad)
        window = (pad + pad)[pos:pos + self._NOW_WIDTH]
        self.now_lbl.config(text=window)

    def _scroll_now_label(self):
        # advance the marquee ~3x/sec, only while something is playing and long
        if getattr(self, "_now_full", "") and len(self._now_full) > self._NOW_WIDTH:
            self._now_scroll_pos += 1
            self._render_now_label()
        self.after(350, self._scroll_now_label)

    def _mark_now_row(self):
        # library: prefix the title cell of the playing row with ♪, clear others
        ctx = self._playing_ctx
        play_lib = ctx[1] if (ctx and ctx[0] == "library") else None
        for iid in self.tree.get_children():
            if not iid.isdigit():
                continue   # songs-only group rows ("gN") -- no marking there
            s = self.filtered[int(iid)] if int(iid) < len(self.filtered) else None
            if s is None:
                continue
            base = s["title"]
            self.tree.set(iid, "title", ("[♪] " + base) if (play_lib is not None and int(iid) == play_lib) else base)
        play_q = ctx[1] if (ctx and ctx[0] == "queue") else None
        for iid in self.qtree.get_children():
            i = int(iid)
            if i >= len(self.queue):
                continue
            base = self.queue[i]["tags"]["Title"]
            self.qtree.set(iid, "title", ("[♪] " + base) if (play_q is not None and i == play_q) else base)
        # tables: the table uses index -1, so match the now-playing row by path
        if hasattr(self, "ttree") and hasattr(self, "_row_by_iid"):
            play_path = ctx[2] if ctx else None
            for iid, row in self._row_by_iid.items():
                song = row.get("song")
                if not song:
                    continue
                base = (song.get("title") or row["entry"].get("title") or "")
                mark = play_path is not None and song.get("path") == play_path
                try:
                    self.ttree.set(iid, "title", ("[♪] " + base) if mark else base)
                except tk.TclError:
                    pass

    def _render_queue(self, out_dir, fmt="FLAC"):
        # Render the live queue, removing each item as it finishes.
        self._render_items(list(self.queue), out_dir, fmt, self.render_btn,
                           remove_cb=self._remove_queue_item, label="Queue")

    def _render_items(self, items, out_dir, fmt, button, remove_cb=None, label="Playlist"):
        """Render a list of {path, tags, title} items to a folder, in parallel across
        worker processes (count from the render-threads setting)."""
        from concurrent.futures import ProcessPoolExecutor, as_completed
        ext = {"FLAC": ".flac", "WAV": ".wav", "OGG": ".ogg"}.get(fmt, ".flac")
        ff = ffmpeg_path() if fmt in ("OGG", "FLAC") else None
        if fmt == "OGG" and not ff:
            self.log("OGG selected but ffmpeg isn't on PATH — aborting. Install ffmpeg "
                     "or pick FLAC/WAV.")
            self.after(0, lambda: button.config(state="normal"))
            return
        if fmt == "FLAC":
            self.log("FLAC compression: level 8 (ffmpeg)" if ff
                     else "FLAC compression: level 5 (default; install ffmpeg for level 8)")
        try:
            # Global 'whole queue' art takes priority. If it's set, every song uses
            # it. If not, each song falls back to its own per-song folder pick (the
            # 'art' key on the queue item).
            global_cover = None
            art_path = self.art_path
            if art_path and os.path.isfile(art_path) and _PIL_OK:
                try:
                    global_cover, csize, cq = process_cover(art_path)
                    self.log(f"Album art ready ({csize[0]}x{csize[1]}, {len(global_cover)//1024} KB).")
                except Exception as e:
                    self.log(f"Album art could not be processed, rendering without it: {e}")
                    global_cover = None

            def cover_for(item):
                if global_cover is not None:
                    return global_cover
                p = item.get("art")
                if p and os.path.isfile(p) and _PIL_OK:
                    try:
                        data, _sz, _q = process_cover(p)
                        return data
                    except Exception:
                        return None
                return None

            # build one job per item, mapping job→item so we can remove from queue
            jobs = {}
            for it in items:
                tags = it["tags"]
                out_path = os.path.join(out_dir,
                                        self._safe_filename(tags.get("Title","untitled")) + ext)
                jobs[(it["path"], out_path, fmt)] = it

            workers = self._render_worker_count()
            total = len(jobs); done = 0
            self.log(f"Rendering {total} song(s) with {workers} worker process(es)…")
            # submit all jobs
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futs = {}
                for (p, o, f), it in jobs.items():
                    job = (p, o, f, it["tags"], cover_for(it), ff, bms_core._LIBRARY_ROOT)
                    futs[ex.submit(render_one_job, job)] = it
                for fut in as_completed(futs):
                    it = futs[fut]
                    done += 1
                    try:
                        out_path, title, err = fut.result()
                    except Exception:
                        self.log(f"  [{done}/{total}] worker crashed: {it['tags'].get('Title')}")
                        continue
                    if err:
                        self.log(f"  [{done}/{total}] FAILED: {title}\n{err}")
                    else:
                        self.log(f"  [{done}/{total}] done -> {out_path}")
                        if remove_cb:
                            self.after(0, remove_cb, it)
            self.log(f"{label} render complete.")
        finally:
            self.after(0, lambda: button.config(state="normal"))

    def _render_worker_count(self):
        """How many worker processes to render with. From the spinbox if it exists,
        else config, else CPU count."""
        cpu = os.cpu_count() or 2
        v = 0
        if hasattr(self, "threads_var"):
            try: v = int(self.threads_var.get())
            except (ValueError, TypeError): v = 0
        if v <= 0:
            try: v = int(load_config().get("render_threads", 0))
            except (ValueError, TypeError): v = 0
        if v <= 0:
            return cpu
        return max(1, min(v, cpu * 2))   # clamp to something sane

    def _on_threads_change(self):
        cfg = load_config()
        try:
            cfg["render_threads"] = int(self.threads_var.get())
            save_config(cfg)
        except (ValueError, TypeError):
            pass

    def _on_close(self):
        # remember the window size/position for next launch
        try:
            if self.state() == "normal":   # don't save a maximized/minimized geometry
                cfg = load_config()
                cfg["window_geometry"] = self.geometry()
                save_config(cfg)
        except Exception:
            pass
        self.destroy()

    def _remove_queue_item(self, q):
        # MAIN thread only.
        if q in self.queue:
            self.queue.remove(q)
            self.refresh_queue()

if __name__ == "__main__":
    import multiprocessing
    # MUST be first: in a frozen (.exe) build, spawned worker processes re-launch
    # the executable; freeze_support() intercepts that so they run the worker
    # function instead of opening a second GUI window.
    multiprocessing.freeze_support()
    App().mainloop()

