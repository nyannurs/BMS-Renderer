"""
Audio player for BMS Renderer.

Wraps sounddevice in a small state machine with:
  - sample-accurate playback position (for the timeline)
  - play / pause / resume / stop
  - seek to an arbitrary position (used on drag-release)
  - an on-finished callback (so the queue can auto-advance)

Design notes:
  * The whole song is a numpy float32 stereo array already in memory (the render
    engine produces it), so "seek" is just moving an integer sample cursor.
  * We use an OutputStream with a callback that copies frames out of the buffer
    starting at the cursor, advancing it. Position = cursor / samplerate.
  * Everything that touches the cursor is guarded by a lock, because the audio
    callback runs on PortAudio's own thread.
"""
import threading

try:
    import sounddevice as sd
    SD_OK = True
except Exception:
    sd = None
    SD_OK = False

import numpy as np


class Player:
    def __init__(self, samplerate=44100, on_finished=None):
        self.sr = samplerate
        self.on_finished = on_finished      # called (from main thread ideally) at end
        self._buf = None                    # current song: (N, 2) float32
        self._cursor = 0                    # next sample frame to output
        self._lock = threading.Lock()
        self._stream = None
        self._state = "stopped"             # stopped | playing | paused
        self._finished_pending = False
        self._volume = 1.0                   # 0.0–1.0, applied in the callback

    def set_volume(self, v):
        self._volume = max(0.0, min(1.0, float(v)))
    # ---- queries ----
    @property
    def state(self):
        return self._state

    def duration_seconds(self):
        with self._lock:
            return 0.0 if self._buf is None else len(self._buf) / self.sr

    def position_seconds(self):
        with self._lock:
            return self._cursor / self.sr

    # ---- core control ----
    def load(self, buffer):
        """Load a new song buffer and reset to the start (does not auto-play)."""
        self.stop()
        with self._lock:
            self._buf = buffer
            self._cursor = 0

    def _callback(self, outdata, frames, time_info, status):
        # Runs on PortAudio's thread. Fill outdata from the buffer at the cursor.
        with self._lock:
            if self._buf is None:
                outdata.fill(0)
                return
            start = self._cursor
            end = min(start + frames, len(self._buf))
            n = end - start
            if n > 0:
                outdata[:n] = self._buf[start:end]
                if self._volume != 1.0:
                    outdata[:n] *= self._volume
            if n < frames:
                outdata[n:].fill(0)          # pad tail with silence
                self._cursor = len(self._buf)
                # mark finished; the main loop polls this and fires the callback
                self._finished_pending = True
            else:
                self._cursor = end

    def _ensure_stream(self):
        if self._stream is None:
            if not SD_OK:
                raise RuntimeError("sounddevice/PortAudio not available")
            self._stream = sd.OutputStream(
                samplerate=self.sr, channels=2, dtype="float32",
                callback=self._callback)

    def play(self):
        """Start or resume playback from the current cursor."""
        if self._buf is None:
            return
        self._ensure_stream()
        self._finished_pending = False
        self._state = "playing"
        if not self._stream.active:
            self._stream.start()

    def pause(self):
        if self._state == "playing" and self._stream is not None:
            self._stream.stop()             # stops the callback; cursor stays put
            self._state = "paused"

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
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

    def poll_finished(self):
        """Called periodically by the GUI. Returns True once when the song ends."""
        if self._finished_pending:
            self._finished_pending = False
            self._state = "stopped"
            return True
        return False
