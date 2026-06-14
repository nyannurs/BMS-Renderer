"""
Audio player for BMS Renderer.

Wraps sounddevice in a small state machine with:
  - sample-accurate playback position (for the timeline)
  - play / pause / resume / stop
  - seek to an arbitrary position (used on drag-release)
  - an on-finished flag (so the queue can auto-advance)

Design notes (why a feeder thread, not a callback):
  * The whole song is a numpy float32 stereo array already in memory (the render
    engine produces it), so "seek" is just moving an integer sample cursor.
  * Instead of giving PortAudio a Python *callback* (which then runs ON PortAudio's
    realtime thread and is therefore at the mercy of the GIL — any Python/GC pause
    starves it and you hear a stutter), we open a PLAIN OutputStream and push audio
    to it from our OWN dedicated feeder thread using the blocking `stream.write()`.
  * PortAudio keeps a sizeable internal buffer (we ask for latency="high"). The
    feeder races ahead to keep that buffer full; PortAudio drains it at the hardware
    clock independently of Python. So if Python briefly stalls, PortAudio just keeps
    playing out of its cushion — the hiccup is absorbed instead of becoming a gap.
  * The feeder owns the cursor while playing. The GUI only reads it (atomic int) for
    the timeline and writes it on a (rare) seek.
"""
import threading

try:
    import numpy as np
except Exception:
    np = None

try:
    import sounddevice as sd
    SD_OK = True
    SD_IMPORT_ERROR = ""
except Exception as _e:
    sd = None
    SD_OK = False
    # keep the reason so the app can tell the user WHY playback is unavailable
    # (commonly: PortAudio's shared library isn't installed)
    SD_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"


class Player:
    # Frames per write block. ~93ms at 44.1kHz: large enough that per-block Python
    # overhead is negligible and the feeder stays well ahead of the hardware drain,
    # small enough that pause/seek still feel responsive.
    BLOCKSIZE = 4096

    def __init__(self, samplerate=44100, on_finished=None):
        self.sr = samplerate
        self.on_finished = on_finished
        self._buf = None                    # current song: (N, 2) float32
        self._cursor = 0                    # next sample frame to output
        self._lock = threading.Lock()       # guards _buf/_cursor for seek vs feeder
        self._stream = None
        self._stream_device = None
        self._state = "stopped"             # stopped | playing | paused
        self._finished_pending = False
        self._volume = 1.0

        # feeder-thread machinery
        self._feeder = None
        self._run = False                   # feeder keeps looping while True
        self._resume = threading.Event()    # set = play, clear = pause (feeder waits)

    def set_volume(self, v):
        self._volume = max(0.0, min(1.0, float(v)))

    # ---- queries ----
    @property
    def state(self):
        return self._state

    def duration_seconds(self):
        b = self._buf
        return 0.0 if b is None else len(b) / self.sr

    def position_seconds(self):
        # The feeder races ahead of the speaker by roughly the stream's output
        # latency; subtract it so the timeline reflects what's actually being heard.
        pos = self._cursor / self.sr
        try:
            if self._stream is not None and self._state == "playing":
                pos -= float(self._stream.latency)
        except Exception:
            pass
        return max(0.0, pos)

    # ---- core control ----
    def load(self, buffer):
        """Load a new song buffer and reset to the start (does not auto-play). The
        buffer is normalized to contiguous float32 stereo up-front so the feeder
        never has to convert dtype/shape/layout while playing."""
        try:
            a = np.asarray(buffer)
            if a.dtype != np.float32:
                a = a.astype(np.float32)
            if a.ndim == 1:
                a = np.column_stack([a, a])
            elif a.ndim == 2 and a.shape[1] == 1:
                a = np.repeat(a, 2, axis=1)
            elif a.ndim == 2 and a.shape[1] > 2:
                a = a[:, :2]
            buffer = np.ascontiguousarray(a, dtype=np.float32)
            # clip ONCE here so the feeder can hand PortAudio direct slices at full
            # volume without per-block clipping work
            np.clip(buffer, -1.0, 1.0, out=buffer)
        except Exception:
            pass
        self.stop()
        with self._lock:
            self._buf = buffer
            self._cursor = 0

    def _query_default_device(self):
        if not SD_OK:
            return None
        try:
            dev = sd.default.device
            out = dev[1] if isinstance(dev, (list, tuple)) else dev
            if out is None or out < 0:
                return sd.query_hostapis(sd.default.hostapi)["default_output_device"]
            return out
        except Exception:
            return None

    def _open_stream(self, device):
        """Open a plain (callback-less) blocking output stream with a deliberately
        deep buffer. blocksize=0 lets PortAudio choose its optimal internal size, and
        a concrete high latency (~0.25s) gives a fat cushion so a late feeder refill
        is absorbed instead of heard as a gap."""
        return sd.OutputStream(
            samplerate=self.sr, channels=2, dtype="float32",
            device=device, blocksize=0, latency=0.25)

    def _ensure_stream(self):
        if not SD_OK:
            raise RuntimeError("sounddevice/PortAudio not available")
        if self._stream is None:
            want = self._query_default_device()
            self._stream = self._open_stream(want)
            self._stream_device = want
            self._stream.start()

    def _feed_loop(self):
        """Runs on our own thread. Pushes blocks into PortAudio's buffer via blocking
        write(); PortAudio drains them at the hardware clock. A brief Python stall
        just means we refill a little late — PortAudio keeps playing from its cushion,
        so the stall is absorbed rather than heard as a gap.

        We write fairly large chunks and, at full volume, hand PortAudio a direct
        slice of the song (no per-block copy/scale/clip) to keep the feeder well ahead
        of the drain rate even under GIL contention."""
        bs = self.BLOCKSIZE
        while self._run:
            if not self._resume.wait(timeout=0.1):
                continue
            if not self._run:
                break

            with self._lock:
                buf = self._buf
                start = self._cursor
                if buf is None:
                    self._resume.clear()
                    continue
                total = len(buf)
                if start >= total:
                    self._finished_pending = True
                    self._resume.clear()
                    continue
                end = min(start + bs, total)
                self._cursor = end
                vol = self._volume
                block = buf[start:end]

            if vol >= 0.999:
                # full volume: the rendered audio is already float32 and pre-clipped
                # at render time, so write the slice directly — no allocation at all
                out = block
            else:
                out = block * vol
                np.clip(out, -1.0, 1.0, out=out)

            try:
                self._stream.write(out)      # blocks until PortAudio has room
            except Exception:
                self._run = False
                break

    def _start_feeder(self):
        if self._feeder is None or not self._feeder.is_alive():
            self._run = True
            self._feeder = threading.Thread(target=self._feed_loop, daemon=True)
            self._feeder.start()

    def play(self):
        """Start or resume playback from the current cursor."""
        if self._buf is None:
            return
        self._ensure_stream()
        self._finished_pending = False
        self._state = "playing"
        self._start_feeder()
        self._resume.set()                   # let the feeder run

    def pause(self):
        if self._state == "playing":
            self._resume.clear()             # feeder parks itself at the next block
            self._state = "paused"

    def stop(self):
        # tell the feeder to exit, wake it, and wait for it to finish
        self._run = False
        self._resume.set()
        if self._feeder is not None and self._feeder.is_alive() \
                and threading.current_thread() is not self._feeder:
            self._feeder.join(timeout=1.0)
        self._feeder = None
        self._resume.clear()
        if self._stream is not None:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._lock:
            self._cursor = 0
        self._state = "stopped"
        self._finished_pending = False

    def toggle(self):
        if self._state == "playing":
            self.pause()
        else:
            self.play()

    def seek_seconds(self, seconds):
        """Move the playhead. Safe whether playing, paused, or stopped."""
        with self._lock:
            if self._buf is None:
                return
            target = int(max(0.0, seconds) * self.sr)
            self._cursor = min(target, len(self._buf))

    def redetect_device(self):
        """Re-read the system audio devices and move playback to the current default
        output, preserving the playhead. Safe whether playing, paused, or stopped.
        Returns True if it switched without error."""
        if not SD_OK:
            return False
        was_playing = (self._state == "playing")
        with self._lock:
            pos = self._cursor
        # tear down feeder + stream first, THEN reinit PortAudio (terminating with a
        # live stream open would crash) so it sees the newly-selected default device
        self._run = False
        self._resume.set()
        if self._feeder is not None and self._feeder.is_alive():
            self._feeder.join(timeout=1.0)
        self._feeder = None
        self._resume.clear()
        if self._stream is not None:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
            self._stream = None
        try:
            sd._terminate(); sd._initialize()
        except Exception:
            pass
        want = self._query_default_device()
        try:
            self._stream = self._open_stream(want)
            self._stream_device = want
            self._stream.start()
            with self._lock:
                self._cursor = pos
            if was_playing:
                self._state = "playing"
                self._finished_pending = False
                self._start_feeder()
                self._resume.set()
            return True
        except Exception:
            self._stream = None
            self._state = "stopped"
            return False

    def poll_finished(self):
        """Called periodically by the GUI. Returns True once when the song ends."""
        if self._finished_pending:
            self._finished_pending = False
            self._state = "stopped"
            return True
        return False
