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
_SOXR_OK = None           # tri-state: None = not yet probed, True/False after first use
soxr = None
def _ensure_resampler():
    """Lazily probe for the soxr resampler (the SoX resampler — high quality, fast, and
    only ~0.5 MB, bundled into the frozen build). Returns "soxr" if available, else None
    (caller falls back to numpy linear interpolation). Probed once, on the first keysound
    that actually needs resampling — never at startup. NOTE: soxr is the intended
    resampler and is bundled in release builds; the linear fallback exists only for a
    from-source run without soxr installed (it works, just without anti-aliasing)."""
    global _SOXR_OK, soxr
    if _SOXR_OK is None:
        try:
            import soxr as _sx
            soxr = _sx; _SOXR_OK = True
        except Exception:
            _SOXR_OK = False
    return "soxr" if _SOXR_OK else None
try:
    from player import Player, SD_OK as _SD_OK
except Exception:
    Player = None
    _SD_OK = False

SR = 44100
BMS_EXTS = (".bms", ".bme", ".bml", ".pms")
AUDIO_EXTS = (".ogg", ".wav", ".flac", ".mp3", ".aiff", ".aif")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
VIDEO_EXTS = (".mpg", ".mpeg", ".mp4", ".avi", ".wmv", ".webm", ".flv", ".m4v", ".mov", ".mkv")

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
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAF/klEQVR4nO2bXW9URRjHfzPnbLdb"
    "SjEYG4hiIIItFCEWApd6peAdHwES9UrRcAExASsk3nFlhBh5SfgOgPGWqxIsAi1tQ01ETCA1ELus"
    "lN0954wXM7Pn9LSldDtn+7L8k8lmz5zzzPN/5v2ZeQTuIE0KEs9ywHZgD/A+0AO8BbwOrE59/xR4"
    "DDwA7gI3gX5gCKgm3vOByKQlAQl4if8F4BPgLFr5CqDqTBUj46yR2ZYoxzNlLxoEU4lvAk4CY0wn"
    "EqJbRoiuudkIR6l30/ljpoxNiXI9o0tDkbT8RuBHoMhUwlXmJjxXilKy7POiKXPjLDplCt/85oBj"
    "wJOEYmlFXSdrDPv/idEhl9ItEySb/F7gOlOJL6Sm62kZSUNcNzpBRl1CJIR+DZQXifiLDFE2uqX1"
    "XTBs3/KBc4mCg0Uknk4BcUWcI+4KCx4XrIA24DJLo9ZfpjVcJp4y6zaCRDejNuCaEbyQOb1Ryep4"
    "zegu6jGCHfA84MoyIp82wpUEj3mNCbYPnV+G5NNGOJ/iNCfsVHd4GZNPG+FwitussH1ll/k4Obou"
    "x2RnqwrQm+I4DbbftwC/GwFZruoalSyHm4bbrOOBbR7HzAfJldZyT5bL0RTXGuyU9yYwwcI3MUst"
    "2U3VhOE4bWq0FjmTsthKSpbTmRTnmiU2ACVWXu2nW0HJcAWQ1o0F8BmwyrzYcAdDAyDQ3FahuYLh"
    "LtBurPtoSzVg5BdKCKmE8EySCkQjWoHl9ifQSqKi9zWCvCU9e741RkOMsA/i5eEB4n6SiWtJCA+l"
    "QgB8v52O9i7y+TcAKJf/oVgaJQhK097NALaLHwB+8dGLgw+pc9c0NwRCgFIhr63ZSdc7X7Cu8yNW"
    "FTboEQiIIvhv8gGPxn9l9I8f+HfiFkIIlAJdL05hp/wPgBaB9tf3E/vVHEIYIhHbu7/lva3fkPNb"
    "CEKIIoXSDBFCIKXA96AaVLgz/D2DI98hhDTvODcC6CXyHon2peVwftAgDIGIvb0X2L2zD6VylCsB"
    "UaRq+ULoCokiRbkSoFSO3Tv72Nt7AaWiWr5jROjuv0eiWwA4NrMmH9LTdYKtWw7ybLJinvvMTEiY"
    "PHg2WWHrloP0dJ1AqdAYwSks110S2FbTwBEs+TUdPezYdpzn5RApcy9ZhEDKHM/LITu2HWdNR08W"
    "RrCKdEviVZHDdqZFdW8+Qs73TT+ej3iBUoqc79O9+Yh79WJhb0tgrUvJWvkQ329nfed+gpC6ak8I"
    "SRDC+s79+H67mRadjwVrJfEprRPplmxHexeFwjoz4NXTfCVRpCgU1tHR3jVFtgs1ze/qzM7T8vlO"
    "PI/aVFcPlFJ4npaVFRb1eHkpIDMDlMvjhKFe5NQLIQRhqGVlBYm+mQGO1gFK6fVUsTTK5OQjpLS7"
    "0PkiQkrB5OQjiqXRKbJdqGl+n0r0EbNDKITwCIISD8ev4nv1Ka5UhO/Bw/GrBEEJITwyWBI/lsBf"
    "tkx3crWokbHTVIPAdIP5iFcIIagGASNjp92rFwv7WwIjrkvQa3iPieIQt++eojXvEUXWJTe3blFU"
    "pTXvcfvuKSaKQ2Z77HSrYhUZlsCA+eN0lWGNMDR6kuF7F2krtJjn9lR9uk46D9oKLQzfu8jQ6Mks"
    "yEPM9TeJ3gpXcT4jqNpurn/gEDdu9SFElXyLbwZGna/JKaQU5Ft8hKhy41Yf/QOHarvJjHwCAXBd"
    "oB0id4B3ycQjZB0iaqk4RCzHUWCHbQo/AZ+i/WWZXDRaQi6xAH0m8DPwuX34MU3qFAU9ILTSXG7x"
    "+4azsNdcnwOXjEEacAfXDoChSZkMdDPBcruE5uzBq6MxImOJB8Yy0ry40hCiuV1Cc/VItPamPx6H"
    "+Lj4qPloJR2Rz3lBAqZekblpPmiqKzIQN4temvCSlMVKvCb3ZYrbnGjqi5Lw6qos0OSXpZNGgCa9"
    "Lp82QlMGTFgsl5CZr2bQ1xmaOmgqiaYNm0uiqQMnLRYjdPYeSyR0NokXBU8PsvDg6UHi4OlCohwn"
    "wdMuLTdT+LxPHD7fi76PtIGXC58fQA9ugzPIdBY+/z8cfXb0bb4G9QAAAABJRU5ErkJggg=="
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

APP_VERSION = "2.3.8"
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

# Pre-compiled patterns for the per-line parse loop (runs across every line of every
# chart during a library scan — 126k+ charts). Python memoises compiled patterns
# internally, but hoisting the hot ones is explicit and avoids the lookup entirely.
_RE_CHANNEL = re.compile(r"(\d{3})([0-9A-Za-z]{2}):(.*)")
_RE_INDEXED = re.compile(r"(WAV|BPM|STOP)([0-9A-Za-z]{2})\s+(.+)", re.IGNORECASE)
_RE_HEADER  = re.compile(r"([A-Za-z]+)\s+(.+)")


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

    # ---- #RANDOM / #SWITCH control-flow state ----
    # We evaluate control flow deterministically so the audio render and the BGA
    # timeline always pick the SAME branch (a seeded RNG keyed to the file path).
    # Spec block structure (hitkey BMS command memo, <CONTROL FLOW>):
    #   #RANDOM n / #SETRANDOM n   -> choose an integer in [1,n] (SETRANDOM forces n)
    #   #IF n .. [#ELSEIF n ..] [#ELSE ..] #ENDIF   -> branch on the active value
    #   #ENDRANDOM                 -> close the RANDOM scope
    #   #SWITCH n / #SETSWITCH n   -> choose [1,n]; C-style switch with fall-through
    #   #CASE n .. [#SKIP] / #DEF  -> a matching #CASE starts execution and FALLS
    #                                 THROUGH later cases until #SKIP (like break)
    #   #ENDSW                     -> close the SWITCH scope
    # Lines inside a scope but outside any #IF/#CASE always apply. Blocks nest.
    import random as _random
    rng = _random.Random(os.path.basename(path))   # deterministic per chart

    rand_stack = []        # stack of active #RANDOM values
    if_stack = []          # stack of #IF dicts: {"active": bool, "matched": bool}
    # stack of #SWITCH dicts: {"value": int, "active": bool, "matched": bool,
    #                          "skipped": bool}
    sw_stack = []

    def _emitting():
        # emit only if every enclosing #IF and #CASE level is currently active
        return (all(f["active"] for f in if_stack) and
                all(s["active"] for s in sw_stack))

    for line in read_bms_text(path).splitlines():
        line = line.strip()
        if not line.startswith("#"):
            continue
        body = line[1:]
        up = body.upper()

        # ---- control-flow commands (consumed here, never stored) ----
        cf = up.split(None, 1)
        head = cf[0] if cf else ""
        arg = cf[1].strip() if len(cf) > 1 else ""

        if head in ("RANDOM", "SETRANDOM"):
            has_random = True
            try:
                n = int(float(arg))
            except (ValueError, IndexError):
                n = 1
            if n < 1:
                n = 1
            val = n if head == "SETRANDOM" else rng.randint(1, n)
            rand_stack.append(val)
            continue
        if head == "ENDRANDOM":
            if rand_stack:
                rand_stack.pop()
            continue
        if head == "IF":
            has_random = True
            cur = rand_stack[-1] if rand_stack else None
            try:
                want = int(float(arg))
            except (ValueError, IndexError):
                want = None
            match = (cur is not None and want == cur)
            if_stack.append({"active": match, "matched": match})
            continue
        if head == "ELSEIF":
            if if_stack:
                cur = rand_stack[-1] if rand_stack else None
                try:
                    want = int(float(arg))
                except (ValueError, IndexError):
                    want = None
                top = if_stack[-1]
                if top["matched"]:
                    top["active"] = False           # a previous branch already won
                else:
                    top["active"] = (cur is not None and want == cur)
                    if top["active"]:
                        top["matched"] = True
            continue
        if head == "ELSE":
            if if_stack:
                top = if_stack[-1]
                top["active"] = not top["matched"]
                top["matched"] = True
            continue
        if head == "ENDIF" or up.replace(" ", "") == "ENDIF":
            if if_stack:
                if_stack.pop()
            continue
        if head in ("SWITCH", "SETSWITCH"):
            has_random = True
            try:
                n = int(float(arg))
            except (ValueError, IndexError):
                n = 1
            if n < 1:
                n = 1
            val = n if head == "SETSWITCH" else rng.randint(1, n)
            # a new switch starts inactive until a #CASE/#DEF matches
            sw_stack.append({"value": val, "active": False,
                             "matched": False, "skipped": False})
            continue
        if head == "CASE":
            if sw_stack:
                top = sw_stack[-1]
                try:
                    want = int(float(arg))
                except (ValueError, IndexError):
                    want = None
                if top["skipped"]:
                    top["active"] = False          # a #SKIP already closed this switch
                elif top["active"]:
                    pass                           # fall-through: stay active
                elif want is not None and want == top["value"]:
                    top["active"] = True           # this case matches -> start emitting
                    top["matched"] = True
            continue
        if head == "DEF":
            if sw_stack:
                top = sw_stack[-1]
                if top["skipped"]:
                    top["active"] = False
                elif top["active"]:
                    pass                           # fell through into #DEF
                elif not top["matched"]:
                    top["active"] = True           # default branch
                    top["matched"] = True
            continue
        if head == "SKIP":
            if sw_stack and sw_stack[-1]["active"]:   # break out only from a taken case
                sw_stack[-1]["active"] = False
                sw_stack[-1]["skipped"] = True
            continue
        if head == "ENDSW":
            if sw_stack:
                sw_stack.pop()
            continue

        # outside an active branch? skip this line entirely
        if (if_stack or sw_stack) and not _emitting():
            continue

        # channel data: mmmCC:payload  (mmm=measure base-10, CC=channel base-36)
        m = _RE_CHANNEL.match(body)
        if m:
            measure = int(m.group(1))
            chan = m.group(2).upper()
            payload = m.group(3).strip()
            bars.setdefault(measure, {}).setdefault(chan, []).append(payload)
            continue

        # indexed header arrays: WAVxx / BPMxx / STOPxx
        m = _RE_INDEXED.match(body)
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
        m = _RE_HEADER.match(body)
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

def detect_bga(path):
    """Inspect a BMS chart for a BGA, WITHOUT rendering anything. Returns a dict:
        {"type": "sequence" | "static" | "video" | "none",
         "frames": int,        # number of distinct base-layer (ch 04) image changes
         "images": int}        # count of #BMP defs that point to images

    Both the base BGA layer (channel 04) and the overlay/layer channel (07) are
    considered, since some charts drive the animation entirely on layer 07. The
    poor/miss layer (06/0A) is still ignored. 'sequence' means the BGA switches
    images at 2+ points in time (an animation); 'static' means a single image shown
    the whole song; 'video' means at least one referenced BGA file is a video.
    """
    bmp_table = {}            # id(int) -> filename (from #BMPxx)
    for line in read_bms_text(path).splitlines():
        line = line.strip()
        if not line.startswith("#"):
            continue
        m = re.match(r"#BMP([0-9A-Za-z]{2})\s+(.+)", line, re.IGNORECASE)
        if m:
            try:
                bmp_table[b36(m.group(1).upper())] = m.group(2).strip()
            except ValueError:
                pass
    if not bmp_table:
        return {"type": "none", "frames": 0, "images": 0}

    # collect BGA events from the base layer (channel 04) AND the overlay/layer
    # channel (07). Many charts keep a static/empty base and drive the actual
    # animation on layer 07 (composited over the base with black = transparent),
    # so ignoring 07 would wrongly report 'no BGA' for those charts.
    base_objs = []            # list of bmp ids actually placed on the timeline
    for line in read_bms_text(path).splitlines():
        line = line.strip()
        mm = re.match(r"#(\d{3})(04|07):(.*)", line)
        if not mm:
            continue
        payload = mm.group(3).strip()
        for i in range(0, len(payload) - 1, 2):
            obj = payload[i:i+2]
            if obj != "00":
                try:
                    base_objs.append(b36(obj.upper()))
                except ValueError:
                    pass

    # Split the placed events into image vs video by their referenced file. A chart
    # can define BOTH a video BGA and a full PNG fallback sequence (the video for
    # players that decode it, the images for those that don't). Since this renderer
    # renders image sequences, we look at which dominates the TIMELINE rather than
    # bailing the moment a single video object appears.
    img_events = sum(1 for i in base_objs
                     if os.path.splitext(bmp_table.get(i, ""))[1].lower() in IMAGE_EXTS)
    vid_events = sum(1 for i in base_objs
                     if os.path.splitext(bmp_table.get(i, ""))[1].lower() in VIDEO_EXTS)
    image_count = sum(1 for fn in bmp_table.values()
                      if os.path.splitext(fn)[1].lower() in IMAGE_EXTS)

    # a usable image sequence/static wins; only call it 'video' when there's no
    # renderable image timeline (the BGA is genuinely video-only)
    if img_events == 0 and vid_events > 0:
        return {"type": "video", "frames": vid_events, "images": image_count}
    if img_events == 0:
        return {"type": "none", "frames": 0, "images": image_count}
    if img_events >= 2:
        return {"type": "sequence", "frames": img_events, "images": image_count}
    return {"type": "static", "frames": 1, "images": image_count}


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
    """Read the JSON config. Returns {} if the file simply doesn't exist yet.

    If the file EXISTS but won't parse (e.g. a hand-edit introduced invalid JSON like
    a trailing comma), we must NOT silently return {} — the very next save_config would
    then write that empty dict back and wipe every setting. Instead we move the corrupt
    file aside to `bms_config.json.bad` (so its contents aren't lost and the user can
    see something went wrong) and start fresh from there."""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        try:
            os.replace(CONFIG_PATH, CONFIG_PATH + ".bad")
        except Exception:
            pass
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
    import urllib.request, ssl
    req = urllib.request.Request(url, headers={"User-Agent": "BMS-Renderer"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        # some table hosts (e.g. stellabms.xyz) serve expired/self-signed certs;
        # other BMS players accept them, so fall back to an unverified context
        if isinstance(getattr(e, "reason", None), ssl.SSLError) or "CERTIFICATE" in str(e).upper():
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read().decode("utf-8", errors="replace")
        raise

def _resolve(base, ref):
    import urllib.parse
    return urllib.parse.urljoin(base, ref)

def fetch_table(url, log=lambda s: None):
    """Fetch a BMS difficulty table following the bmstable standard.
    Returns {name, symbol, entries:[{md5,level,title,artist,url}]}.
    The page's meta-bmstable points to a header JSON; the header's data_url
    points to the data JSON (an array of chart entries keyed by md5)."""
    import re as _re, json as _json
    url = url.strip()
    if not _re.match(r"^https?://", url, _re.IGNORECASE):
        url = "https://" + url          # user typed "stellabms.xyz/..." with no scheme
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

def _dir_index(folder):
    """Cache a folder's real filenames keyed by lowercase name, so we can resolve a
    referenced filename case-INSENSITIVELY. BMS charts are authored on Windows (a
    case-insensitive filesystem) and routinely reference 'BD_000.wav' when the file
    on disk is 'bd_000.ogg'; on a case-sensitive Linux filesystem an exact-case
    os.path.exists() check misses, so every such keysound goes silent and the render
    comes out empty. This index lets us match regardless of case.

    Cached by folder path for the lifetime of the process. A song folder is static
    during a render, and find_audio/find_image only consult this index as a fallback
    (after exact + extension-swap miss), so re-listing every miss would be wasteful.
    Call clear_dir_index_cache() if folder contents may have changed."""
    index = _dir_index._cache.get(folder)
    if index is None:
        try:
            index = {n.lower(): n for n in os.listdir(folder)}
        except OSError:
            index = {}
        _dir_index._cache[folder] = index
    return index
_dir_index._cache = {}


def clear_dir_index_cache():
    """Drop the cached directory listings (call if a song folder changed on disk)."""
    _dir_index._cache.clear()


def _resolve_ci(folder, name):
    """Return the real on-disk path for `name` in `folder`, matched case-insensitively;
    None if absent."""
    real = _dir_index(folder).get(name.lower())
    return os.path.join(folder, real) if real else None


def find_audio(folder, name):
    """BMS often names a .wav but ships .ogg, and is authored case-insensitively.
    Try exact, then swap extensions, each first case-sensitively (fast path) then
    case-insensitively (Linux/macOS correctness)."""
    exact = os.path.join(folder, name)
    if os.path.exists(exact):
        return exact
    stem = os.path.splitext(name)[0]
    for ext in AUDIO_EXTS:
        cand = os.path.join(folder, stem + ext)
        if os.path.exists(cand):
            return cand
    # case-insensitive fallback: exact name, then each candidate extension
    hit = _resolve_ci(folder, name)
    if hit:
        return hit
    for ext in AUDIO_EXTS:
        hit = _resolve_ci(folder, stem + ext)
        if hit:
            return hit
    return None

def find_image(folder, name):
    """BGA often names a .bmp but ships .png (or vice versa), and is authored
    case-insensitively. Exact, then extension swap, then case-insensitive fallback."""
    exact = os.path.join(folder, name)
    if os.path.exists(exact):
        return exact
    stem = os.path.splitext(name)[0]
    for ext in IMAGE_EXTS:
        cand = os.path.join(folder, stem + ext)
        if os.path.exists(cand):
            return cand
    hit = _resolve_ci(folder, name)
    if hit:
        return hit
    for ext in IMAGE_EXTS:
        hit = _resolve_ci(folder, stem + ext)
        if hit:
            return hit
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
    """Resample stereo float32 audio from `sr` to the output SR using soxr (SoX
    resampler — high quality, anti-aliased). Falls back to plain linear interpolation
    (no anti-aliasing, but still fine) only if soxr isn't installed — release builds
    always bundle soxr."""
    if sr == SR:
        return audio
    if _ensure_resampler() == "soxr":
        return soxr.resample(audio, sr, SR).astype(np.float32)
    # fallback (from-source run without soxr): linear interpolation per channel
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
        # Decoding is I/O-bound (reading many small keysound files), not CPU-bound, so
        # we can run more concurrent reads than cores to hide per-file disk latency —
        # this is the dominant cost on a FIRST (cold-cache) render of a song, where the
        # OS hasn't cached the files yet. Capped at 16 (diminishing returns past that,
        # and it bounds open file handles). Warm renders are fast regardless.
        workers = min(16, max(4, (os.cpu_count() or 4) * 2), len(to_decode))
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

    # Single pass over the schedule: bucket note onsets per wav-id AND track the
    # furthest sample any clip reaches. A chart often places the SAME keysound on both
    # the BGM autoplay channel (01) AND a player lane at the SAME instant — in-game the
    # player either hits the note OR it autoplays, never both, so it sounds once. A
    # renderer has no player, so without de-duping we'd play both copies and that note
    # would be 2x as loud. Collapsing identical (position, wav) pairs fixes that spurious
    # doubling while leaving genuine musical layering (different sounds, or the same sound
    # at different times) untouched.
    onsets = {}
    last_end = 0
    for pos, wid in schedule:
        clip = clips.get(wid)
        if clip is None:
            continue
        p = int(pos)
        bucket = onsets.get(wid)
        if bucket is None:
            bucket = onsets[wid] = set()
        if p not in bucket:
            bucket.add(p)
            e = p + len(clip)
            if e > last_end:
                last_end = e
    out = np.zeros((last_end + SR, 2), dtype=np.float32)  # +1s tail

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

def _encode_ogg_ffmpeg(ff, audio, out_path, quality=None):
    import subprocess
    # default preserves the original -q:a 6; a quality dict can raise/lower it (VBR)
    q = "6"
    if quality and quality.get("ogg_q") is not None:
        q = str(quality["ogg_q"])
    tmp_wav = out_path + ".tmp.wav"
    try:
        sf.write(tmp_wav, audio, SR, format="WAV")
        proc = subprocess.run([ff, "-y", "-i", tmp_wav, "-c:a", "libvorbis", "-q:a", q,
                               out_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **_no_window_kwargs())
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + proc.stderr.decode("utf-8","replace")[-300:])
    finally:
        if os.path.exists(tmp_wav):
            try: os.remove(tmp_wav)
            except OSError: pass

def _encode_flac_ffmpeg(ff, audio, out_path, quality=None):
    import subprocess
    lvl = "8"                                    # default unchanged
    if quality and quality.get("flac_level") is not None:
        lvl = str(max(0, min(12, int(quality["flac_level"]))))
    tmp_wav = out_path + ".tmp.wav"
    try:
        sf.write(tmp_wav, audio, SR, format="WAV")
        proc = subprocess.run([ff, "-y", "-i", tmp_wav, "-c:a", "flac",
                               "-compression_level", lvl, out_path],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **_no_window_kwargs())
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + proc.stderr.decode("utf-8","replace")[-300:])
    finally:
        if os.path.exists(tmp_wav):
            try: os.remove(tmp_wav)
            except OSError: pass

def _encode_mp3_ffmpeg(ff, audio, out_path, quality=None):
    import subprocess
    tmp_wav = out_path + ".tmp.wav"
    # default preserves 320k CBR; a quality dict switches to VBR at a target bitrate
    enc = ["-c:a", "libmp3lame", "-b:a", "320k"]
    if quality and quality.get("mp3_vbr_q") is not None:
        enc = ["-c:a", "libmp3lame", "-q:a", str(quality["mp3_vbr_q"])]
    try:
        sf.write(tmp_wav, audio, SR, format="WAV")
        proc = subprocess.run([ff, "-y", "-i", tmp_wav] + enc + [out_path],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **_no_window_kwargs())
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + proc.stderr.decode("utf-8","replace")[-300:])
    finally:
        if os.path.exists(tmp_wav):
            try: os.remove(tmp_wav)
            except OSError: pass

def bga_timeline(path):
    """Extract the BGA as time-ordered (seconds, image_path) events, using the SAME
    tempo/measure-length/STOP accumulator as render_bms so visuals line up with audio.
    Returns (events, total_seconds, missing, layer_events):
      events       : base layer (channel 04) [(t_seconds, abs_path_or_None), ...]
      total_seconds: length of the chart in seconds (for the final-frame hold)
      missing      : count of referenced base-layer images not found on disk
      layer_events : overlay layer (channel 07), same shape; composited over the base
                     with black treated as transparent. Empty when the chart has no
                     layer BGA.
    Only images are resolved; video-format BGA files resolve to None (skipped)."""
    d = parse_bms(path)
    header, bars = d["header"], d["bars"]
    bpm_table, stop_table = d["bpm_table"], d["stop_table"]
    folder = os.path.dirname(path)

    # #BMPxx table (re-read; parse_bms doesn't keep it)
    bmp_table = {}
    for line in read_bms_text(path).splitlines():
        line = line.strip()
        m = re.match(r"#BMP([0-9A-Za-z]{2})\s+(.+)", line, re.IGNORECASE)
        if m:
            try:
                bmp_table[b36(m.group(1).upper())] = m.group(2).strip()
            except ValueError:
                pass

    try:
        base_bpm = float(header.get("BPM", 120)) or 120.0
    except (ValueError, TypeError):
        base_bpm = 120.0
    if not bars:
        return [], 0.0, 0, []

    max_measure = max(bars.keys())
    events = []           # (sample_pos, bmp_id)  base layer (ch 04)
    layer_events = []     # (sample_pos, bmp_id)  overlay layer (ch 07)
    bpm = base_bpm
    sample_pos = 0.0

    for measure in range(max_measure + 1):
        bar = bars.get(measure, {})
        ratio = 1.0
        if "02" in bar:
            try:
                ratio = float(bar["02"][-1]) or 1.0
            except (ValueError, IndexError):
                ratio = 1.0
        # resolution must account for tempo channels (03/08/09) AND the BGA
        # channel (04) so timing matches render_bms exactly
        resolution = 1
        tracked = {}      # chan -> [pairs,...]
        for chan, payloads in bar.items():
            if chan == "02":
                continue
            if chan not in ("03", "08", "09", "04", "07"):
                continue
            for payload in payloads:
                pairs = [payload[i:i+2] for i in range(0, len(payload) - len(payload) % 2, 2)]
                n = len(pairs)
                if n == 0:
                    continue
                resolution = resolution * n // gcd(resolution, n)
                tracked.setdefault(chan, []).append(pairs)

        for step in range(resolution):
            stop_here = 0.0
            for chan, lines in tracked.items():
                for pairs in lines:
                    n = len(pairs)
                    if (step * n) % resolution != 0:
                        continue
                    pair = pairs[(step * n) // resolution]
                    if pair == "00" or pair == "":
                        continue
                    try:
                        val = b36(pair.upper())
                    except ValueError:
                        continue
                    if chan == "03":                      # inline hex BPM
                        try: bpm = float(int(pair.upper(), 16))
                        except ValueError: pass
                    elif chan == "08":                    # extended BPM
                        if val in bpm_table: bpm = bpm_table[val]
                    elif chan == "09":                    # STOP
                        stop_here = stop_table.get(val, 0.0)
                    elif chan == "04":                    # BGA base layer
                        events.append((sample_pos, val))
                    elif chan == "07":                    # BGA overlay/layer
                        layer_events.append((sample_pos, val))
            if bpm <= 0:
                bpm = base_bpm
            sample_pos += (SR * 60.0 / bpm) / (resolution / 4.0) * ratio
            if stop_here != 0.0:
                sample_pos += (SR * 60.0 / bpm) * (stop_here / 192.0 * 4.0)

    total_seconds = sample_pos / SR

    # resolve bmp ids to image paths (videos → None/skip)
    def _resolve(evlist):
        res, miss = [], 0
        for sp, bid in evlist:
            fn = bmp_table.get(bid)
            img = None
            if fn:
                ext = os.path.splitext(fn)[1].lower()
                if ext in IMAGE_EXTS:
                    img = find_image(folder, fn)
                    if img is None:
                        miss += 1
            res.append((sp / SR, img))
        return res, miss

    out, missing = _resolve(events)
    layer_out, layer_missing = _resolve(layer_events)
    return out, total_seconds, missing, layer_out


def _is_nvenc_args(args):
    """True if the ffmpeg arg list selects an NVENC (hardware) video encoder."""
    return any(isinstance(a, str) and a.endswith("_nvenc") for a in args)


def _bga_encode_args(opts, out_path):
    """Translate a GUI encode-options dict into ffmpeg output args + the real output
    path (the container/extension may change). `opts` keys:
        video: 'default'|'x264'|'x264_nvenc'|'hevc'|'hevc_nvenc'|'av1'|'vp9'
        vbitrate: int kbps
        audio: 'default'|'flac'|'wav'|'ogg'|'mp3'|'aac'|'opus'
        abitrate: int kbps   (lossy)
        flac_level: int 0-8  (flac only)
    Returns (args_list, final_out_path). When opts is None the EXACT original behavior
    is used (H.264 4:2:0 + AAC 192k in mp4)."""
    # ---- default: preserve original behavior exactly ----
    if not opts or (opts.get("video", "default") == "default"
                    and opts.get("audio", "default") == "default"):
        return (["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                 "-c:a", "aac", "-b:a", "192k", "-shortest"], out_path)

    base, _ext = os.path.splitext(out_path)
    # any non-default combination uses MKV for maximum codec compatibility
    final_out = base + ".mkv"

    v = opts.get("video", "default")
    a = opts.get("audio", "default")
    vbr = int(opts.get("vbitrate", 6000))
    abr = int(opts.get("abitrate", 192))

    args = []

    # ---- video ----
    if v == "default":
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast"]
    elif v == "x264":
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
                 "-b:v", f"{vbr}k"]
    elif v == "x264_nvenc":
        args += ["-c:v", "h264_nvenc", "-pix_fmt", "yuv420p", "-preset", "p5",
                 "-b:v", f"{vbr}k"]
    elif v == "hevc":
        args += ["-c:v", "libx265", "-pix_fmt", "yuv420p", "-preset", "medium",
                 "-b:v", f"{vbr}k"]
    elif v == "hevc_nvenc":
        args += ["-c:v", "hevc_nvenc", "-pix_fmt", "yuv420p", "-preset", "p5",
                 "-b:v", f"{vbr}k"]
    elif v == "av1":
        args += ["-c:v", "libaom-av1", "-pix_fmt", "yuv420p", "-b:v", f"{vbr}k",
                 "-cpu-used", "6"]
    elif v == "vp9":
        args += ["-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p", "-b:v", f"{vbr}k"]
    else:
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast"]

    # ---- audio ----
    if a == "default":
        args += ["-c:a", "aac", "-b:a", "192k"]
    elif a == "wav":
        args += ["-c:a", "pcm_s16le"]                       # uncompressed; no bitrate
    elif a == "flac":
        lvl = int(opts.get("flac_level", 8))
        args += ["-c:a", "flac", "-compression_level", str(max(0, min(12, lvl)))]
    elif a == "ogg":
        args += ["-c:a", "libvorbis", "-q:a", str(_vorbis_q(abr))]   # VBR
    elif a == "mp3":
        args += ["-c:a", "libmp3lame", "-q:a", str(_lame_q(abr))]    # VBR
    elif a == "aac":
        args += ["-c:a", "aac", "-b:a", f"{abr}k"]
    elif a == "opus":
        # opus needs 48k; resampling is out of scope, so callers should not offer it
        # at 44.1k — but if it arrives, fall back to aac so we never silently fail
        args += ["-c:a", "aac", "-b:a", f"{abr}k"]
    else:
        args += ["-c:a", "aac", "-b:a", "192k"]

    args += ["-shortest"]
    return (args, final_out)


def _vorbis_q(kbps):
    """Map a target kbps to a libvorbis VBR quality (-q:a, roughly 0..10)."""
    table = [(64, 0), (96, 2), (128, 4), (160, 5), (192, 6), (224, 7),
             (256, 8), (320, 9)]
    for lim, q in table:
        if kbps <= lim:
            return q
    return 10


def _lame_q(kbps):
    """Map a target kbps to a libmp3lame VBR quality (-q:a, 0=best..9). Higher kbps
    -> lower q number -> better quality."""
    table = [(128, 5), (160, 4), (192, 2), (224, 1), (256, 0)]
    for lim, q in table:
        if kbps <= lim:
            return q
    return 0


def render_bga_video_job(job):
    """Render ONE chart to an MP4 with its image-sequence BGA synced to the audio.
    Runs in a worker process. job = (in_path, out_path, ffmpeg, library_root, fps, size).
    Returns (out_path, title, None) on success or (out_path, title, error_str).
    Strategy: render audio with render_bms; build the channel-04 BGA timeline; feed
    raw RGB frames to ffmpeg at a fixed fps while muxing the audio in one pass."""
    # job may carry an optional 8th element: the encode-options dict (None = default),
    # and an optional 9th: the pool's worker count (so per-worker cache caps can be
    # scaled down as workers rise, keeping TOTAL pool cache memory roughly constant —
    # this is what prevents OOM when many workers each render a heavy BGA at once).
    n_workers = 1
    if len(job) >= 8:
        in_path, out_path, ff, lib_root, fps, size, _opts, n_workers = job[:8]
    elif len(job) == 7:
        in_path, out_path, ff, lib_root, fps, size, _opts = job[:7]
    else:
        in_path, out_path, ff, lib_root, fps, size = job
        _opts = None
    n_workers = max(1, int(n_workers or 1))
    title = os.path.basename(in_path)
    try:
        import subprocess
        from PIL import Image
        set_library_root(lib_root)
        assert_safe_output(out_path)
        if not ff:
            raise RuntimeError("ffmpeg is required for BGA video export")

        # 1) audio
        audio, _ = render_bms(in_path)
        tmp_wav = out_path + ".tmp.wav"
        sf.write(tmp_wav, audio, SR, format="WAV")
        audio_seconds = len(audio) / SR

        # 2) BGA timeline (image events in seconds)
        events, bga_seconds, _missing, layer_events = bga_timeline(in_path)
        duration = max(audio_seconds, bga_seconds)

        # 3) choose output dimensions from the BGA's OWN aspect ratio so the image
        #    fills the frame with no letterboxing and no cropping. We don't trust the
        #    FIRST image (it's often a wide title/loading card while the real animated
        #    BGA is square). Instead we measure every distinct BGA image and pick the
        #    dimensions that dominate the timeline (most events use them).
        target = max(size)
        from collections import Counter
        dim_time = Counter()           # (w,h) -> number of events using it
        dim_seen = {}                  # path -> (w,h), measured once
        for _t, p in list(events) + list(layer_events):
            if not p:
                continue
            if p not in dim_seen:
                try:
                    with Image.open(p) as _im:
                        dim_seen[p] = _im.size
                except Exception:
                    dim_seen[p] = None
            wh = dim_seen[p]
            if wh and wh[0] > 0 and wh[1] > 0:
                dim_time[wh] += 1
        if dim_time:
            iw, ih = dim_time.most_common(1)[0][0]    # the dominant frame size
        else:
            iw, ih = target, target

        # The REQUESTED output size keeps the BGA's aspect ratio inside `target`.
        if iw >= ih:
            req_W = target; req_H = max(2, round(target * ih / iw))
        else:
            req_H = target; req_W = max(2, round(target * iw / ih))
        req_W -= req_W % 2; req_H -= req_H % 2          # H.264 needs even dims

        # --- change A: never UPSCALE inside PIL --------------------------------
        # Per-image BICUBIC upscaling is the dominant cost of the whole pipeline,
        # and it scales with OUTPUT pixels: a 512px BGA exported at 1080p paid ~8x
        # the per-image time for blur it can't add real detail to. So we composite
        # at a WORKING size capped to the dominant source resolution, then let
        # ffmpeg upscale the finished stream to the requested size exactly ONCE
        # (one GPU/CPU-cheap pass over the encoded video) instead of upscaling
        # every distinct frame in Python. When the request is already <= source
        # (downscale or equal), the working size IS the requested size and nothing
        # changes — downscaling stays in PIL where BOX gives clean averaging.
        cap = max(2, max(iw, ih))               # longest dominant source edge
        work_target = min(target, cap)
        if iw >= ih:
            W = work_target; H = max(2, round(work_target * ih / iw))
        else:
            H = work_target; W = max(2, round(work_target * iw / ih))
        W -= W % 2; H -= H % 2
        # only ask ffmpeg to rescale if the working size differs from requested
        upscale_to = (req_W, req_H) if (req_W, req_H) != (W, H) else None
        black = Image.new("RGB", (W, H), (0, 0, 0))

        def _resample(up):
            # bicubic when scaling UP (smooth); box/area when scaling DOWN (clean
            # averaging, no aliasing). We key the overlay's transparency from the
            # source pixels, so bicubic edge ringing can't create a black halo.
            return Image.BICUBIC if up else Image.BOX

        # All the per-image / per-frame caches below are byte-bounded LRUs. An
        # unbounded dict here grew to multiple GB on long charts (thousands of
        # distinct BGA images/frames — e.g. charts with 2700+ frames), exhausting RAM
        # in parallel playlist renders and yielding 0-byte output (ffmpeg's "received
        # no packets" when the worker is OOM-killed mid-pipe). Sequential frame
        # generation only touches a small working set at a time, so an LRU keeps the
        # speedup while capping memory. Eviction is safe: every cached value is a pure
        # function of (source image, target size), so a re-miss recomputes identically.
        from collections import OrderedDict
        class _LRUBytes:
            def __init__(self, cap):
                self.cap = cap; self.od = OrderedDict(); self.n = 0
            def get(self, k):
                v = self.od.get(k)
                if v is not None:
                    self.od.move_to_end(k); return v[0]
                return None
            def put(self, k, val, nbytes):
                if k in self.od:
                    self.n -= self.od[k][1]
                self.od[k] = (val, nbytes); self.n += nbytes
                self.od.move_to_end(k)
                while self.n > self.cap and len(self.od) > 1:
                    _k, (_v, _b) = self.od.popitem(last=False); self.n -= _b
                return val
        # Scale each cache's cap DOWN as the pool grows, so total memory across all
        # workers stays roughly constant no matter the thread count. A per-worker floor
        # keeps caches useful even at high worker counts. This is the fix for heavy
        # charts (2700+ frames) OOM-ing a large batch: alone they fit easily; 11 at once
        # with full-size caches each blew past RAM, killing a worker mid-pipe (ffmpeg
        # then reports "received no packets").
        def _cap(full_mb, floor_mb=16):
            return max(floor_mb, full_mb // n_workers) * 1024 * 1024
        _px = max(1, W * H)

        cache = _LRUBytes(_cap(96))           # img_path -> scaled RGB Image (base use)
        def _scaled(img_path):
            """Load an image scaled to fit the frame, centered on black. Cached.
            Used for the BASE layer. If the image has an alpha channel it is composited
            ONTO BLACK using that alpha — a transparent base image (e.g. white title
            text on a transparent background) must show as its content over black, not
            have its hidden RGB flattened to fill the whole frame (which turned such
            frames solid white)."""
            hit = cache.get(img_path)
            if hit is not None:
                return hit
            canvas = black.copy()
            if img_path is not None:
                try:
                    src = Image.open(img_path)
                    has_alpha = (src.mode in ("RGBA", "LA", "PA")) or (
                        "transparency" in getattr(src, "info", {}))
                    im = src.convert("RGBA") if has_alpha else src.convert("RGB")
                    iw, ih = im.size
                    if iw > 0 and ih > 0:
                        scale = min(W / iw, H / ih)
                        nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
                        im = im.resize((nw, nh), _resample(scale > 1.0))
                    pos = ((W - im.width) // 2, (H - im.height) // 2)
                    if has_alpha:
                        canvas.paste(im, pos, im)         # use alpha as the mask
                    else:
                        canvas.paste(im, pos)
                except Exception:
                    pass
            return cache.put(img_path, canvas, _px * 3)

        lcache = _LRUBytes(_cap(160))  # img_path -> scaled RGBA Image (overlay)
        def _scaled_rgba(img_path):
            """Load the OVERLAY (channel 07) image as RGBA for compositing over the
            base, matching how beatoraja renders a layer BMP:

            * If the image carries a MEANINGFUL ALPHA channel (one that actually
              contains some transparency), use it verbatim. Some layers define their
              shape purely in alpha (and may store inverted RGB), so deriving alpha
              from RGB would invert them — that was the ring/'?' bug.
            * If the image is nominally RGBA but its alpha is DEGENERATE — every
              pixel fully opaque (min == 255) — the alpha carries no shape and must
              NOT be trusted. This happens when a chart's original RGB/BMP layers were
              batch-converted to PNG and the converter stapled on a blanket opaque
              alpha. Trusting it verbatim leaves the layer's black background opaque,
              so it paints over the base and the whole frame goes black except the
              non-black overlay pixels (the 庫の女・庫の目 'base disappeared, only
              lyrics survive' bug). Such layers fall through to the black key below.
            * Otherwise (RGB/BMP, no alpha) use a BINARY black key: black is the
              transparent colour, every non-black pixel is FULLY opaque. There is NO
              brightness ramp — a layer's own translucency is baked into its pixels and
              must not be re-derived from luminance, or bright opaque overlays (e.g.
              the GOTTA CHAOS circles) wash out to faint ghosts. A small tolerance
              (<=16) treats near-black as transparent to absorb JPEG/BMP noise without
              keying out real dark content.

            The mask is built at SOURCE resolution then scaled together with the RGB,
            so edges anti-alias cleanly (no dark halo) without dimming interior pixels.
            """
            hit = lcache.get(img_path)
            if hit is not None:
                return hit
            canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            if img_path is not None:
                try:
                    import numpy as _np
                    src = Image.open(img_path)
                    has_alpha = (src.mode in ("RGBA", "LA", "PA")) or (
                        "transparency" in getattr(src, "info", {}))
                    if has_alpha:
                        im = src.convert("RGBA")          # trust the embedded alpha
                        # ...but only if that alpha is MEANINGFUL. A nominally-RGBA
                        # layer whose alpha is uniformly opaque (min == 255) carries no
                        # transparency — typically a batch BMP->PNG conversion that
                        # stapled on a blanket opaque channel. Trusting it would keep
                        # the layer's black background opaque and paint over the base.
                        # Fall through to the black key in that case.
                        if _np.asarray(im)[:, :, 3].min() == 255:
                            has_alpha = False
                    if not has_alpha:
                        rgb = _np.asarray(src.convert("RGB"))
                        lum = rgb.max(axis=2)             # 0..255
                        # binary black-key: BMS layers use pure black as the transparent
                        # colour. Use a TIGHT threshold so only true/near-pure black keys
                        # out — dark-but-coloured content (e.g. dark-brown hair at max~10)
                        # must stay opaque. A loose threshold (e.g. 16) wrongly keyed such
                        # content transparent. Measured assets: transparent regions are
                        # exactly (0,0,0); the darkest real content sits at max-channel ~10.
                        BLACK_TOL = 4
                        alpha = (_np.where(lum > BLACK_TOL, 255, 0)).astype("uint8")
                        rgba = _np.dstack([rgb, alpha]).astype("uint8")
                        im = Image.fromarray(rgba, "RGBA")
                    iw, ih = im.size
                    if iw > 0 and ih > 0:
                        scale = min(W / iw, H / ih)
                        nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
                        im = im.resize((nw, nh), _resample(scale > 1.0))
                    canvas.paste(im, ((W - im.width) // 2, (H - im.height) // 2), im)
                except Exception:
                    pass
            return lcache.put(img_path, canvas, _px * 4)

        rgba_base_cache = _LRUBytes(_cap(96))   # base_path -> base RGBA
        def _base_rgba_img(base_path):
            hit = rgba_base_cache.get(base_path)
            if hit is not None:
                return hit
            return rgba_base_cache.put(base_path, _scaled(base_path).convert("RGBA"),
                                       _px * 4)

        frame_cache = _LRUBytes(_cap(160))   # (base,layer) -> raw RGB bytes
        def frame_for(base_path, layer_path):
            key = (base_path, layer_path)
            hit = frame_cache.get(key)
            if hit is not None:
                return hit
            if layer_path is None:
                raw = _scaled(base_path).tobytes()
            else:
                # Alpha-composite the overlay (channel 07) over the base. PIL's
                # alpha_composite is C-optimised (faster than a numpy float blend).
                # The base RGBA image is cached per base so it isn't re-converted for
                # every overlay paired with it. Overlay alpha is proportional to
                # brightness (see _scaled_rgba) so edges blend instead of haloing.
                comp = _base_rgba_img(base_path).copy()
                comp.alpha_composite(_scaled_rgba(layer_path))
                raw = comp.convert("RGB").tobytes()
            return frame_cache.put(key, raw, len(raw))

        # 4) per-output-frame lookup for BOTH layers: show the last event whose time
        #    <= t. Prepend a blank (None) at t=0 so nothing shows before the first
        #    real event instead of freezing on frame 1.
        def _prep(evlist):
            e = sorted(((t, p) for t, p in evlist), key=lambda x: x[0])
            if not e or e[0][0] > 0.0:
                e = [(0.0, None)] + e
            return e
        ev = _prep(events)
        lev = _prep(layer_events)
        total_frames = max(1, int(duration * fps))

        # 5) pipe raw frames to ffmpeg, muxing the wav. Codec/container come from the
        #    encode options (None -> original H.264 4:2:0 + AAC in mp4).
        enc_args, real_out = _bga_encode_args(_opts, out_path)
        # change A: if we composited at a capped working size, upscale the finished
        # stream to the requested size in ONE ffmpeg pass (lanczos ~ matches the
        # BICUBIC look we'd have done per-frame) instead of upscaling every frame in
        # PIL. No-op when upscale_to is None (request was <= source, nothing to do).
        scale_args = []
        if upscale_to is not None:
            sw, sh = upscale_to
            scale_args = ["-vf", f"scale={sw}:{sh}:flags=lanczos"]
        out_path = real_out          # report the actual file we wrote

        # NVENC has a hard limit on SIMULTANEOUS encode sessions on consumer NVIDIA
        # GPUs (a handful at a time). In a big parallel batch, the workers past that
        # limit can't open a session and ffmpeg produces no video stream ("received no
        # packets"). So if an NVENC encode fails, retry ONCE with the software codec —
        # those few charts encode on the CPU instead of failing. (h264_nvenc→libx264,
        # hevc_nvenc→libx265, done by swapping the video opt and rebuilding the args.)
        def _run_encode(args):
            cmd = [ff, "-y", "-loglevel", "error",
                   "-f", "rawvideo", "-pixel_format", "rgb24",
                   "-video_size", f"{W}x{H}", "-framerate", str(fps), "-i", "pipe:0",
                   "-i", tmp_wav] + scale_args + args + [real_out]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    **_no_window_kwargs())
            # CRITICAL: drain ffmpeg's stdout/stderr on background threads while we feed
            # frames to stdin. If we don't, ffmpeg's stderr pipe fills, ffmpeg blocks,
            # stops reading stdin, and our writes block too — a deadlock (0-byte file).
            import threading as _th
            captured = {"err": b"", "out": b""}
            def _drain(stream, key):
                try:
                    captured[key] = stream.read()
                except Exception:
                    pass
            t_err = _th.Thread(target=_drain, args=(proc.stderr, "err"), daemon=True)
            t_out = _th.Thread(target=_drain, args=(proc.stdout, "out"), daemon=True)
            t_err.start(); t_out.start()
            ei = 0; li = 0
            try:
                for fno in range(total_frames):
                    t = fno / fps
                    while ei + 1 < len(ev) and ev[ei + 1][0] <= t:
                        ei += 1
                    while li + 1 < len(lev) and lev[li + 1][0] <= t:
                        li += 1
                    proc.stdin.write(frame_for(ev[ei][1], lev[li][1]))
            except BrokenPipeError:
                pass                       # ffmpeg exited early; error captured below
            try:
                proc.stdin.close()
            except OSError:
                pass
            proc.wait()
            t_err.join(timeout=5); t_out.join(timeout=5)
            return proc.returncode, captured["err"].decode("utf-8", "replace")

        rc, err_txt = _run_encode(enc_args)
        if rc != 0 and _is_nvenc_args(enc_args):
            # NVENC failed (most often the session limit in a big batch) — fall back to
            # software and try once more so the chart still renders.
            sw_opts = dict(_opts or {})
            sw_opts["video"] = {"x264_nvenc": "x264",
                                "hevc_nvenc": "hevc"}.get(sw_opts.get("video"), "x264")
            enc_args, real_out = _bga_encode_args(sw_opts, out_path)
            out_path = real_out
            rc, err_txt = _run_encode(enc_args)
        if os.path.exists(tmp_wav):
            try: os.remove(tmp_wav)
            except OSError: pass
        if rc != 0:
            raise RuntimeError("ffmpeg failed: " + err_txt[-300:])
        return (out_path, title, None)
    except Exception:
        return (out_path, title, traceback.format_exc())


def render_one_job(job):
    """Render ONE song to its final tagged file. Runs in a worker process.
    `job` = (in_path, out_path, fmt, tags, cover_bytes, ffmpeg, library_root[, quality]).
    Returns (out_path, title, None) on success or (out_path, title, error_str)."""
    if len(job) >= 8:
        in_path, out_path, fmt, tags, cover, ff, lib_root, quality = job[:8]
    else:
        in_path, out_path, fmt, tags, cover, ff, lib_root = job
        quality = None
    title = tags.get("Title", os.path.basename(in_path))
    try:
        set_library_root(lib_root)              # restore guard in this subprocess
        assert_safe_output(out_path)            # never write into the library
        audio, _ = render_bms(in_path)
        assert_safe_output(out_path)
        if fmt == "OGG":
            _encode_ogg_ffmpeg(ff, audio, out_path, quality)
        elif fmt == "MP3":
            _encode_mp3_ffmpeg(ff, audio, out_path, quality)
        elif fmt == "FLAC" and ff:
            _encode_flac_ffmpeg(ff, audio, out_path, quality)
        else:
            sf.write(out_path, audio, SR, format=("WAV" if fmt == "WAV" else "FLAC"))
        write_tags_to_file(out_path, fmt, tags, cover)
        return (out_path, title, None)
    except Exception:
        return (out_path, title, traceback.format_exc())

