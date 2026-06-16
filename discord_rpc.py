"""
Optional Discord Rich Presence for BMS Renderer.
=================================================

This file is COMPLETELY OPTIONAL. If you delete it (or it fails to load), BMS
Renderer runs exactly as normal, just without Discord status. Drop it in the same
folder as bms_renderer_qt.py to turn Rich Presence on.

What you need to do
-------------------
1. Install the dependency:
       pip install pypresence
2. Create a Discord application so the status has a name + icon:
   - Go to https://discord.com/developers/applications and click "New Application".
   - Name it whatever you want the status to read as (e.g. "BMS Renderer").
     The application NAME is what shows up as "Playing <name>" in Discord.
   - Copy the "Application ID" (a long number) from the General Information page.
   - Paste it into CLIENT_ID below.
3. (Optional) Add artwork:
   - In the app, open "Rich Presence" -> "Art Assets" and upload an image.
   - Name the asset key exactly  bms_logo  (or change LARGE_IMAGE_KEY below).
     Without this, the status still works but shows no large icon.
4. Make sure the Discord DESKTOP app is running and you're logged in. RPC talks to
   the local Discord client over IPC; it does nothing if Discord isn't open.

That's it. Run BMS Renderer and play a song — your Discord profile will show it.

Nothing here ever touches your BMS files; it only sends "now playing" text to your
local Discord client.
"""

import time

# ---------------------------------------------------------------------------
# CONFIG — edit these three values.
# ---------------------------------------------------------------------------
CLIENT_ID = "PUT_YOUR_APPLICATION_ID_HERE"   # <-- paste your Discord Application ID
LARGE_IMAGE_KEY = "bms_logo"                  # Art Asset key, or "" for no image
LARGE_IMAGE_TEXT = "BMS Renderer"             # tooltip when hovering the large image
# ---------------------------------------------------------------------------

try:
    from pypresence import Presence
    _PYPRESENCE_OK = True
except Exception:
    Presence = None
    _PYPRESENCE_OK = False


class RichPresence:
    """A thin, fail-safe wrapper around pypresence.

    Every method swallows its own errors: if Discord isn't running, the client id
    isn't set, or pypresence isn't installed, calls quietly do nothing so the host
    app never breaks. `connected` reports whether the link is live.
    """

    def __init__(self, client_id=None):
        self.client_id = client_id or CLIENT_ID
        self._rpc = None
        self.connected = False
        self._start_ts = None
        self.last_error = ""        # surfaced to the host app so failures are visible

    # -- lifecycle ----------------------------------------------------------
    def connect(self):
        """Open the IPC link to the local Discord client. Safe to call once."""
        if not _PYPRESENCE_OK:
            self.last_error = "pypresence not installed (pip install pypresence)"
            return False
        if not self.client_id or self.client_id == "PUT_YOUR_APPLICATION_ID_HERE":
            self.last_error = "CLIENT_ID not set in discord_rpc.py"
            return False
        try:
            self._rpc = Presence(self.client_id)
            self._rpc.connect()
            self.connected = True
            self.last_error = ""
        except Exception as e:
            self._rpc = None
            self.connected = False
            self.last_error = f"{type(e).__name__}: {e}"
        return self.connected

    def close(self):
        if self._rpc is not None:
            try:
                self._rpc.clear()
            except Exception:
                pass
            try:
                self._rpc.close()
            except Exception:
                pass
        self._rpc = None
        self.connected = False

    # -- presence updates ---------------------------------------------------
    def set_playing(self, title, artist="", duration=0):
        """Show '<title>' / 'by <artist>' with an elapsed (or remaining) timer."""
        if not self.connected or self._rpc is None:
            return
        # Discord requires the 'details'/'state' strings to be 2..128 chars; clamp.
        details = self._fit(title or "Unknown title")
        state = self._fit(("by " + artist) if artist else "Playing a chart")
        kwargs = {
            "details": details,
            "state": state,
            "large_image": LARGE_IMAGE_KEY or None,
            "large_text": LARGE_IMAGE_TEXT or None,
        }
        now = time.time()
        if duration and duration > 0:
            # show a countdown that ends when the song ends
            kwargs["start"] = int(now)
            kwargs["end"] = int(now + duration)
        else:
            kwargs["start"] = int(now)
        self._start_ts = now
        try:
            self._rpc.update(**kwargs)
            self.last_error = ""
        except Exception as e:
            self.last_error = f"update failed: {type(e).__name__}: {e}"

    def set_paused(self):
        if not self.connected or self._rpc is None:
            return
        try:
            self._rpc.update(
                details="Paused",
                state=" ",                     # a single space keeps Discord happy
                large_image=LARGE_IMAGE_KEY or None,
                large_text=LARGE_IMAGE_TEXT or None,
            )
        except Exception:
            pass

    def clear(self):
        if not self.connected or self._rpc is None:
            return
        try:
            self._rpc.clear()
        except Exception:
            pass

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _fit(s):
        """Discord rejects empty or >128-char fields; keep them in range."""
        s = (s or "").strip()
        if not s:
            s = " "
        return s[:128]
