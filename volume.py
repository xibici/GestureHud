"""System master volume control.

Two flavours: `Volume` sets the level straight through Core Audio, and
`MediaKeyVolume` nudges it with the same media keys a keyboard sends. The
latter is a little coarser (Windows moves in fixed 2% steps) but it is what
makes Windows show its own volume OSD - the shell only puts that up for
volume-key input, never for an API-driven change.
"""
import ctypes
import comtypes
from ctypes import cast, POINTER
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

user32 = ctypes.windll.user32

VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
KEYEVENTF_KEYUP = 0x0002

# Windows moves master volume in 50 discrete steps.
VOLUME_STEP = 0.02
MAX_STEPS_PER_UPDATE = 50  # a full-range swipe, so one update can never spin


class Volume:
    def __init__(self):
        comtypes.CoInitialize()
        speakers = AudioUtilities.GetSpeakers()
        interface = speakers.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        self._endpoint = cast(interface, POINTER(IAudioEndpointVolume))

    def get(self) -> float:
        """Current volume as a 0.0-1.0 fraction (0 if muted)."""
        if self._endpoint.GetMute():
            return 0.0
        return self._endpoint.GetMasterVolumeLevelScalar()

    def set(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        if fraction > 0 and self._endpoint.GetMute():
            self._endpoint.SetMute(0, None)
        self._endpoint.SetMasterVolumeLevelScalar(fraction, None)


class MediaKeyVolume(Volume):
    """Adjusts volume by sending media keys, so Windows shows its own OSD.

    Reading stays on Core Audio (exact); only the write goes through keys.
    """

    @staticmethod
    def _tap(vk):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    def set(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        current = self.get()
        steps = int(round((fraction - current) / VOLUME_STEP))
        if steps == 0:
            return
        vk = VK_VOLUME_UP if steps > 0 else VK_VOLUME_DOWN
        for _ in range(min(abs(steps), MAX_STEPS_PER_UPDATE)):
            self._tap(vk)
