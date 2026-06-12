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

import os, sys, time, threading, traceback

# ---- Console-hide relaunch (must run BEFORE the heavy imports below) ----
# Running "python bms_renderer.py" on Windows keeps a console window open behind
# the GUI. We relaunch ourselves once under pythonw.exe (no console). Doing this
# at the very top — before importing numpy/scipy/PIL via bms_core — means the
# throwaway first process exits immediately instead of paying the full import cost
# first, so the real window appears much faster. The _BMS_NOCONSOLE env guard
# prevents an infinite relaunch loop.
if (__name__ == "__main__" and sys.platform == "win32"
        and not getattr(sys, "frozen", False)
        and os.environ.get("_BMS_NOCONSOLE") != "1"
        and "--multiprocessing-fork" not in sys.argv):   # don't disturb worker spawns
    _pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.isfile(_pyw):
        try:
            import subprocess
            subprocess.Popen([_pyw, os.path.abspath(__file__)] + sys.argv[1:],
                             env=dict(os.environ, _BMS_NOCONSOLE="1"), close_fds=True)
            sys.exit(0)
        except Exception:
            pass   # fall through and run normally if relaunch fails

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
    render_bga_video_job, detect_bga,
    pick_playable_chart, pick_discovery_art,
    load_tables_file, save_tables_file, fetch_table,
    load_playlists, save_one_playlist, delete_playlist_file,
    _migrate_old_cache, _PIL_OK, _num,
    Player, _SD_OK,
)

_BLACK_COVER = None
def _black_cover_bytes():
    """JPEG bytes for a 1000x1000 black square, generated once and cached. Used as
    a clean 'no real art' cover without writing any file to disk."""
    global _BLACK_COVER
    if _BLACK_COVER is None and _PIL_OK:
        from PIL import Image
        import io as _io
        buf = _io.BytesIO()
        Image.new("RGB", (1000, 1000), (0, 0, 0)).save(buf, format="JPEG", quality=85)
        _BLACK_COVER = buf.getvalue()
    return _BLACK_COVER

# ---------------------------------------------------------------- album grid
class AlbumGrid:
    """A reusable virtualized art-grid (the Discovery view, generalized so it can
    also back the album view in Tables / Playlists / Queue). Only on-screen tiles
    exist as widgets; they are recycled while scrolling. The selected tile gets a
    highlighted border so the current song is obvious."""
    TILE = 170
    PAD = 14
    TILE_H = 170 + 56
    ROW_H = 170 + 76
    SEL_COLOR = "#2d7dff"

    def __init__(self, app, parent, get_items, on_click, on_double, on_menu,
                 get_sections=None):
        self.app = app
        self.get_items = get_items
        self.on_click = on_click
        self.on_double = on_double
        self.on_menu = on_menu
        # get_sections() -> dict {item_index: "label"} marking where a labelled
        # section (e.g. a table level) begins. Optional.
        self.get_sections = get_sections
        self.canvas = tk.Canvas(parent, highlightthickness=0, bg="#f0f0f0")
        self.vsb = ttk.Scrollbar(parent, orient="vertical", command=self._yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._relayout())
        self.canvas.bind("<Enter>", lambda e: setattr(self, "_hot", True))
        self.canvas.bind("<Leave>", lambda e: setattr(self, "_hot", False))
        self._hot = False
        self._pool = []
        self._thumbs = {}
        self._thumb_order = []
        self._pending = set()
        self._cols = 1
        self._scroll = 0
        self._token = 0
        self._selected = None
        self._ph = None
        self._collapsed = set()      # section labels currently collapsed

    def refresh(self, reset_scroll=True):
        self._token += 1
        self._thumbs.clear(); self._thumb_order.clear(); self._pending.clear()
        self._layout_key = None        # force layout recompute (items/sections changed)
        for t in self._pool:
            t["idx"] = None
        if reset_scroll:
            self._scroll = 0
            self._selected = None
        self.app.after_idle(lambda: self._relayout(rebuild_pool=True))

    def set_selected(self, idx):
        self._selected = idx
        self._redraw()

    def on_mousewheel(self, event):
        if not self._hot:
            return
        self._scroll -= (event.delta // 120) * 90
        self._clamp(); self._redraw()

    def _items(self):
        return self.get_items() or []

    SECTION_H = 34   # height of a section header band

    def _layout(self):
        """Compute (x, y) for every item plus header bands, accounting for section
        breaks and collapsed sections. Cached per (cols, items, sections, collapsed)."""
        items = self._items()
        cols = self._cols
        sections = (self.get_sections() or {}) if self.get_sections else {}
        collapsed = getattr(self, "_collapsed", set())
        key = (cols, len(items), tuple(sorted(sections.items())),
               tuple(sorted(collapsed)))
        if getattr(self, "_layout_key", None) == key and getattr(self, "_layout_cache", None):
            return self._layout_cache
        # map each item index to the label of the section it belongs to
        starts = sorted(sections.items())            # [(start_idx, label), ...]
        positions = [None] * len(items)              # None ⇒ hidden (collapsed)
        headers = []            # (y, label, collapsed_bool)
        y = self.PAD
        col = 0
        cur_label = None
        cur_collapsed = False
        for i in range(len(items)):
            if i in sections:
                if col != 0:
                    y += self.ROW_H; col = 0
                cur_label = sections[i]
                cur_collapsed = cur_label in collapsed
                headers.append((y, cur_label, cur_collapsed))
                y += self.SECTION_H
            if cur_collapsed:
                positions[i] = None                  # item hidden under collapsed header
                continue
            x = self.PAD + col * (self.TILE + self.PAD * 2)
            positions[i] = (x, y)
            col += 1
            if col >= cols:
                col = 0; y += self.ROW_H
        total = y + (self.ROW_H if col != 0 else 0) + self.PAD
        self._layout_key = key
        self._layout_cache = (positions, headers, total)
        return self._layout_cache

    def toggle_section(self, label):
        if not hasattr(self, "_collapsed"):
            self._collapsed = set()
        if label in self._collapsed:
            self._collapsed.discard(label)
        else:
            self._collapsed.add(label)
        self._layout_key = None
        self._clamp(); self._redraw()

    def _content_h(self):
        if not self._items():
            return 0
        return self._layout()[2]

    def _clamp(self):
        ch = self.canvas.winfo_height()
        self._scroll = max(0, min(self._scroll, max(0, self._content_h() - ch)))

    def _relayout(self, rebuild_pool=False):
        cw = self.canvas.winfo_width(); ch = self.canvas.winfo_height()
        if cw <= 1 or ch <= 1 or not self._items():
            return
        self._cols = max(1, cw // (self.TILE + self.PAD * 2))
        need = (ch // self.ROW_H + 3) * self._cols
        if rebuild_pool or need > len(self._pool):
            for _ in range(len(self._pool), need):
                self._pool.append(self._new_tile())
        self._clamp(); self._redraw()

    def _new_tile(self):
        tile = ttk.Frame(self.canvas, width=self.TILE, height=self.TILE_H)
        tile.grid_propagate(False)
        tile.grid_columnconfigure(0, weight=1)
        art = tk.Label(tile, bg="#000000", bd=1, relief="solid")
        art.grid(row=0, column=0)
        t_lbl = tk.Label(tile, anchor="center", width=1)
        t_lbl.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        a_lbl = tk.Label(tile, anchor="center", fg="#777", width=1)
        a_lbl.grid(row=2, column=0, sticky="ew")
        t = {"frame": tile, "art": art, "title": t_lbl, "artist": a_lbl,
             "idx": None, "win": None, "_full": ""}
        for w in (tile, art, t_lbl, a_lbl):
            w.bind("<Button-1>", lambda e, t=t: self._click(t))
            w.bind("<Double-Button-1>", lambda e, t=t: self._double(t))
            w.bind("<Button-3>", lambda e, t=t: self._menu(e, t))
            w.bind("<Enter>", lambda e, t=t: self._hover(t, True))
            w.bind("<Leave>", lambda e, t=t: self._hover(t, False))
        return t

    def _redraw(self):
        items = self._items()
        ch = self.canvas.winfo_height()
        if not items:
            self.canvas.delete("section")
            self._update_sb(); return
        positions, headers, _total = self._layout()
        # draw header bands; each is clickable to collapse/expand its section
        self.canvas.delete("section")
        for hy, label, is_collapsed in headers:
            yy = hy - self._scroll
            if yy > ch or yy + self.SECTION_H < 0:
                continue
            arrow = "\u25b8 " if is_collapsed else "\u25be "   # ▸ collapsed / ▾ expanded
            tid = self.canvas.create_text(
                self.PAD, yy + self.SECTION_H // 2, anchor="w",
                text=arrow + label, fill="#333",
                font=("", 11, "bold"), tags=("section", "sechdr"))
            # bind the click on this specific header text to toggle its section
            self.canvas.tag_bind(tid, "<Button-1>",
                                 lambda e, lb=label: self.toggle_section(lb))
            self.canvas.create_line(self.PAD, yy + self.SECTION_H - 2,
                                    self.canvas.winfo_width() - self.PAD,
                                    yy + self.SECTION_H - 2,
                                    fill="#ccc", tags="section")
        used = set(); slot = 0
        for i, pos in enumerate(positions):
            if pos is None:               # item is under a collapsed section
                continue
            x, ay = pos
            y = ay - self._scroll
            if y + self.ROW_H < 0:
                continue
            if y > ch:
                break
            if slot >= len(self._pool):
                break
            t = self._pool[slot]
            self._fill(t, i)
            t["art"].config(highlightthickness=(3 if i == self._selected else 0),
                            highlightbackground=self.SEL_COLOR,
                            highlightcolor=self.SEL_COLOR)
            if t["win"] is None:
                t["win"] = self.canvas.create_window(x, y, anchor="nw", window=t["frame"])
            else:
                self.canvas.coords(t["win"], x, y)
                self.canvas.itemconfigure(t["win"], state="normal")
            used.add(slot); slot += 1
        for s in range(len(self._pool)):
            if s not in used and self._pool[s]["win"] is not None:
                self.canvas.itemconfigure(self._pool[s]["win"], state="hidden")
        self._update_sb()

    def _fill(self, t, idx):
        if t["idx"] == idx:
            return
        t["idx"] = idx
        rep, charts = self._items()[idx]
        full = rep["title"] or "(unknown)"
        self._marquee_stop(t["title"], full, None)
        t["title"].config(text=self._ell(full, 22))
        t["_full"] = full
        t["artist"].config(text=self._ell(rep.get("artist") or "", 24))
        img = self._thumbs.get(idx)
        if img is not None:
            t["art"].config(image=img); self._touch(idx)
        else:
            t["art"].config(image=self._placeholder()); self._request(idx)

    def _resolve(self, t):
        idx = t["idx"]
        if idx is None or idx >= len(self._items()):
            return None
        return idx

    def _click(self, t):
        idx = self._resolve(t)
        if idx is None: return
        self.set_selected(idx)
        rep, charts = self._items()[idx]
        self.on_click(idx, rep, charts)

    def _double(self, t):
        idx = self._resolve(t)
        if idx is None: return
        rep, charts = self._items()[idx]
        self.on_double(idx, rep, charts)

    def _menu(self, event, t):
        idx = self._resolve(t)
        if idx is None: return
        self.set_selected(idx)
        rep, charts = self._items()[idx]
        self.on_menu(event, idx, rep, charts)

    def _hover(self, t, entering):
        full = t.get("_full", "")
        if len(full) <= 22:
            return
        if entering:
            pend = t.get("_hstop")
            if pend:
                try: self.app.after_cancel(pend)
                except Exception: pass
                t["_hstop"] = None
            if not getattr(t["title"], "_mjob", None):
                self._marquee_start(t["title"], full, 22)
        else:
            def stop():
                t["_hstop"] = None
                self._marquee_stop(t["title"], full, 22)
            t["_hstop"] = self.app.after(60, stop)

    @staticmethod
    def _ell(s, n):
        return s if len(s) <= n else s[:n-1] + "\u2026"

    def _marquee_start(self, label, full, n):
        self._marquee_stop(label, full, None)
        pad = full + "    "
        def step(pos=0):
            label.config(text=(pad[pos:] + pad[:pos])[:n])
            label._mjob = self.app.after(180, step, (pos + 1) % len(pad))
        step()

    def _marquee_stop(self, label, full, n):
        job = getattr(label, "_mjob", None)
        if job:
            try: self.app.after_cancel(job)
            except Exception: pass
            label._mjob = None
        if n is not None:
            label.config(text=self._ell(full, n))

    def _placeholder(self):
        if self._ph is not None:
            return self._ph
        try:
            from PIL import Image
            import io as _io, base64 as _b64
            img = Image.new("RGB", (self.TILE, self.TILE), (0, 0, 0))
            buf = _io.BytesIO(); img.save(buf, format="PNG")
            self._ph = tk.PhotoImage(data=_b64.b64encode(buf.getvalue()).decode("ascii"))
        except Exception:
            self._ph = None
        return self._ph

    def _request(self, idx):
        if idx in self._pending or idx in self._thumbs:
            return
        self._pending.add(idx)
        token = self._token
        threading.Thread(target=self._decode, args=(token, idx), daemon=True).start()

    def _decode(self, token, idx):
        try:
            from PIL import Image
            import io as _io, base64 as _b64
        except Exception:
            return
        if token != self._token:
            return
        items = self._items()
        if idx >= len(items):
            return
        rep, charts = items[idx]
        data = None
        try:
            art = pick_discovery_art(rep["path"])
            if art:
                img = Image.open(art).convert("RGB")
                img.thumbnail((self.TILE - 2, self.TILE - 2), Image.LANCZOS)
                cv = Image.new("RGB", (self.TILE, self.TILE), (0, 0, 0))
                cv.paste(img, ((self.TILE - img.width) // 2, (self.TILE - img.height) // 2))
                buf = _io.BytesIO(); cv.save(buf, format="PNG")
                data = _b64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            data = None
        self.app.after(0, self._thumb_ready, token, idx, data)

    def _thumb_ready(self, token, idx, data):
        self._pending.discard(idx)
        if token != self._token or data is None:
            return
        try:
            img = tk.PhotoImage(data=data)
        except tk.TclError:
            return
        self._thumbs[idx] = img
        self._thumb_order.append(idx)
        while len(self._thumb_order) > 600:
            old = self._thumb_order.pop(0)
            if old != idx:
                self._thumbs.pop(old, None)
        for t in self._pool:
            if t["idx"] == idx and t["win"] is not None:
                t["art"].config(image=img)
                break

    def _touch(self, idx):
        try:
            self._thumb_order.remove(idx); self._thumb_order.append(idx)
        except ValueError:
            pass

    def _update_sb(self):
        total = self._content_h(); ch = self.canvas.winfo_height()
        if total <= 0:
            self.vsb.set(0, 1); return
        self.vsb.set(self._scroll / total, min(1.0, (self._scroll + ch) / total))

    def _yview(self, *args):
        total = self._content_h(); ch = self.canvas.winfo_height()
        if args[0] == "moveto":
            self._scroll = int(float(args[1]) * total)
        elif args[0] == "scroll":
            unit = self.ROW_H if args[2] == "pages" else 60
            self._scroll += int(args[1]) * unit
        self._clamp(); self._redraw()


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
        self._art_black = False     # whole-queue black-square cover flag
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
        #      notebook claims the rest (prevents the tree from squishing it).
        #      Its contents live in a scrollable canvas so everything stays
        #      reachable on short screens (e.g. 1080p laptops). ----
        right_outer = ttk.Frame(mid, width=340); right_outer.pack(side="right", fill="y", padx=(8,0))
        right_outer.pack_propagate(False)
        self._right = right_outer
        right_canvas = tk.Canvas(right_outer, highlightthickness=0, width=340)
        right_vsb = ttk.Scrollbar(right_outer, orient="vertical",
                                  command=right_canvas.yview)
        right_canvas.configure(yscrollcommand=right_vsb.set)
        right_vsb.pack(side="right", fill="y")
        right_canvas.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(right_canvas)          # the actual content parent
        self._right_inner = right
        self._right_canvas = right_canvas
        _rwin = right_canvas.create_window((0, 0), window=right, anchor="nw", width=326)
        def _right_scrollregion(_=None):
            # use the inner frame's REAL required height, not bbox("all") (which can
            # include stretched/empty window-item space and create phantom scroll room)
            content_h = right.winfo_reqheight()
            view_h = right_canvas.winfo_height()
            # if everything fits, scrollregion == viewport so the bar stays inactive
            # and there's no empty space to scroll into
            region_h = max(content_h, view_h)
            right_canvas.configure(scrollregion=(0, 0, 326, region_h))
        right.bind("<Configure>", _right_scrollregion)
        right_canvas.bind("<Configure>", _right_scrollregion)
        # compute the scrollregion once the panel is fully laid out, so the
        # scrollbar reflects the real content height immediately (not greyed out)
        self.after_idle(_right_scrollregion)
        self.after(300, _right_scrollregion)
        # scroll the right panel with the wheel only when the pointer is over it
        right_canvas.bind("<Enter>", lambda e: setattr(self, "_right_hot", True))
        right_canvas.bind("<Leave>", lambda e: setattr(self, "_right_hot", False))
        right.bind("<Enter>", lambda e: setattr(self, "_right_hot", True))
        right.bind("<Leave>", lambda e: setattr(self, "_right_hot", False))
        self._right_hot = False

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
        qtopbar = ttk.Frame(q_tab); qtopbar.pack(fill="x", pady=(4,0))
        self.q_album = tk.BooleanVar(value=False)
        ttk.Checkbutton(qtopbar, text="Album view", variable=self.q_album,
                        command=self._q_toggle_album).pack(side="left", padx=4)
        ttk.Label(qtopbar, text="(album view plays left-to-right)",
                  foreground="#888").pack(side="left")
        self._q_body = ttk.Frame(q_tab); self._q_body.pack(fill="both", expand=True, pady=(2,2))
        self._q_album_wrap = ttk.Frame(self._q_body)
        self.q_grid = AlbumGrid(self, self._q_album_wrap,
            get_items=lambda: getattr(self, "_q_album_items", []),
            on_click=self._album_click, on_double=self._q_album_double,
            on_menu=self._q_album_menu)
        qf = ttk.Frame(self._q_body); qf.pack(fill="both", expand=True)
        self._q_tree_frame = qf
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
        self.bga_btn = ttk.Button(qbtns, text="▶ Render All BGA in Queue",
                                  command=self.render_all_bga)
        if ffmpeg_path():
            self.bga_btn.pack(side="right", padx=(0,6))
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
        _fmts = ["FLAC", "WAV"] + (["OGG", "MP3"] if ffmpeg_path() else [])
        self.fmt_pick = ttk.Combobox(ctrls, state="readonly", width=7, values=_fmts)
        self.fmt_pick.set("FLAC")
        self.fmt_pick.pack(side="left")

        # Tables tab
        t_tab = ttk.Frame(self.nb); self.nb.insert(1, t_tab, text="Tables")
        self.t_tab = t_tab
        tbar = ttk.Frame(t_tab); tbar.pack(fill="x", pady=(4,2))
        self.tbl_album = tk.BooleanVar(value=False)
        ttk.Checkbutton(tbar, text="Album view", variable=self.tbl_album,
                        command=self._tbl_toggle_album).pack(side="left", padx=(0,10))
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
        self._tbl_body = ttk.Frame(t_tab); self._tbl_body.pack(fill="both", expand=True)
        self._tbl_album_wrap = ttk.Frame(self._tbl_body)
        self.tbl_grid = AlbumGrid(self, self._tbl_album_wrap,
            get_items=lambda: getattr(self, "_tbl_album_items", []),
            on_click=self._album_click, on_double=self._album_double,
            on_menu=self._album_menu_library,
            get_sections=lambda: getattr(self, "_tbl_album_sections", {}))
        tf = ttk.Frame(self._tbl_body); tf.pack(fill="both", expand=True)
        self._tbl_tree_frame = tf
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
        self.pl_album = tk.BooleanVar(value=False)
        ttk.Checkbutton(pbar, text="Album view", variable=self.pl_album,
                        command=self._pl_toggle_album).pack(side="left", padx=(0,10))
        ttk.Label(pbar, text="Playlist:").pack(side="left")
        self.pl_pick = ttk.Combobox(pbar, state="readonly", width=26, values=[])
        self.pl_pick.pack(side="left", padx=4)
        self.pl_pick.bind("<<ComboboxSelected>>", lambda e: self.show_playlist())
        ttk.Button(pbar, text="New…", command=self.new_playlist).pack(side="left", padx=(6,0))
        ttk.Button(pbar, text="Rename…", command=self.rename_playlist).pack(side="left", padx=4)
        ttk.Button(pbar, text="Delete", command=self.delete_playlist).pack(side="left")
        self.pl_status = ttk.Label(pbar, text="", foreground="#666")
        self.pl_status.pack(side="left", padx=8)
        self._pl_body = ttk.Frame(p_tab); self._pl_body.pack(fill="both", expand=True)
        self._pl_album_wrap = ttk.Frame(self._pl_body)
        self.pl_grid = AlbumGrid(self, self._pl_album_wrap,
            get_items=lambda: getattr(self, "_pl_album_items", []),
            on_click=self._pl_album_click, on_double=self._pl_album_double,
            on_menu=self._pl_album_menu)
        pf = ttk.Frame(self._pl_body); pf.pack(fill="both", expand=True)
        self._pl_tree_frame = pf
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

        # ---- Discovery tab: an art grid of the whole library (AlbumGrid) ----
        d_tab = ttk.Frame(self.nb); self.nb.insert(1, d_tab, text="Discovery")
        self.disc_tab = d_tab
        dbar = ttk.Frame(d_tab); dbar.pack(fill="x", pady=(4,2))
        self.disc_status = ttk.Label(dbar, text="scroll through your library -- "
                                                "double-click a tile to listen",
                                     foreground="#666")
        self.disc_status.pack(side="left", padx=10)
        dwrap = ttk.Frame(d_tab); dwrap.pack(fill="both", expand=True)
        self._disc_items = []
        self.disc_grid = AlbumGrid(self, dwrap,
            get_items=lambda: self._disc_items,
            on_click=self._album_click, on_double=self._album_double,
            on_menu=self._album_menu_library)
        self._disc_loaded = False
        # one global wheel handler routes to whichever grid the mouse is over
        self.bind_all("<MouseWheel>", self._album_wheel)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)


        # ---- right panel: Tags, BMS information, Album art (top to bottom) ----
        # 1) Tags (editable)
        tags_box = ttk.LabelFrame(right, text="Tags"); tags_box.pack(fill="x")
        self.fields = {}
        self._tag_keys = ("Title", "Artist", "Album", "AlbumArtist", "Genre", "BPM")
        labels = {"AlbumArtist": "Alb.Artist"}
        for ki, key in enumerate(self._tag_keys):
            row = ttk.Frame(tags_box); row.pack(fill="x", padx=8, pady=3)
            ttk.Label(row, text=labels.get(key, key), width=8).pack(side="left")
            e = ttk.Entry(row); e.pack(side="left", fill="x", expand=True)
            e.bind("<KeyRelease>", self.on_tag_edit)
            e.bind("<Down>", self._tag_next_song)        # jump to next song in the list
            e.bind("<Up>", self._tag_prev_song)          # jump to previous song
            # Tab / Shift-Tab cycle ONLY through the tag fields (Title…BPM), wrapping,
            # so you can tag at speed from the keyboard without focus escaping the box
            e.bind("<Tab>", lambda ev, i=ki: self._tag_focus_field(i + 1))
            e.bind("<Shift-Tab>", lambda ev, i=ki: self._tag_focus_field(i - 1))
            e.bind("<ISO_Left_Tab>", lambda ev, i=ki: self._tag_focus_field(i - 1))  # X11 Shift-Tab
            self.fields[key] = e
        # session-only Album / Album Artist: empty at startup, persist within the
        # session, reset on reboot (never written to config)
        self._session_album = ""
        self._session_album_artist = ""
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
        # scrubber: drag to flip through the folder's images (kept in sync with ◀ ▶)
        self.song_art_slider = ttk.Scale(pic, from_=1, to=1, orient="horizontal",
                                         command=self._on_art_slider)
        self.song_art_slider.pack(fill="x", padx=10, pady=(2,0))
        self._art_slider_sync = False   # guard so programmatic .set() doesn't recurse
        brow = ttk.Frame(pic); brow.pack(fill="x", padx=6, pady=(0,6))
        # fixed width + clipping so a long filename can't change the panel width
        self.song_art_status = ttk.Label(brow, text="", foreground="#666",
                                         width=24, anchor="w")
        self.song_art_status.pack(side="left")
        self.ignore_bmp = tk.BooleanVar(value=True)
        ttk.Checkbutton(brow, text="ignore .bmp", variable=self.ignore_bmp,
                        command=self._reload_song_art).pack(side="right")
        # per-song picker state
        self._song_art_files = []   # image paths in the current song's folder
        self._song_art_idx = 0      # which one is shown/selected
        self._song_art_thumb = None # keep a ref so Tk doesn't GC the preview

        # 4) Album art (one cover for the whole queue) — overrides per-song art
        art = ttk.LabelFrame(right, text="Album art (whole queue)"); art.pack(fill="x", pady=(8,4))
        btns = ttk.Frame(art); btns.pack(fill="x", padx=6, pady=(6,2))
        ttk.Button(btns, text="Choose…", command=self.choose_art).pack(side="left")
        ttk.Button(btns, text="Assign black square",
                   command=self.assign_black_art).pack(side="left", padx=6)
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
        self.redetect_btn = ttk.Button(tp, text="⟳ Audio device", width=14,
                                       command=self.redetect_audio_device)
        self.redetect_btn.pack(side="left", padx=(0,12))
        self.time_lbl = ttk.Label(tp, text="0:00 / 0:00", width=14)
        self.time_lbl.pack(side="left")
        # waveform doubles as the seek bar: filled amplitude envelope with a
        # progress fill; click or drag to seek
        self.wave = tk.Canvas(tp, height=34, highlightthickness=0)
        try:                        # match the system/ttk window background so it blends in
            self.wave.config(bg=ttk.Style(self).lookup("TFrame", "background")
                             or self.cget("background"))
        except tk.TclError:
            pass
        self.wave.pack(side="left", fill="x", expand=True, padx=8)
        self.wave.bind("<Configure>", lambda e: self._draw_wave())
        self.wave.bind("<ButtonPress-1>", self._wave_grab)
        self.wave.bind("<B1-Motion>", self._wave_drag)
        self.wave.bind("<ButtonRelease-1>", self._wave_release)
        self._wave_env = None      # cached amplitude envelope (list of 0..1)
        self._wave_pos = 0.0       # 0..1 progress for the fill overlay
        # volume control: a horizontally-stretched triangle that fills left→right,
        # styled like the waveform (blue fill over grey). Click/drag to set.
        self.vol_level = 1.0       # 0..1
        self.vol = tk.Canvas(tp, width=110, height=24, highlightthickness=0)
        try:
            self.vol.config(bg=ttk.Style(self).lookup("TFrame", "background")
                            or self.cget("background"))
        except tk.TclError:
            pass
        self.vol.pack(side="right", padx=(0,6))
        self.vol.bind("<Configure>", lambda e: self._draw_vol())
        self.vol.bind("<ButtonPress-1>", self._vol_set_from_event)
        self.vol.bind("<B1-Motion>", self._vol_set_from_event)
        # shuffle / loop toggles (left of volume). Checkbuttons read as on/off.
        self.loop_on = tk.BooleanVar(value=False)
        self.shuffle_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(tp, text="Loop", variable=self.loop_on).pack(side="right", padx=(0,8))
        ttk.Checkbutton(tp, text="Shuffle", variable=self.shuffle_on).pack(side="right", padx=(8,4))
        self.now_lbl = ttk.Label(tp, text="", foreground="#2a7", width=22)
        self.now_lbl.pack(side="right", padx=(0,12))
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
        # The right panel scrolls, so the window can be shorter than the panel's
        # full height without clipping anything (helps on 1080p laptops).
        self.minsize(980, 560)

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

    def _show_black_preview(self):
        try:
            box = self._ART_BOX
            self._song_art_thumb = tk.PhotoImage(width=box, height=box)
            self._song_art_thumb.put("#000000", to=(0, 0, box, box))
            self.song_art_canvas.config(image=self._song_art_thumb, text="", compound="center")
            self.song_art_status.config(text="black square")
        except Exception:
            pass

    def _set_art_label(self, path):
        label = path if "/" not in path and "\\" not in path else os.path.basename(path)
        self.art_lbl.config(text=f"Art: {label}")

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
            self._art_black = False
            self._set_art_label(path)
            self.log(f"Album art set: {os.path.basename(path)} → "
                     f"{size[0]}x{size[1]} JPEG, {len(data)//1024} KB{over}  "
                     f"(resets when you close the app)")
        except Exception as e:
            messagebox.showerror("Couldn't read that image",
                f"That file couldn't be processed as album art:\n{e}")

    def clear_art(self):
        self.art_path = None
        self._art_black = False
        self.art_lbl.config(text="(none — FLACs will have no cover)")
        self.log("Album art cleared.")

    def assign_black_art(self):
        """Set the whole-queue cover to a plain black 1:1 square (a clean cover
        instead of no art at all). Generated in memory — no file on disk."""
        if not _PIL_OK:
            messagebox.showerror("Album art unavailable",
                "Pillow isn't installed, so the app can't make the image.\n"
                "Install it once with:  pip install Pillow")
            return
        self._art_black = True
        self.art_path = None
        self._set_art_label("solid black square")
        # show it in the preview box so it's obvious what was assigned
        self._show_black_preview()
        self.log("Album art set: solid black square (resets when you close the app).")
        # if a playlist song is selected, persist the black square to its entry too,
        # so it survives reboots just like a normal art pick does there
        if self._save_pl_entry_art("__BLACK__"):
            self.log("Saved black square to this playlist entry.")

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
        self._on_art_selected()
        self._render_song_art()

    def _on_art_slider(self, val):
        """Scrub to the image the slider points at (1-based). Guarded so the
        programmatic .set() done while rendering doesn't recurse."""
        if self._art_slider_sync or not self._song_art_files:
            return
        idx = int(round(float(val))) - 1
        idx = max(0, min(idx, len(self._song_art_files) - 1))
        if idx == self._song_art_idx:
            return
        self._song_art_idx = idx
        self._on_art_selected()
        self._render_song_art()

    def _sync_art_slider(self):
        """Point the slider at the current image without re-triggering its command."""
        n = len(self._song_art_files)
        self._art_slider_sync = True
        try:
            self.song_art_slider.config(from_=1, to=max(1, n),
                                        state=("normal" if n > 1 else "disabled"))
            self.song_art_slider.set(self._song_art_idx + 1)
        except tk.TclError:
            pass
        self._art_slider_sync = False

    def _on_art_selected(self):
        """Record the currently-shown art as the selection for whatever is active:
        a queued item (in memory) or a playlist entry (saved to its JSON file)."""
        if not self._song_art_files:
            return
        art = self._song_art_files[self._song_art_idx]
        qi = self._song_art_qitem
        if qi is not None:
            qi["art"] = art                       # queued song: in-memory cover pick
        self._save_pl_entry_art(art)              # playlist song: persist to its JSON

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
            self._sync_art_slider()
            return
        path = self._song_art_files[self._song_art_idx]
        name = os.path.basename(path)
        if len(name) > 16:
            name = name[:13] + "…"
        self.song_art_status.config(
            text=f"{self._song_art_idx+1} / {n}  ·  {name}")
        self._sync_art_slider()
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
        self.song_art_slider.config(state=state)

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
        # index by exact path, and by folder, so album views resolve songs in O(1)
        self._path_index = {}
        self._folder_index = {}
        for s in songs:
            m = s.get("md5")
            if m:
                self._md5_index.setdefault(m, s)
            self._path_index[s["path"]] = s
            self._folder_index.setdefault(os.path.dirname(s["path"]), []).append(s)
        for charts in self._folder_index.values():
            charts.sort(key=lambda s: _num(s.get("notes")))
        self.apply_filter()
        self.reload_tables()
        # if the Discovery tab is open (or was opened) but hasn't populated yet
        # because the cache was still loading, fill it in now
        try:
            if not self._disc_loaded and self.nb.select() == str(self.disc_tab) and self.songs:
                self._disc_loaded = True
                self.discovery_reset()
        except tk.TclError:
            pass
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
        album_songs = []           # owned songs in level order, for the album view
        album_sections = {}        # {first album index of a level: "Level N"} 
        for lv in sorted(self._tbl_by_level, key=level_key):
            entries = list(self._tbl_by_level[lv])
            if self._tbl_sort_col:
                entries.sort(key=sortkey(self._tbl_sort_col), reverse=self._tbl_sort_desc)
            owned_here = sum(1 for e in entries if e["md5"] in idx)
            parent = self.ttree.insert(
                "", "end", iid=f"L{lv}", open=True,
                text=f"{sym}{lv}  ({owned_here}/{len(entries)})",
                values=("", "", "", ""))
            level_started = False
            for e in entries:
                song = idx.get(e["md5"])
                owned = song is not None
                if owned:
                    owned_count += 1
                iid = f"r{rid}"; rid += 1
                self._row_by_iid[iid] = {"entry": e, "song": song, "owned": owned}
                if owned:
                    self._tbl_play_order.append(song)
                    if not level_started:        # mark this level's first owned tile
                        album_sections[len(album_songs)] = f"{sym}{lv}"
                        level_started = True
                    album_songs.append(song)
                title = (song["title"] if owned else e["title"]) or e["title"] or "(unknown)"
                artist = (song["artist"] if owned else e["artist"]) if owned else e["artist"]
                notes = song.get("notes","") if owned else ""
                self.ttree.insert(parent, "end", iid=iid, text="",
                                  tags=() if owned else ("missing",),
                                  values=(title, artist, "✓" if owned else "missing", notes))
        self._tbl_album_songs = album_songs
        self._tbl_album_sections = album_sections
        # header arrows
        for c in ("title", "artist", "owned", "notes"):
            arrow = (" ▼" if self._tbl_sort_desc else " ▲") if c == self._tbl_sort_col else ""
            self.ttree.heading(c, text=c.title() + arrow)
        self.table_status.config(
            text=f"{getattr(self,'_tbl_name','')}: you own {owned_count} of {getattr(self,'_tbl_total',0)}")
        if getattr(self, "tbl_album", None) and self.tbl_album.get():
            self._tbl_album_items = self._album_items_from_songs(self._tbl_album_songs)
            self.tbl_grid.refresh(reset_scroll=False)

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
        # multi-select: offer "add all selected owned to queue"
        sel = [i for i in self.ttree.selection() if not i.startswith("L")]
        if len(sel) > 1:
            songs = []
            for i in sel:
                r = self._table_row_at(i)
                if r and r.get("owned") and r.get("song"):
                    songs.append(r["song"])
            m = tk.Menu(self, tearoff=0)
            m.add_command(label=f"Add {len(songs)} selected to Queue",
                          command=lambda ss=songs: self._enqueue_songs(ss))
            sub = tk.Menu(m, tearoff=0)
            for name in sorted(self._playlists):
                sub.add_command(label=name,
                                command=lambda n=name, ss=songs: [self._add_song_to_playlist(x, n) for x in ss])
            if self._playlists:
                m.add_cascade(label="Add selected to playlist", menu=sub)
            m.tk_popup(event.x_root, event.y_root)
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
        best = self._lowest_per_folder(owned)
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
        best = self._lowest_per_folder(owned)
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

    def _enqueue_songs(self, songs, inherit_tags=None):
        if not songs:
            return
        queued = {q["path"] for q in self.queue}
        added = 0
        for s in songs:
            if s["path"] in queued:
                continue
            self.queue.append(self._make_queue_item(s, inherit_tags))
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
    def _on_tab_changed(self, _):
        try:
            if self.nb.select() == str(self.disc_tab) and not self._disc_loaded:
                if self.songs:
                    self._disc_loaded = True
                    self.discovery_reset()
                else:
                    self.disc_status.config(
                        text="loading library\u2026 Discovery will fill in shortly")
        except tk.TclError:
            pass

    def discovery_reset(self):
        import random
        if not self.songs:
            self.disc_status.config(text="scan a library first -- then come back to explore it")
            return
        groups = {}
        for s in self.songs:
            groups.setdefault(os.path.dirname(s["path"]), []).append(s)
        order = []
        for charts in groups.values():
            charts = sorted(charts, key=lambda s: _num(s.get("notes")))
            order.append((charts[0], charts))
        random.shuffle(order)
        self._disc_items = order
        self.disc_status.config(text=f"{len(order):,} songs -- scroll to explore, "
                                     "double-click to listen")
        self.disc_grid.refresh()

    # ---- shared AlbumGrid interaction callbacks ----
    def _album_wheel(self, event):
        # route the wheel to whichever album grid the pointer is currently over
        for g in (getattr(self, "disc_grid", None), getattr(self, "tbl_grid", None),
                  getattr(self, "pl_grid", None), getattr(self, "q_grid", None)):
            if g is not None and g._hot:
                g.on_mousewheel(event)
                return
        # otherwise, if the pointer is over the (scrollable) right panel, scroll it
        if getattr(self, "_right_hot", False) and getattr(self, "_right_canvas", None):
            self._right_canvas.yview_scroll(-(event.delta // 120), "units")

    def _album_click(self, idx, rep, charts):
        self.selected_kind = "library"; self.selected_index = None
        self._update_info(rep)
        self._show_tags(self._default_tags(rep), editable=False,
                        hint="Album preview (read-only) -- add to Queue to edit tags.")
        self._load_song_art_for(rep["path"], None)

    def _album_double(self, idx, rep, charts):
        if not _SD_OK:
            return
        s = pick_playable_chart(charts)
        if s:
            self._start_song("library", 0, s["path"], s["title"])

    def _album_menu_library(self, event, idx, rep, charts):
        s = pick_playable_chart(charts)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Play", command=lambda: self._album_double(idx, rep, charts))
        menu.add_command(label="Add to Queue",
                         command=lambda: self._enqueue_songs([s] if s else []))
        menu.add_command(label="Show all charts for this song",
                         command=lambda: self._show_all_for_song(rep))
        plmenu = tk.Menu(menu, tearoff=0)
        self._fill_playlist_submenu(plmenu, lambda: rep)
        menu.add_cascade(label="Add to playlist", menu=plmenu)
        menu.tk_popup(event.x_root, event.y_root)

    # ---- Album-view toggles for Tables / Playlists / Queue ----
    @staticmethod
    def _lowest_per_folder(songs):
        """One song per folder: the chart with the fewest notes. Used when adding a
        whole table/level to the queue (one representative chart per song folder)."""
        best = {}
        for s in songs:
            folder = os.path.dirname(os.path.normcase(os.path.abspath(s["path"])))
            cur = best.get(folder)
            if cur is None or _num(s.get("notes")) < _num(cur.get("notes")):
                best[folder] = s
        return best

    def _charts_for_folder(self, path):
        """All charts in the same folder as `path` (a song's difficulties),
        via the prebuilt folder index — O(1) instead of scanning the library."""
        return getattr(self, "_folder_index", {}).get(os.path.dirname(path))

    def _album_items_from_songs(self, songs):
        """Turn a flat list of song dicts into AlbumGrid (rep, charts) tuples."""
        items = []
        for s in songs:
            if not s or not s.get("path"):
                continue
            charts = self._charts_for_folder(s["path"]) or [s]
            items.append((s, charts))
        return items

    def _swap_view(self, tree_frame, album_wrap, show_album):
        if show_album:
            tree_frame.pack_forget()
            album_wrap.pack(fill="both", expand=True)
        else:
            album_wrap.pack_forget()
            tree_frame.pack(fill="both", expand=True)

    def _tbl_toggle_album(self):
        on = self.tbl_album.get()
        self._swap_view(self._tbl_tree_frame, self._tbl_album_wrap, on)
        if on:
            self._tbl_album_items = self._album_items_from_songs(
                getattr(self, "_tbl_play_order", []))
            self.tbl_grid.refresh()

    def _pl_toggle_album(self):
        on = self.pl_album.get()
        self._swap_view(self._pl_tree_frame, self._pl_album_wrap, on)
        if on:
            owned = [s for s in getattr(self, "_pl_rows", []) if not s.get("_missing")]
            self._pl_album_items = self._album_items_from_songs(owned)
            self.pl_grid.refresh()

    def _q_toggle_album(self):
        on = self.q_album.get()
        self._swap_view(self._q_tree_frame, self._q_album_wrap, on)
        if on:
            self._q_rebuild_album_items()
            self.q_grid.refresh()

    def _q_rebuild_album_items(self):
        # queue items carry only a path; resolve each to a song dict for art/charts
        items = []
        for q in self.queue:
            s = getattr(self, "_path_index", {}).get(q["path"])
            if s is None:
                s = {"path": q["path"], "title": q.get("title",""),
                     "artist": q.get("artist",""), "notes": q.get("notes","")}
            charts = self._charts_for_folder(q["path"]) or [s]
            items.append((s, charts))
        self._q_album_items = items

    # playlist album: double-click plays within the playlist (auto-advance)
    def _pl_row_for_path(self, path):
        """Map a song path back to its row index in the current playlist view."""
        for i, s in enumerate(getattr(self, "_pl_rows", [])):
            if s.get("path") == path:
                return i
        return None

    def _pl_album_click(self, idx, rep, charts):
        """Selecting a tile in the playlist album view edits that entry's tags
        (same as selecting it in the list view) — fields must not be locked here."""
        row = self._pl_row_for_path(rep["path"])
        if row is None:
            return
        self.selected_kind = "playlist"; self.selected_index = row
        self._update_info(rep)
        self._show_tags(self._pl_entry_tags(row, self._pl_rows[row]), editable=True,
                        hint="Edit tags here; they save to the playlist file as you type.")
        self._load_song_art_for(rep["path"], None)

    def _pl_album_double(self, idx, rep, charts):
        if not _SD_OK:
            return
        s = pick_playable_chart(charts)
        row = self._pl_row_for_path(rep["path"])
        if s and row is not None:
            self._start_song("playlist", row, s["path"], s["title"])

    def _pl_album_menu(self, event, idx, rep, charts):
        s = pick_playable_chart(charts)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Play", command=lambda: self._pl_album_double(idx, rep, charts))
        menu.add_command(label="Add to Queue",
                         command=lambda: self._enqueue_songs([s] if s else []))
        plmenu = tk.Menu(menu, tearoff=0)
        self._fill_playlist_submenu(plmenu, lambda: rep)
        menu.add_cascade(label="Add to playlist", menu=plmenu)
        menu.tk_popup(event.x_root, event.y_root)

    # queue album: double-click starts queue playback at that tile (L-to-R order)
    def _q_album_double(self, idx, rep, charts):
        if not _SD_OK or idx >= len(self.queue):
            return
        q = self.queue[idx]
        self._start_song("queue", idx, q["path"], q.get("title",""))

    def _q_album_menu(self, event, idx, rep, charts):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Play", command=lambda: self._q_album_double(idx, rep, charts))
        menu.add_command(label="Remove from queue",
                         command=lambda: self._q_album_remove(idx))
        menu.tk_popup(event.x_root, event.y_root)

    def _q_album_remove(self, idx):
        if 0 <= idx < len(self.queue):
            del self.queue[idx]
            self.refresh_queue()

    def _ctx_show_all(self):
        self._show_all_for_song(self._ctx_song)

    # ----------------------------------------------------------- playlists tab
    def _fill_playlist_submenu(self, menu, get_song, get_tags=None):
        """Rebuild an 'Add to playlist' submenu: one entry per playlist + New….
        get_tags (optional) returns tags to inherit (Album/AlbumArtist stripped)."""
        menu.delete(0, "end")
        tags_fn = get_tags or (lambda: None)
        for name in sorted(self._playlists):
            menu.add_command(label=name,
                             command=lambda n=name: self._add_song_to_playlist(
                                 get_song(), n, tags_fn()))
        if self._playlists:
            menu.add_separator()
        menu.add_command(label="New playlist…",
                         command=lambda: self._add_song_to_new_playlist(
                             get_song(), tags_fn()))

    def _add_song_to_playlist(self, song, name, inherit_tags=None):
        if not song:
            return
        md5 = song.get("md5", "")
        if not md5:
            self.log(f"Can't add '{song.get('title','?')}' — no MD5 available "
                     f"(rescan the library so it has a hash)."); return
        entries = self._playlists.setdefault(name, [])
        if any(isinstance(e, dict) and e.get("md5") == md5 for e in entries):
            self.log(f"Already in '{name}': {song['title']}"); return
        entry = {"md5": md5, "title": song.get("title",""),
                 "artist": song.get("artist","")}
        # carry over tags from the source, minus Album / Album Artist (those are
        # per-destination on purpose)
        if inherit_tags:
            carried = {k: v for k, v in inherit_tags.items()
                       if k not in ("Album", "AlbumArtist") and v}
            if carried:
                entry["tags"] = carried
                entry["title"] = carried.get("Title", entry["title"])
                entry["artist"] = carried.get("Artist", entry["artist"])
        entries.append(entry)
        save_one_playlist(name, entries)
        self.log(f"Added to '{name}': {entry['title']}")
        if self.pl_pick.get() == name:
            self.show_playlist()

    def _add_song_to_new_playlist(self, song, inherit_tags=None):
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
        self._add_song_to_playlist(song, name, inherit_tags)
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
        if getattr(self, "pl_album", None) and self.pl_album.get():
            owned = [s for s in self._pl_rows if not s.get("_missing")]
            self._pl_album_items = self._album_items_from_songs(owned)
            self.pl_grid.refresh(reset_scroll=False)

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
        i = int(sel[0])
        s = self._pl_rows[i]
        self.selected_kind = "playlist"; self.selected_index = i
        self._update_info(s)
        if s.get("_missing"):
            # song not in this library: tags still editable & saved, but no art/play
            self._show_tags(self._pl_entry_tags(i, s), editable=True,
                            hint="Not in your library — tags still save to the playlist.")
            self._load_song_art_for(None)
            return
        self._show_tags(self._pl_entry_tags(i, s), editable=True,
                        hint="Edit tags here; they save to the playlist file as you type.")
        # show the entry's saved art if it still exists, else the folder picker
        entry = self._playlists[self.pl_pick.get()][i]
        saved_art = entry.get("art") if isinstance(entry, dict) else None
        if saved_art == "__BLACK__":
            self._load_song_art_for(s["path"], None)
            self._show_black_preview()
        elif saved_art and os.path.isfile(saved_art):
            self._load_song_art_for(s["path"], None)
            self._select_art_path(saved_art)
        else:
            self._load_song_art_for(s["path"], None)

    def _pl_entry_tags(self, i, s):
        """Tags to show for a playlist row: the entry's saved tags if present, else
        sensible defaults from the song, merged with the session Album/AlbumArtist."""
        name = self.pl_pick.get()
        entry = self._playlists[name][i] if name in self._playlists else {}
        saved = entry.get("tags", {}) if isinstance(entry, dict) else {}
        base = {"Title": s.get("title",""), "Artist": s.get("artist",""),
                "Album": "", "AlbumArtist": "",
                "Genre": s.get("genre",""), "BPM": str(s.get("bpm",""))}
        base.update({k: v for k, v in saved.items() if v})
        return base

    def _select_art_path(self, art_path):
        """Point the art picker at a specific file in the current folder (if present)."""
        try:
            if art_path in self._song_art_files:
                self._song_art_idx = self._song_art_files.index(art_path)
                self._render_song_art()
        except (AttributeError, ValueError):
            pass

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
        # If right-clicking outside the current selection, select just this row;
        # if it's already part of a multi-selection, keep the whole selection.
        if iid not in self.pltree.selection():
            self.pltree.selection_set(iid)
        selected = self.pltree.selection()
        multi = len(selected) > 1
        s = self._pl_rows[int(iid)]
        owned = not s.get("_missing")
        # entries: 0 Play, 1 Add to Queue, 2 Add to playlist (cascade), 3 Remove
        if multi:
            # build a dedicated multi-select menu
            m = tk.Menu(self, tearoff=0)
            songs = [self._pl_rows[int(i)] for i in selected
                     if not self._pl_rows[int(i)].get("_missing")]
            m.add_command(label=f"Add {len(songs)} selected to Queue",
                          command=lambda ss=songs: self._enqueue_songs(ss))
            sub = tk.Menu(m, tearoff=0)
            self._fill_playlist_submenu(sub, lambda: None)  # placeholder, replaced below
            # add-to-playlist for many isn't entry-tag aware; just add each song
            sub.delete(0, "end")
            for name in sorted(self._playlists):
                sub.add_command(label=name,
                                command=lambda n=name, ss=songs: [self._add_song_to_playlist(x, n) for x in ss])
            m.add_cascade(label="Add selected to playlist", menu=sub)
            m.add_separator()
            m.add_command(label="Remove selected from playlist",
                          command=self.remove_from_playlist)
            m.tk_popup(event.x_root, event.y_root)
            return
        for idx in (0, 1, 2):
            self._plmenu.entryconfig(idx, state="normal" if owned else "disabled")
        if owned:
            self._fill_playlist_submenu(
                self._pl_plmenu, self._pl_selected_song,
                get_tags=lambda i=int(iid): self._pl_saved_tags(i))
        self._plmenu.tk_popup(event.x_root, event.y_root)

    def _current_pl_entry(self):
        """The (name, entry-dict) for the currently-selected playlist song, or
        (None, None) if no playlist song is selected / the entry isn't a dict."""
        if self.selected_kind != "playlist" or self.selected_index is None:
            return None, None
        name = self.pl_pick.get(); i = self.selected_index
        if name in self._playlists and i < len(self._playlists[name]):
            entry = self._playlists[name][i]
            if isinstance(entry, dict):
                return name, entry
        return None, None

    def _save_pl_entry_art(self, value):
        """Persist an art value (a path or the "__BLACK__" sentinel) onto the
        currently-selected playlist entry and write the file. Returns True if saved."""
        name, entry = self._current_pl_entry()
        if entry is not None:
            entry["art"] = value
            save_one_playlist(name, self._playlists[name])
            return True
        return False

    def _pl_saved_tags(self, i):
        """The saved tags dict for playlist row i (for tag inheritance)."""
        name = self.pl_pick.get()
        if name in self._playlists and i < len(self._playlists[name]):
            entry = self._playlists[name][i]
            if isinstance(entry, dict):
                return entry.get("tags")
        return None

    def _pl_play(self):
        s = self._pl_selected_song()
        if s and _SD_OK:
            sel = self.pltree.selection()
            self._start_song("playlist", int(sel[0]), s["path"], s["title"])

    def _pl_add_queue(self):
        sel = self.pltree.selection()
        s = self._pl_selected_song()
        if s and sel:
            self._enqueue_songs([s], inherit_tags=self._pl_saved_tags(int(sel[0])))

    def remove_from_playlist(self):
        name = self.pl_pick.get()
        sel = self.pltree.selection()
        if not name or not sel:
            return
        # delete in descending index order so earlier deletions don't shift the rest
        for i in sorted((int(x) for x in sel), reverse=True):
            try:
                del self._playlists[name][i]
            except (KeyError, IndexError):
                pass
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
        # build queue-like items from the playlist's owned songs (resolved by MD5),
        # using each entry's saved tags + art so you can tag now and render later
        idx = getattr(self, "_md5_index", {})
        items = []
        for entry in self._playlists[name]:
            md5 = entry.get("md5", "") if isinstance(entry, dict) else ""
            s = idx.get(md5) if md5 else None
            if not s:
                continue
            tags = dict(self._default_tags(s))
            saved = entry.get("tags", {}) if isinstance(entry, dict) else {}
            tags.update({k: v for k, v in saved.items() if v})
            it = {"path": s["path"], "tags": tags, "title": tags.get("Title", s["title"])}
            # use the saved art if its file still exists; otherwise fall back to the
            # song's folder art so a shared playlist never crashes on missing images
            saved_art = entry.get("art") if isinstance(entry, dict) else None
            if saved_art == "__BLACK__":
                it["_black_art"] = True       # render side uses black-square bytes
            elif saved_art and os.path.isfile(saved_art):
                it["art"] = saved_art
            it["_fallback_art"] = s["path"]   # render side resolves folder art if needed
            items.append(it)
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
        # Album / Album Artist default to the current session values (empty at
        # startup; whatever the user last typed otherwise). Genre comes from the chart.
        return {"Title": s["title"], "Artist": s["artist"],
                "Album": getattr(self, "_session_album", ""),
                "AlbumArtist": getattr(self, "_session_album_artist", ""),
                "Genre": s.get("genre", ""), "BPM": str(s.get("bpm", ""))}

    def _make_queue_item(self, s, inherit_tags=None):
        """Build a queue item from a song dict. If this song is the one currently
        shown in the art picker, carry that previewed image over as its cover so a
        pick made before queueing isn't lost. inherit_tags (e.g. from a tagged
        playlist entry) is merged in, minus Album / Album Artist."""
        tags = self._default_tags(s)
        if inherit_tags:
            for k, v in inherit_tags.items():
                if k not in ("Album", "AlbumArtist") and v:
                    tags[k] = v
        item = {"path": s["path"], "title": tags.get("Title", s["title"]),
                "artist": tags.get("Artist", s["artist"]),
                "mode": s.get("mode", "?"), "notes": s.get("notes", ""),
                "tags": tags}
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
        if getattr(self, "q_album", None) and self.q_album.get():
            self._q_rebuild_album_items()
            self.q_grid.refresh(reset_scroll=False)

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
            # Album / Album Artist are session-wide: typing them updates the session
            # value and propagates to EVERY queued item (and future additions).
            self._session_album = self.fields["Album"].get()
            self._session_album_artist = self.fields["AlbumArtist"].get()
            for other in self.queue:
                other["tags"]["Album"] = self._session_album
                other["tags"]["AlbumArtist"] = self._session_album_artist
            try:
                self.qtree.item(str(i), values=(q["tags"]["Title"],
                                                q["tags"]["Artist"], q["mode"],
                                                q.get("notes","")))
            except tk.TclError:
                pass
        # live-save edits into the playlist file (tag-at-your-own-pace)
        elif self.selected_kind == "playlist" and self.selected_index is not None:
            self._save_playlist_entry_tags(self.selected_index)

    def _save_playlist_entry_tags(self, i):
        name = self.pl_pick.get()
        if name not in self._playlists or i >= len(self._playlists[name]):
            return
        entry = self._playlists[name][i]
        if not isinstance(entry, dict):
            return
        entry["tags"] = {k: e.get() for k, e in self.fields.items()}
        # keep the lightweight title/artist labels in sync for the greyed display
        entry["title"] = entry["tags"].get("Title", entry.get("title",""))
        entry["artist"] = entry["tags"].get("Artist", entry.get("artist",""))
        save_one_playlist(name, self._playlists[name])

    def _active_tag_tree(self):
        """The tree whose selection the tag fields are editing (queue or playlist)."""
        if self.selected_kind == "queue":
            return self.qtree
        if self.selected_kind == "playlist":
            return self.pltree
        return None

    def _tag_move_song(self, direction):
        """Move the selection up/down in the active list and keep editing tags, so
        you can tag down (or up) the whole list from the keyboard."""
        tree = self._active_tag_tree()
        if tree is None:
            return "break"
        sel = tree.selection()
        if not sel:
            return "break"
        target = tree.next(sel[0]) if direction > 0 else tree.prev(sel[0])
        if target:
            tree.selection_set(target)
            tree.focus(target)
            tree.see(target)
            # keep typing in the same field on the newly-selected song
            focused = self.focus_get()
            self.after_idle(lambda: focused.focus_set() if focused else None)
        return "break"

    def _tag_next_song(self, _):
        return self._tag_move_song(1)

    def _tag_prev_song(self, _):
        return self._tag_move_song(-1)

    def _tag_focus_field(self, idx):
        """Focus a tag field by index, wrapping around, so Tab/Shift-Tab cycle only
        within Title…BPM and never let focus escape the tag box."""
        keys = self._tag_keys
        idx %= len(keys)
        e = self.fields[keys[idx]]
        e.focus_set()
        e.icursor("end")
        e.select_range(0, "end")
        return "break"

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

    def render_all_bga(self):
        if not self.queue:
            messagebox.showinfo("Queue empty", "Add songs to the queue first."); return
        out_dir = load_config().get("output")
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showinfo("No output folder", "Choose an output folder first."); return
        if not ffmpeg_path():
            messagebox.showinfo("ffmpeg required",
                                "BGA video export needs ffmpeg on your PATH."); return
        self.bga_btn.config(state="disabled")
        threading.Thread(target=self._render_bga_queue, args=(out_dir,), daemon=True).start()

    def _render_bga_queue(self, out_dir):
        """Render each queued chart that has an image-sequence (or static) BGA to an
        MP4 with the audio synced, in parallel across worker processes. Charts whose
        BGA is video-format or absent are skipped (logged), since this path only
        composites still images."""
        from concurrent.futures import ProcessPoolExecutor, as_completed
        ff = ffmpeg_path()
        # fps and a target size CAP. The actual video dimensions are derived from
        # each BGA's own aspect ratio (so square BGAs stay square, no letterboxing);
        # this just caps the largest side.
        fps, size = 30, (720, 720)
        try:
            items = list(self.queue)
            # decide which charts are eligible (have an image BGA we can composite)
            jobs = []
            skipped = 0
            for it in items:
                info = detect_bga(it["path"])
                if info["type"] in ("sequence", "static"):
                    title = it["tags"].get("Title", "untitled")
                    out_path = os.path.join(out_dir, self._safe_filename(title) + ".mp4")
                    jobs.append(((it["path"], out_path, ff,
                                  bms_core._LIBRARY_ROOT, fps, size), it))
                else:
                    skipped += 1
            if not jobs:
                self.log("No queued charts have an image BGA to render "
                         f"({skipped} skipped: video/none).")
                return
            workers = self._render_worker_count()
            total = len(jobs); done = 0
            self.log(f"Rendering {total} BGA video(s) at up to {max(size)}px {fps}fps "
                     f"(sized to each BGA's aspect ratio) "
                     f"with {workers} worker process(es)… "
                     f"({skipped} chart(s) skipped — video/no BGA)")
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(render_bga_video_job, job): it for job, it in jobs}
                for fut in as_completed(futs):
                    it = futs[fut]; done += 1
                    try:
                        out_path, title, err = fut.result()
                    except Exception:
                        self.log(f"  [{done}/{total}] worker crashed: "
                                 f"{it['tags'].get('Title')}")
                        continue
                    if err:
                        self.log(f"  [{done}/{total}] FAILED: {title}\n{err}")
                    else:
                        self.log(f"  [{done}/{total}] done -> {out_path}")
            self.log("BGA render complete.")
        finally:
            self.after(0, lambda: self.bga_btn.config(state="normal"))

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
        # reflect the now-playing song as the selected tile in album views
        if kind == "queue" and getattr(self, "q_album", None) and self.q_album.get():
            self.q_grid.set_selected(index)
        elif kind == "playlist" and getattr(self, "pl_album", None) and self.pl_album.get():
            self.pl_grid.set_selected(index)
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
        self._build_wave_envelope(audio)
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
        self._wave_pos = 0.0
        self._draw_wave()
        self.time_lbl.config(text="0:00 / 0:00")
        self._playing_ctx = None
        self._update_now_playing(None)

    def redetect_audio_device(self):
        """Switch playback to the current default output device (after the user
        changes their Windows audio output). Keeps the playhead where it is."""
        if not _SD_OK or self.player is None:
            return
        ok = self.player.redetect_device()
        if ok:
            self.log("Audio output switched to the current default device.")
        else:
            self.log("Couldn't open the current default audio device.")
            self._refresh_play_btn()

    # seek via the waveform: drag updates the fill, release jumps the audio
    def _wave_frac(self, x):
        w = max(1, self.wave.winfo_width())
        return min(1.0, max(0.0, x / w))

    def _wave_grab(self, e):
        self._seeking = True
        self._wave_pos = self._wave_frac(e.x)
        self._draw_wave()

    def _wave_drag(self, e):
        if self._seeking:
            self._wave_pos = self._wave_frac(e.x)
            self._draw_wave()

    def _wave_release(self, e):
        if self.player is None or self.player.duration_seconds() <= 0:
            self._seeking = False
            return
        frac = self._wave_frac(e.x)
        self.player.seek_seconds(frac * self.player.duration_seconds())
        self._wave_pos = frac
        self._seeking = False
        self._draw_wave()

    def _build_wave_envelope(self, audio):
        """Downsample a stereo buffer to a small amplitude envelope (peaks per
        bucket, 0..1) for the filled waveform graph. Cheap and resolution-capped."""
        try:
            import numpy as np
            mono = np.abs(np.asarray(audio)).mean(axis=1) if audio.ndim > 1 \
                else np.abs(np.asarray(audio))
            buckets = 600
            if len(mono) < buckets:
                env = mono
            else:
                env = mono[:len(mono) // buckets * buckets].reshape(buckets, -1).max(axis=1)
            peak = float(env.max()) or 1.0
            self._wave_env = (env / peak).tolist()
        except Exception:
            self._wave_env = None
        self._wave_pos = 0.0
        self._draw_wave()

    def _draw_wave(self):
        """Draw the filled amplitude graph; the portion before the playhead is
        drawn in the accent colour, the rest dim."""
        c = getattr(self, "wave", None)
        if c is None:
            return
        c.delete("all")
        w = max(1, c.winfo_width()); h = int(c.winfo_height())
        mid = h / 2
        env = self._wave_env
        if not env:
            return
        n = len(env)
        played_x = self._wave_pos * w
        # one (x, top, bottom) per sample bucket
        cols = []
        for i, a in enumerate(env):
            x = (i / (n - 1) * w) if n > 1 else 0.0
            amp = a * (mid - 2)
            cols.append((x, mid - amp, mid + amp))
        def poly(lo, hi, color):
            seg = [col for col in cols if lo <= col[0] <= hi]
            if len(seg) < 2:
                return
            pts = [(x, t) for x, t, b in seg] + [(x, b) for x, t, b in reversed(seg)]
            flat = [v for xy in pts for v in xy]
            c.create_polygon(flat, fill=color, outline="")
        poly(0, w, "#c2c2c2")                 # full envelope, soft grey (unplayed)
        if played_x > 0:
            poly(0, played_x, "#2d7dff")      # played portion, accent blue
        c.create_line(played_x, 0, played_x, h, fill="#1a1a1a")

    def _draw_vol(self):
        """Draw a horizontal triangle (thin at left, tall at right); fill it blue up
        to the current level over a grey base — same palette as the waveform."""
        c = getattr(self, "vol", None)
        if c is None:
            return
        c.delete("all")
        w = max(1, c.winfo_width()); h = int(c.winfo_height())
        base = h - 2
        # full triangle outline shape: (0,base) → (w,base) → (w, top)
        def tri(x_to, color):
            if x_to <= 0:
                return
            y_at = base - (x_to / w) * (base - 2)   # height of the hypotenuse at x_to
            pts = [0, base, x_to, base, x_to, y_at]
            c.create_polygon(pts, fill=color, outline="")
        tri(w, "#c2c2c2")                       # full triangle, grey
        tri(self.vol_level * w, "#2d7dff")      # filled portion, blue
        # thin level marker
        x = self.vol_level * w
        c.create_line(x, base, x, base - (x / w) * (base - 2), fill="#1a1a1a")

    def _vol_set_from_event(self, e):
        w = max(1, self.vol.winfo_width())
        self.vol_level = min(1.0, max(0.0, e.x / w))
        self._draw_vol()
        self._on_volume(self.vol_level * 100.0)

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
                        self._wave_pos = pos / dur
                        self._draw_wave()
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
        ext = {"FLAC": ".flac", "WAV": ".wav", "OGG": ".ogg", "MP3": ".mp3"}.get(fmt, ".flac")
        ff = ffmpeg_path() if fmt in ("OGG", "MP3", "FLAC") else None
        if fmt in ("OGG", "MP3") and not ff:
            self.log(f"{fmt} selected but ffmpeg isn't on PATH — aborting. Install ffmpeg "
                     "or pick FLAC/WAV.")
            self.after(0, lambda: button.config(state="normal"))
            return
        if fmt == "MP3":
            self.log("MP3 export: 320 kbps (ffmpeg).")
        if fmt == "FLAC":
            self.log("FLAC compression: level 8 (ffmpeg)" if ff
                     else "FLAC compression: level 5 (default; install ffmpeg for level 8)")
        try:
            # Global 'whole queue' art takes priority. If it's set, every song uses
            # it. If not, each song falls back to its own per-song folder pick (the
            # 'art' key on the queue item).
            global_cover = None
            art_path = self.art_path
            if getattr(self, "_art_black", False):
                global_cover = _black_cover_bytes()
            elif art_path and os.path.isfile(art_path) and _PIL_OK:
                try:
                    global_cover, csize, cq = process_cover(art_path)
                    self.log(f"Album art ready ({csize[0]}x{csize[1]}, {len(global_cover)//1024} KB).")
                except Exception as e:
                    self.log(f"Album art could not be processed, rendering without it: {e}")
                    global_cover = None

            def cover_for(item):
                if global_cover is not None:
                    return global_cover
                if item.get("_black_art"):
                    return _black_cover_bytes()
                p = item.get("art")
                # saved art missing (e.g. a shared playlist on another PC)? fall back
                # to the song's own folder art so it still gets a cover, never crashes
                if (not p or not os.path.isfile(p)) and item.get("_fallback_art"):
                    p = pick_discovery_art(item["_fallback_art"])
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
                    job = (p, o, f, it["tags"], cover_for(it), ff,
                           bms_core._LIBRARY_ROOT)
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

