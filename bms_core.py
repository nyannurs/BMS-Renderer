"""
bms_core.py -- pure logic for BMS Renderer (no GUI).

Everything here is importable and testable without Tkinter, audio hardware, or a
display: the BMS parser, the render engine, the SQLite cache, config, playlists,
difficulty-table fetching, tag writing, and the parallel-render worker. The GUI
application (bms_renderer.py) imports from this module, so there is a single
source of truth shared by the app and the test suite.
"""

import os, re, sys, json, io, traceback
from math import gcd
import numpy as np
import soundfile as sf
from mutagen.flac import FLAC, Picture
try:
    from PIL import Image
    _PIL_OK = True
except Exception:
    _PIL_OK = False
_SCIPY_OK = None          # tri-state: None = not yet probed, True/False after first use
resample_poly = None
def _ensure_scipy():
    """Import scipy lazily — it's a slow import and is only needed when a keysound
    has to be resampled, not at startup. Returns True if available."""
    global _SCIPY_OK, resample_poly
    if _SCIPY_OK is None:
        try:
            from scipy.signal import resample_poly as _rp
            resample_poly = _rp
            _SCIPY_OK = True
        except Exception:
            _SCIPY_OK = False
    return _SCIPY_OK
try:
    from player import Player, SD_OK as _SD_OK
except Exception:
    Player = None
    _SD_OK = False

SR = 44100
BMS_EXTS = (".bms", ".bme", ".bml", ".pms")
AUDIO_EXTS = (".ogg", ".wav", ".flac", ".mp3", ".aiff", ".aif")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")

def list_folder_images(folder, include_bmp=False):
    """Return image file paths in `folder`, sorted by name. When include_bmp is
    False, .bmp files are skipped (BGA frame-bmps can number in the hundreds)."""
    exts = IMAGE_EXTS if include_bmp else tuple(e for e in IMAGE_EXTS if e != ".bmp")
    if not folder:
        return []
    folder = os.path.normpath(folder)   # tidy separators / relative bits
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    for n in names:
        full = os.path.join(folder, n)
        if n.lower().endswith(exts) and os.path.isfile(full):
            out.append(full)
    return out

def read_stagefile(bms_path):
    """Quickly scan a chart's header for #STAGEFILE and return the filename
    (as written in the chart) or None."""
    try:
        text = read_bms_text(bms_path)
    except Exception:
        return None
    for line in text.splitlines():
        ls = line.strip()
        if ls.upper().startswith("#STAGEFILE"):
            val = ls[len("#STAGEFILE"):].strip()
            return val or None
    return None

def pick_playable_chart(charts, lo=100, hi=500):
    """Pick a chart for casual playback from a song's chart list (dicts with a
    'notes' field): prefer the lowest notecount within [lo, hi]; if none, expand
    the upper range (lowest notecount >= lo); if nothing reaches lo, fall back to
    the chart with the most notes. Returns None for an empty list."""
    if not charts:
        return None
    def n(c):
        try:
            return int(float(c.get("notes") or 0))
        except (TypeError, ValueError):
            return 0
    in_range = [c for c in charts if lo <= n(c) <= hi]
    if in_range:
        return min(in_range, key=n)
    above = [c for c in charts if n(c) >= lo]
    if above:
        return min(above, key=n)        # expand upper range: smallest >= lo
    return max(charts, key=n)           # everything is tiny -- take the biggest

def pick_discovery_art(bms_path, rng=None):
    """Choose an image to represent a song for the Discovery grid.
    Fallback chain: the chart's #STAGEFILE if that file exists -> a random
    non-.bmp image in the folder -> a random .bmp (BGA frame). Returns a path
    or None if the folder has no images at all."""
    import random as _random
    rng = rng or _random
    folder = os.path.dirname(bms_path)
    stage = read_stagefile(bms_path)
    if stage:
        cand = os.path.normpath(os.path.join(folder, stage))
        if os.path.isfile(cand):
            return cand
        # charts often name a stagefile with the wrong extension -- try swaps
        base = os.path.splitext(cand)[0]
        for ext in IMAGE_EXTS:
            if os.path.isfile(base + ext):
                return base + ext
    nonbmp = list_folder_images(folder, include_bmp=False)
    if nonbmp:
        return rng.choice(nonbmp)
    bmps = [p for p in list_folder_images(folder, include_bmp=True)
            if p.lower().endswith(".bmp")]
    if bmps:
        return rng.choice(bmps)
    return None

# Embedded window icon (64px PNG, base64). No external file needed.
_ICON_PNG_B64 = (
    "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"
)

import shutil as _shutil
def ffmpeg_path():
    """Return the path to ffmpeg if available on PATH, else None.
    OGG export uses ffmpeg to avoid libsndfile's unreliable Vorbis encoder."""
    return _shutil.which("ffmpeg")

def _no_window_kwargs():
    """Return subprocess kwargs that prevent a console window from flashing on
    Windows (e.g. when calling ffmpeg). No-op on other platforms."""
    if sys.platform == "win32":
        import subprocess
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        # CREATE_NO_WINDOW also keeps console subprocesses fully hidden
        return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}
    return {}

# Cache lives next to the program, NEVER in the library.
# (sys.argv[0] is this script; if frozen to .exe, sys.executable's folder.)
def program_dir():
    # Resolve the real folder this program lives in, independent of the
    # current working directory (which can differ when double-clicked or
    # launched from another folder). realpath also resolves any symlinks.
    if getattr(sys, "frozen", False):           # packaged .exe
        return os.path.dirname(os.path.realpath(sys.executable))
    try:
        return os.path.dirname(os.path.realpath(__file__))
    except NameError:
        return os.path.dirname(os.path.realpath(sys.argv[0]))

CACHE_PATH = os.path.join(program_dir(), "bms_cache.json")   # legacy, removed on migrate
DB_PATH = os.path.join(program_dir(), "bms_cache.db")
CONFIG_PATH = os.path.join(program_dir(), "bms_config.json")
TABLES_PATH = os.path.join(program_dir(), "tables.json")
PLAYLISTS_PATH = os.path.join(program_dir(), "playlists.json")   # legacy, migrated
PLAYLISTS_DIR = os.path.join(program_dir(), "Playlists")

APP_VERSION = "1.9.14"
CHANGELOG = []

# ============================================================================
#  LIBRARY PROTECTION  --  this section guarantees the library is never written
# ============================================================================
#
#  Two rules, enforced in code:
#    1. Every file inside the library is opened READ-ONLY ("rb"). There is no
#       code path anywhere in this program that opens a library file for writing.
#    2. Before ANY file is written (the FLAC export), assert_safe_output() checks
#       that the destination is NOT inside the library root. If it is, the write
#       is refused and nothing happens to your library.
#
#  These are belt-and-suspenders: even a future bug or a mis-click in the save
#  dialog cannot land a write inside the protected folder.

_LIBRARY_ROOT = None   # set once when you choose your library folder

def set_library_root(path):
    global _LIBRARY_ROOT
    _LIBRARY_ROOT = os.path.realpath(path) if path else None

def _is_inside(child, parent):
    """True if `child` is the same as, or nested under, `parent`. Case-insensitive
    on Windows. Uses realpath so symlinks/.. tricks can't slip past."""
    if not parent:
        return False
    c = os.path.realpath(child)
    p = os.path.realpath(parent)
    if os.name == "nt":
        c, p = c.lower(), p.lower()
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False  # different drives -> definitely not inside

def open_readonly(path):
    """The ONLY way this program opens a library file. Read-only, always."""
    return open(path, "rb")

def assert_safe_output(out_path):
    """Refuse to write anywhere inside the protected library. Raises on violation."""
    if _is_inside(out_path, _LIBRARY_ROOT):
        raise PermissionError(
            "BLOCKED: the output path is inside your read-only library.\n"
            f"  library : {_LIBRARY_ROOT}\n  output  : {os.path.realpath(out_path)}\n"
            "Choose an export location OUTSIDE your library. Nothing was written."
        )

# ---------------------------------------------------------------- BMS parsing

def b36(s):
    return int(s, 36)

def read_bms_text(path):
    with open_readonly(path) as f:
        data = f.read()
    for enc in ("shift_jis", "cp932", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")

def parse_bms(path):
    """Parse a BMS chart. Returns a dict with:
      header   : generic #KEY value pairs (TITLE, ARTIST, BPM, ...)
      wav_table: wav-id(int) -> filename
      bpm_table: id(int) -> float  (from #BPMxx, for channel 08)
      stop_table:id(int) -> float  (from #STOPxx, for channel 09)
      lnobj    : set of wav-ids that terminate a long note (silent)
      bars     : measure(int) -> { channel_str -> [payload, ...] }
    Channels are kept as their raw 2-char base-36 strings ('01','11','5A',...).
    """
    header, wav_table = {}, {}
    bpm_table, stop_table = {}, {}
    lnobj = set()
    bars = {}
    has_random = False

    for line in read_bms_text(path).splitlines():
        line = line.strip()
        if not line.startswith("#"):
            continue
        body = line[1:]

        # flag the #RANDOM control family (we don't evaluate it yet, just detect it)
        up = body.upper()
        if (up.startswith("RANDOM") or up.startswith("SETRANDOM") or
                up.startswith("IF") or up.startswith("ELSEIF") or
                up.startswith("ENDIF") or up.startswith("ENDRANDOM")):
            has_random = True

        # channel data: mmmCC:payload  (mmm=measure base-10, CC=channel base-36)
        m = re.match(r"(\d{3})([0-9A-Za-z]{2}):(.*)", body)
        if m:
            measure = int(m.group(1))
            chan = m.group(2).upper()
            payload = m.group(3).strip()
            bars.setdefault(measure, {}).setdefault(chan, []).append(payload)
            continue

        # indexed header arrays: WAVxx / BPMxx / STOPxx
        m = re.match(r"(WAV|BPM|STOP)([0-9A-Za-z]{2})\s+(.+)", body, re.IGNORECASE)
        if m:
            kind = m.group(1).upper()
            idx = b36(m.group(2).upper())
            val = m.group(3).strip()
            if kind == "WAV":
                wav_table[idx] = val
            elif kind == "BPM":
                try: bpm_table[idx] = float(val)
                except ValueError: pass
            elif kind == "STOP":
                try: stop_table[idx] = float(val)
                except ValueError: pass
            continue

        # generic header: KEY value
        m = re.match(r"([A-Za-z]+)\s+(.+)", body)
        if m:
            key = m.group(1).upper()
            val = m.group(2).strip()
            header[key] = val
            if key == "LNOBJ":
                try: lnobj.add(b36(val.upper()))
                except ValueError: pass
            continue

    return {"header": header, "wav_table": wav_table, "bpm_table": bpm_table,
            "stop_table": stop_table, "lnobj": lnobj, "bars": bars,
            "is_pms": path.lower().endswith(".pms"), "has_random": has_random}

# ---- channel classification (mirrors the bmx2wav reference) ----
def _ch(s):  # base-36 value of a 2-char channel string
    return int(s, 36)

def _num(v):
    """Coerce a possibly-blank, possibly-string value to a float for sorting.
    Blanks sort as -1 so empty BPM/notes group at the bottom ascending."""
    try:
        return float(v)
    except (ValueError, TypeError):
        return -1.0

_C_BGM       = _ch("01")
_C_BPM       = _ch("03")   # inline hex BPM
_C_BPM_EXT   = _ch("08")   # extended BPM (references #BPMxx)
_C_STOP      = _ch("09")   # stop sequence (references #STOPxx)

# Channel-range boundaries, computed once (these were previously recomputed on
# every predicate call, inside the per-note render loop). All inclusive ranges.
_C_P1_LO, _C_P1_HI = _ch("11"), _ch("1Z")   # player-1 visible lanes
_C_P2_LO, _C_P2_HI = _ch("21"), _ch("2Z")   # player-2 visible lanes
_C_LN1_LO, _C_LN1_HI = _ch("51"), _ch("5Z") # player-1 long notes
_C_LN2_LO, _C_LN2_HI = _ch("61"), _ch("6Z") # player-2 long notes
_C_MINE1_LO, _C_MINE1_HI = _ch("D1"), _ch("DZ")  # player-1 landmines
_C_MINE2_LO, _C_MINE2_HI = _ch("E1"), _ch("EZ")  # player-2 landmines

def ch_is_long_note(c):
    v = _ch(c)
    return _C_LN1_LO <= v <= _C_LN1_HI or _C_LN2_LO <= v <= _C_LN2_HI

def ch_is_player_note(c):
    v = _ch(c)
    return (_C_P1_LO <= v <= _C_P1_HI or _C_P2_LO <= v <= _C_P2_HI or
            _C_LN1_LO <= v <= _C_LN1_HI or _C_LN2_LO <= v <= _C_LN2_HI)

def _classify_channel(c):
    """Classify a channel once into the single category the render loop acts on,
    using the SAME precedence the per-subdivision predicate chain used:
      landmine > inline-BPM > ext-BPM > stop > long-note > player-note > BGM.
    Returns one of 'mine','bpm_inline','bpm_ext','stop','long','note','bgm', or
    None for channels the renderer ignores (visual/BGA/etc)."""
    try:
        v = _ch(c)
    except ValueError:
        return None
    if _C_MINE1_LO <= v <= _C_MINE1_HI or _C_MINE2_LO <= v <= _C_MINE2_HI:
        return "mine"
    if v == _C_BPM:
        return "bpm_inline"
    if v == _C_BPM_EXT:
        return "bpm_ext"
    if v == _C_STOP:
        return "stop"
    if _C_LN1_LO <= v <= _C_LN1_HI or _C_LN2_LO <= v <= _C_LN2_HI:
        return "long"            # long-note channels (start sounds, end muted)
    if _C_P1_LO <= v <= _C_P1_HI or _C_P2_LO <= v <= _C_P2_HI:
        return "note"            # visible player lanes (subject to #LNOBJ muting)
    if v == _C_BGM:
        return "bgm"             # autoplay BGM lane
    return None

def count_playable_notes(parsed):
    """Count playable notes the way difficulty is usually rated:
       objects in player lanes (11-1Z, 21-2Z) + long-note channels (51-5Z, 61-6Z),
       excluding BGM/autoplay and control channels. Long notes count as ONE note
       per head/tail pair, and #LNOBJ terminators are not counted.
       `parsed` is the dict returned by parse_bms()."""
    bars = parsed["bars"]
    lnobj = parsed.get("lnobj", set())
    total = 0
    ln_open = {}  # channel -> bool, tracks an open long note so we count the pair once
    for measure in sorted(bars.keys()):
        bar = bars[measure]
        for chan, payloads in bar.items():
            if not ch_is_player_note(chan):
                continue
            is_ln = ch_is_long_note(chan)
            for payload in payloads:
                pairs = [payload[i:i+2] for i in range(0, len(payload) - len(payload) % 2, 2)]
                for pair in pairs:
                    if pair == "00" or pair == "":
                        continue
                    try:
                        wid = b36(pair.upper())
                    except ValueError:
                        continue
                    if is_ln:
                        # head starts the note (count it); tail closes it (don't double-count)
                        if not ln_open.get(chan):
                            ln_open[chan] = True
                            total += 1
                        else:
                            ln_open[chan] = False
                    else:
                        if wid in lnobj:
                            continue  # LNOBJ terminator: not a separate note
                        total += 1
    return total

def detect_mode_from_bars(parsed):
    """Infer play type from the parsed chart's channel usage.
    Returns '7K SP', '5K SP', 'DP', 'PMS', or '?'.
    Player-1 keys live in 11-19 (18/19 = keys 6/7) with LN variants 51-59;
    player-2 side 21-29 / 61-69 means double play."""
    if parsed.get("is_pms"):
        return "PMS"
    p1 = set(); p2 = False
    for chs in parsed["bars"].values():
        for ch in chs:
            if ch in ("11","12","13","14","15","16","18","19",
                      "51","52","53","54","55","56","58","59"):
                p1.add(ch)
            if ch in ("21","22","23","24","25","26","28","29",
                      "61","62","63","64","65","66","68","69"):
                p2 = True
    if p2:
        return "DP"
    if not p1:
        return "?"
    if p1 & {"18","19","58","59"}:
        return "7K SP"
    return "5K SP"


# ============================================================================
#  LIBRARY CACHE  --  fast relaunch via path + size + mtime
# ============================================================================
#
#  The cache stores, per chart: size, mtime, and parsed header fields.
#  On scan we list the library (cheap, no file reads) and only re-parse charts
#  whose size or mtime differ from the cache, or that are brand new. Charts that
#  vanished from disk are dropped. This makes relaunches near-instant and picks
#  up newly added BMS automatically.

import sqlite3

def db_connect():
    """Open (creating if needed) the SQLite cache database, with the charts table.
    If an existing table has an outdated column set, it's dropped and rebuilt."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")      # faster, safer concurrent writes
    conn.execute("PRAGMA synchronous=NORMAL")
    expected = ["path","size","mtime","title","artist","genre","bpm","mode",
                "notes","random","md5"]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(charts)")]
    if cols and cols != expected:
        conn.execute("DROP TABLE charts")        # schema changed → rebuild from scratch
        conn.commit()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS charts (
            path   TEXT PRIMARY KEY,
            size   INTEGER,
            mtime  INTEGER,
            title  TEXT,
            artist TEXT,
            genre  TEXT,
            bpm    TEXT,
            mode   TEXT,
            notes  INTEGER,
            random INTEGER,
            md5    TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_md5 ON charts(md5)")
    conn.commit()
    return conn

def _row_to_song(r):
    """Turn a DB row tuple into the song dict the rest of the app expects."""
    (path, size, mtime, title, artist, genre, bpm, mode, notes, random, md5) = r
    return {"path": path, "title": title or os.path.basename(path),
            "artist": artist or "", "genre": genre or "", "bpm": bpm or "",
            "mode": mode or "?", "notes": notes if notes is not None else "",
            "random": bool(random), "md5": md5 or "",
            "_sig": (size, mtime)}

# Legacy JSON cache is no longer used; if present we just ignore/remove it.
def _migrate_old_cache():
    try:
        if os.path.exists(CACHE_PATH):
            os.remove(CACHE_PATH)
    except OSError:
        pass

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)

def file_md5(path):
    """MD5 of the raw .bms file bytes — the LR2/BMS-standard chart identifier."""
    import hashlib
    h = hashlib.md5()
    with open_readonly(path) as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def load_tables_file():
    """Read tables.json: a list of {name, url} the user is tracking.
    Returns [] if the file is missing or unreadable."""
    try:
        with open(TABLES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tables", []) if isinstance(data, dict) else data
    except Exception:
        return []

def save_tables_file(tables):
    tmp = TABLES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tables": tables}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, TABLES_PATH)

def _safe_playlist_filename(name):
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad else c for c in name).strip()
    return (cleaned or "untitled") + ".json"

def load_playlists():
    """Load all playlists from the Playlists/ folder — one JSON file per playlist,
    each {"name","songs":[{md5,title,artist}, ...]}. Returns {name: [entries]}.
    Also migrates an old single playlists.json (if present) into the folder."""
    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
    playlists = {}
    # one-time migration from the old single-file format
    if os.path.exists(PLAYLISTS_PATH):
        try:
            with open(PLAYLISTS_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
            for name, songs in (old.get("playlists", {}) if isinstance(old, dict) else {}).items():
                playlists[name] = songs
                save_one_playlist(name, songs)
            os.replace(PLAYLISTS_PATH, PLAYLISTS_PATH + ".migrated")
        except Exception:
            pass
    for fn in os.listdir(PLAYLISTS_DIR):
        if not fn.lower().endswith(".json"):
            continue
        try:
            with open(os.path.join(PLAYLISTS_DIR, fn), "r", encoding="utf-8") as f:
                data = json.load(f)
            name = data.get("name") or os.path.splitext(fn)[0]
            playlists[name] = data.get("songs", [])
        except Exception:
            continue
    return playlists

def save_one_playlist(name, songs):
    """Write a single playlist to its own portable JSON file in Playlists/."""
    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
    path = os.path.join(PLAYLISTS_DIR, _safe_playlist_filename(name))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"name": name, "songs": songs}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)

def delete_playlist_file(name):
    path = os.path.join(PLAYLISTS_DIR, _safe_playlist_filename(name))
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass

def _http_get(url, timeout=20):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "BMS-Renderer"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def _resolve(base, ref):
    import urllib.parse
    return urllib.parse.urljoin(base, ref)

def fetch_table(url, log=lambda s: None):
    """Fetch a BMS difficulty table following the bmstable standard.
    Returns {name, symbol, entries:[{md5,level,title,artist,url}]}.
    The page's meta-bmstable points to a header JSON; the header's data_url
    points to the data JSON (an array of chart entries keyed by md5)."""
    import re as _re, json as _json
    header_url = url
    # If given the HTML page, find <meta name="bmstable" content="header.json">
    if not url.lower().endswith(".json"):
        try:
            html = _http_get(url)
            m = _re.search(r'<meta[^>]+name=["\']bmstable["\'][^>]+content=["\']([^"\']+)["\']',
                           html, _re.IGNORECASE)
            if not m:
                m = _re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']bmstable["\']',
                               html, _re.IGNORECASE)
            if m:
                header_url = _resolve(url, m.group(1))
            else:
                # some tables are linked directly to header.json-style URLs
                header_url = _resolve(url, "header.json")
        except Exception as e:
            log(f"  couldn't read table page: {e}")
            header_url = _resolve(url, "header.json")

    header = _json.loads(_http_get(header_url))
    name = header.get("name", url)
    symbol = header.get("symbol", "")
    data_url = _resolve(header_url, header.get("data_url", "data.json"))
    raw = _json.loads(_http_get(data_url))

    entries = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        md5 = (e.get("md5") or "").lower().strip()
        entries.append({
            "md5": md5,
            "level": str(e.get("level", "")),
            "title": e.get("title", ""),
            "artist": e.get("artist", ""),
            "url": e.get("url", ""),
        })
    return {"name": name, "symbol": symbol, "entries": entries}

def scan_library(root, conn, log=lambda s: None, progress=lambda d, t: None):
    """Incrementally update the SQLite cache for `root`, then return
    (songs list, stats). Only changed/new charts are parsed; vanished charts
    are deleted. `conn` is an open sqlite3 connection."""
    reused = parsed = removed = 0

    # existing signatures, read once: path -> (size, mtime)
    existing = {row[0]: (row[1], row[2])
                for row in conn.execute("SELECT path, size, mtime FROM charts")}

    # cheap pass: every chart path on disk
    disk_paths = []
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith(BMS_EXTS):
                disk_paths.append(os.path.join(dirpath, fn))
    total = len(disk_paths)
    disk_set = set(disk_paths)

    upserts = []
    for i, p in enumerate(disk_paths):
        try:
            st = os.stat(p)
        except OSError:
            continue
        sig = (st.st_size, int(st.st_mtime))
        if existing.get(p) == sig:
            reused += 1
        else:
            try:
                d = parse_bms(p); h = d["header"]
                notes = count_playable_notes(d)
                mode = detect_mode_from_bars(d)
                has_random = 1 if d.get("has_random", False) else 0
            except Exception:
                h, notes, mode, has_random = {}, 0, "?", 0
            try:
                md5 = file_md5(p)
            except Exception:
                md5 = ""
            upserts.append((p, sig[0], sig[1],
                            h.get("TITLE", os.path.basename(p)),
                            h.get("ARTIST", ""), h.get("GENRE", ""),
                            h.get("BPM", ""), mode, notes, has_random, md5))
            parsed += 1
            # commit in batches so a huge first scan isn't one giant transaction
            if len(upserts) >= 500:
                conn.executemany(
                    "INSERT OR REPLACE INTO charts VALUES (?,?,?,?,?,?,?,?,?,?,?)", upserts)
                conn.commit(); upserts.clear()
        if i % 200 == 0:
            progress(i, total)
    if upserts:
        conn.executemany(
            "INSERT OR REPLACE INTO charts VALUES (?,?,?,?,?,?,?,?,?,?,?)", upserts)
        conn.commit()
    progress(total, total)

    # delete charts no longer on disk
    gone = [p for p in existing if p not in disk_set]
    if gone:
        conn.executemany("DELETE FROM charts WHERE path=?", [(p,) for p in gone])
        conn.commit()
        removed = len(gone)

    # build the song list from the DB
    songs = [_row_to_song(r) for r in conn.execute(
        "SELECT path,size,mtime,title,artist,genre,bpm,mode,notes,random,md5 FROM charts")]
    return songs, {"reused": reused, "parsed": parsed, "removed": removed, "total": total}

def find_audio(folder, name):
    """BMS often names a .wav but ships .ogg. Try exact, then swap extensions."""
    exact = os.path.join(folder, name)
    if os.path.exists(exact):
        return exact
    stem = os.path.splitext(name)[0]
    for ext in AUDIO_EXTS:
        cand = os.path.join(folder, stem + ext)
        if os.path.exists(cand):
            return cand
    return None

def process_cover(path, max_px=1000, max_bytes=500_000):
    """Load a JPEG/PNG and return safe-spec JPEG bytes for embedding:
       RGB, at most max_px on the long edge, at most max_bytes.
       Returns (jpeg_bytes, (w,h), quality) or raises on failure."""
    if not _PIL_OK:
        raise RuntimeError("Pillow not installed — album art unavailable. "
                           "Run: pip install Pillow")
    img = Image.open(path)
    img = img.convert("RGB")           # flatten alpha/palette -> always-valid JPEG
    w, h = img.size
    if max(w, h) > max_px:
        scale = max_px / max(w, h)
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    data = None
    for q in (90, 85, 80, 72, 65, 58, 50):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data, img.size, q
    return data, img.size, 50          # smallest achievable, even if still over

def _resample_to_sr(audio, sr):
    """Resample stereo float32 audio from `sr` to the output SR.
    Uses high-quality polyphase resampling (scipy) when available — this is
    objectively better than the linear interpolation bmx2wav uses, with far
    less aliasing. Falls back to linear interpolation (still much better than
    nearest-neighbor) if scipy isn't installed."""
    if sr == SR:
        return audio
    if _ensure_scipy():
        g = gcd(int(SR), int(sr))
        up, down = int(SR) // g, int(sr) // g
        # resample_poly applies an anti-aliasing FIR filter internally
        out = resample_poly(audio, up, down, axis=0).astype(np.float32)
        return out
    # fallback: linear interpolation per channel
    n_out = int(len(audio) * SR / sr)
    xp = np.arange(len(audio))
    xq = np.linspace(0, len(audio) - 1, n_out)
    ch = [np.interp(xq, xp, audio[:, c]) for c in range(audio.shape[1])]
    return np.stack(ch, axis=1).astype(np.float32)

def _decode_clip(ap):
    audio, sr = sf.read(ap, dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    audio = _resample_to_sr(audio, sr)
    return audio

def render_bms(path, log=lambda s: None):
    """Faithful render following the bmx2wav timing model:
       - per-measure native resolution (lcm of all channel lengths in that bar)
       - sample position advances by (SR*60/bpm)/(resolution/4)*ratio per step
       - channel 02 = measure-length ratio
       - channel 03 = inline hex BPM, 08 = extended BPM (#BPMxx)
       - channel 09 = STOP (#STOPxx), freezes time
       - 51-5Z / 61-6Z = long notes: only the START sounds, END is silent
       - #LNOBJ-marked notes in player lanes are silent (they end an LN)
       - D1-DZ / E1-EZ = landmines: never sound
    """
    d = parse_bms(path)
    header, wav_table = d["header"], d["wav_table"]
    bpm_table, stop_table, lnobj, bars = d["bpm_table"], d["stop_table"], d["lnobj"], d["bars"]
    folder = os.path.dirname(path)

    try:
        base_bpm = float(header.get("BPM", 120))
        if base_bpm <= 0:
            base_bpm = 120.0
    except (ValueError, TypeError):
        base_bpm = 120.0

    if not bars:
        raise RuntimeError("no note data found in this BMS")

    max_measure = max(bars.keys())

    # ---- pass 1: schedule events as (sample_position, wav_id) ----
    # We accumulate sample position bar by bar, subdivision by subdivision,
    # exactly like the reference, so BPM/length/STOP all fall out naturally.
    # (Audio is decoded AFTER this, for only the wav-ids that actually sound.)
    schedule = []
    bpm = base_bpm
    sample_pos = 0.0
    open_ln = {}  # channel -> True while a long note is open (so we mute its end)

    for measure in range(max_measure + 1):
        bar = bars.get(measure, {})

        # measure-length ratio (channel 02), default 1.0
        ratio = 1.0
        if "02" in bar:
            try:
                ratio = float(bar["02"][-1])  # last definition wins
                if ratio <= 0:
                    ratio = 1.0
            except (ValueError, IndexError):
                ratio = 1.0

        # resolution = lcm of the lengths (in note-pairs) of every channel here.
        # We also classify each channel ONCE here (kind + base-36 value) instead of
        # re-deriving it for every subdivision below — that inner work is the hot path.
        resolution = 1
        parsed_channels = {}  # channel -> (kind, [pairs_line, ...])
        for chan, payloads in bar.items():
            if chan == "02":
                continue
            kind = _classify_channel(chan)
            if kind is None:
                continue  # not a channel we act on (e.g. BGA/visual lanes)
            for payload in payloads:
                pairs = [payload[i:i+2] for i in range(0, len(payload) - len(payload) % 2, 2)]
                n = len(pairs)
                if n == 0:
                    continue
                resolution = resolution * n // gcd(resolution, n)
                if chan not in parsed_channels:
                    parsed_channels[chan] = (kind, [])
                parsed_channels[chan][1].append(pairs)

        # advance over each subdivision of the bar
        for step in range(resolution):
            stop_here = 0.0
            for chan, (kind, lines) in parsed_channels.items():
                for pairs in lines:
                    n = len(pairs)
                    # does this channel place an object at this subdivision?
                    if (step * n) % resolution != 0:
                        continue
                    pair = pairs[(step * n) // resolution]
                    if pair == "00" or pair == "":
                        continue
                    try:
                        wid = b36(pair.upper())
                    except ValueError:
                        continue

                    if kind == "mine":
                        continue  # landmines never sound
                    if kind == "bpm_inline":
                        try:
                            bpm = float(int(pair.upper(), 16))
                        except ValueError:
                            pass
                        continue
                    if kind == "bpm_ext":
                        if wid in bpm_table:
                            bpm = bpm_table[wid]
                        continue
                    if kind == "stop":
                        stop_here = stop_table.get(wid, 0.0)
                        continue

                    # kind is a sounding channel ("bgm", "note", or "long")
                    if kind == "long":
                        # long-note start/end pairing: mute the closing note
                        if not open_ln.get(chan):
                            open_ln[chan] = True   # START -> sounds
                        else:
                            open_ln[chan] = False  # END -> silent
                            continue
                    elif kind == "note" and wid in lnobj:
                        continue  # #LNOBJ terminator in a player lane is silent
                    schedule.append((sample_pos, wid))

            # advance sample position for this subdivision
            if bpm <= 0:
                bpm = base_bpm
            sample_pos += (SR * 60.0 / bpm) / (resolution / 4.0) * ratio
            if stop_here != 0.0:
                sample_pos += (SR * 60.0 / bpm) * (stop_here / 192.0 * 4.0)

    if not schedule:
        raise RuntimeError("no audible notes found in this BMS")

    # ---- decode only the clips that actually sound, in parallel ----
    # (decoding dominates render time; each clip is independent and soundfile
    # releases the GIL, so a thread pool gives a real speedup. Skipping unused
    # #WAV definitions avoids reading files the chart never plays.)
    used_ids = {wid for _, wid in schedule}
    clips, missing = {}, 0
    to_decode = []
    for wid in used_ids:
        fn = wav_table.get(wid)
        if not fn:
            continue
        ap = find_audio(folder, fn)
        if not ap:
            missing += 1
            continue
        to_decode.append((wid, fn, ap))
    if to_decode:
        from concurrent.futures import ThreadPoolExecutor
        def _decode_one(item):
            wid, fn, ap = item
            try:
                return wid, _decode_clip(ap), None
            except Exception as e:
                return wid, None, f"{fn}: {e}"
        workers = min(8, (os.cpu_count() or 4) * 2, len(to_decode))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for wid, clip, err in ex.map(_decode_one, to_decode):
                if err:
                    log(f"  could not read {err}")
                else:
                    clips[wid] = clip
    if missing:
        log(f"  note: {missing} referenced sounds not found in folder")

    # ---- pass 2: mix (vectorized) ----
    # Group note onsets by wav-id so each distinct clip is added in as few numpy
    # operations as possible. Output is bit-identical to a per-note add loop.
    if not clips:
        # nothing decoded successfully — return a short silence so callers don't crash
        return np.zeros((SR, 2), dtype=np.float32), header

    # total length = furthest (onset + clip length)
    last_end = 0
    for pos, wid in schedule:
        clip = clips.get(wid)
        if clip is not None:
            e = int(pos) + len(clip)
            if e > last_end:
                last_end = e
    out = np.zeros((last_end + SR, 2), dtype=np.float32)  # +1s tail

    # bucket onsets per wav-id
    onsets = {}
    for pos, wid in schedule:
        if wid in clips:
            onsets.setdefault(wid, []).append(int(pos))
    for wid, positions in onsets.items():
        clip = clips[wid]; clen = len(clip)
        for s in positions:
            out[s:s + clen] += clip[:len(out) - s]   # tail-safe slice add

    # gentle normalize if overlaps clipped
    peak = float(np.abs(out).max()) if out.size else 0.0
    if peak > 1.0:
        out /= peak
    return out, header

def write_tags_to_file(path, fmt, tags, cover):
    """Write format-appropriate tags. FLAC/OGG get full tags + cover; WAV basic only.
    Module-level so both the GUI and render worker processes can call it.
    Album and Album Artist are written as given (may be empty)."""
    album = tags.get("Album", "")
    album_artist = tags.get("AlbumArtist", "")
    if fmt == "FLAC":
        f = FLAC(path)
        f["title"] = tags.get("Title",""); f["artist"] = tags.get("Artist","")
        f["album"] = album; f["genre"] = tags.get("Genre","")
        if album_artist: f["albumartist"] = album_artist
        if tags.get("BPM"): f["bpm"] = tags["BPM"]
        if cover:
            pic = Picture(); pic.type = 3; pic.mime = "image/jpeg"
            pic.desc = "Cover"; pic.data = cover
            f.clear_pictures(); f.add_picture(pic)
        f.save()
    elif fmt == "WAV":
        from mutagen.wave import WAVE
        from mutagen.id3 import TIT2, TPE1, TPE2, TALB, TCON
        f = WAVE(path)
        if f.tags is None:
            f.add_tags()
        f.tags.add(TIT2(encoding=3, text=tags.get("Title","")))
        f.tags.add(TPE1(encoding=3, text=tags.get("Artist","")))
        f.tags.add(TALB(encoding=3, text=album))
        if album_artist:
            f.tags.add(TPE2(encoding=3, text=album_artist))
        f.tags.add(TCON(encoding=3, text=tags.get("Genre","")))
        f.save()
    elif fmt == "OGG":
        import base64
        from mutagen.oggvorbis import OggVorbis
        from mutagen.flac import Picture as _Pic
        f = OggVorbis(path)
        f["title"] = tags.get("Title",""); f["artist"] = tags.get("Artist","")
        f["album"] = album; f["genre"] = tags.get("Genre","")
        if album_artist: f["albumartist"] = album_artist
        if tags.get("BPM"): f["bpm"] = tags["BPM"]
        if cover:
            pic = _Pic(); pic.type = 3; pic.mime = "image/jpeg"
            pic.desc = "Cover"; pic.data = cover
            f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        f.save()

    elif fmt == "MP3":
        from mutagen.id3 import (ID3, TIT2, TPE1, TPE2, TALB, TCON, TBPM, APIC,
                                 ID3NoHeaderError)
        try:
            f = ID3(path)
        except ID3NoHeaderError:
            f = ID3()
        f.add(TIT2(encoding=3, text=tags.get("Title","")))
        f.add(TPE1(encoding=3, text=tags.get("Artist","")))
        f.add(TALB(encoding=3, text=album))
        if album_artist:
            f.add(TPE2(encoding=3, text=album_artist))
        f.add(TCON(encoding=3, text=tags.get("Genre","")))
        if tags.get("BPM"):
            f.add(TBPM(encoding=3, text=str(tags["BPM"])))
        if cover:
            f.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        f.save(path)

def _encode_ogg_ffmpeg(ff, audio, out_path):
    import subprocess
    tmp_wav = out_path + ".tmp.wav"
    try:
        sf.write(tmp_wav, audio, SR, format="WAV")
        proc = subprocess.run([ff, "-y", "-i", tmp_wav, "-c:a", "libvorbis", "-q:a", "6",
                               out_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **_no_window_kwargs())
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + proc.stderr.decode("utf-8","replace")[-300:])
    finally:
        if os.path.exists(tmp_wav):
            try: os.remove(tmp_wav)
            except OSError: pass

def _encode_flac_ffmpeg(ff, audio, out_path):
    import subprocess
    tmp_wav = out_path + ".tmp.wav"
    try:
        sf.write(tmp_wav, audio, SR, format="WAV")
        proc = subprocess.run([ff, "-y", "-i", tmp_wav, "-c:a", "flac",
                               "-compression_level", "8", out_path],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **_no_window_kwargs())
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + proc.stderr.decode("utf-8","replace")[-300:])
    finally:
        if os.path.exists(tmp_wav):
            try: os.remove(tmp_wav)
            except OSError: pass

def _encode_mp3_ffmpeg(ff, audio, out_path):
    import subprocess
    tmp_wav = out_path + ".tmp.wav"
    try:
        sf.write(tmp_wav, audio, SR, format="WAV")
        proc = subprocess.run([ff, "-y", "-i", tmp_wav, "-c:a", "libmp3lame",
                               "-b:a", "320k", out_path],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **_no_window_kwargs())
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + proc.stderr.decode("utf-8","replace")[-300:])
    finally:
        if os.path.exists(tmp_wav):
            try: os.remove(tmp_wav)
            except OSError: pass

def render_one_job(job):
    """Render ONE song to its final tagged file. Runs in a worker process.
    `job` = (in_path, out_path, fmt, tags, cover_bytes, ffmpeg, library_root).
    Returns (out_path, title, None) on success or (out_path, title, error_str)."""
    in_path, out_path, fmt, tags, cover, ff, lib_root = job
    title = tags.get("Title", os.path.basename(in_path))
    try:
        set_library_root(lib_root)              # restore guard in this subprocess
        assert_safe_output(out_path)            # never write into the library
        audio, _ = render_bms(in_path)
        assert_safe_output(out_path)
        if fmt == "OGG":
            _encode_ogg_ffmpeg(ff, audio, out_path)
        elif fmt == "MP3":
            _encode_mp3_ffmpeg(ff, audio, out_path)
        elif fmt == "FLAC" and ff:
            _encode_flac_ffmpeg(ff, audio, out_path)
        else:
            sf.write(out_path, audio, SR, format=("WAV" if fmt == "WAV" else "FLAC"))
        write_tags_to_file(out_path, fmt, tags, cover)
        return (out_path, title, None)
    except Exception:
        return (out_path, title, traceback.format_exc())

