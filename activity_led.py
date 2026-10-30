"""Optional Raspberry Pi ACT LED indication through Linux sysfs (no GPIO library)."""

from pathlib import Path
import re
import sys
import threading


class ActivityLed:
    """Blink after a successful saved sample; restore the kernel trigger on exit."""

    def __init__(self, selection="off", root=Path("/sys/class/leds")):
        self.path = None
        if selection != "off":
            names = ("ACT", "led0") if selection == "auto" else (selection,)
            self.path = next((Path(root) / name for name in names
                              if (Path(root) / name / "trigger").exists()), None)
            if self.path is None:
                raise OSError("ACT LED not found; inspect /sys/class/leds or use --led off")
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._recording = False
        self._thread = None
        self._original_trigger = None
        self._original_brightness = None
        self._maximum = 1

    def _write(self, name, value):
        (self.path / name).write_text(str(value) + "\n", encoding="ascii")

    def __enter__(self):
        if self.path is None:
            return self
        triggers = (self.path / "trigger").read_text()
        selected = re.search(r"\[([^]]+)\]", triggers)
        if not selected:
            raise OSError(f"Cannot determine current LED trigger at {self.path}")
        self._original_trigger = selected[1]
        self._original_brightness = int((self.path / "brightness").read_text())
        self._maximum = int((self.path / "max_brightness").read_text())
        try:
            self._write("trigger", "none")
            self._write("brightness", 0)
        except OSError as exc:
            self._restore()
            raise OSError("Cannot control ACT LED; install the supplied udev rule and "
                          "join the gpio group (see README), or use --led off") from exc
        self._thread = threading.Thread(target=self._blink, name="activity-led", daemon=True)
        self._thread.start()
        return self

    def set_recording(self, recording):
        with self._lock:
            self._recording = recording
            if not recording and self.path is not None:
                try:
                    self._write("brightness", 0)
                except OSError:
                    pass  # The worker reports LED failures; logging can still continue.

    def _blink(self):
        try:
            while not self._stop.is_set():
                with self._lock:
                    enabled = self._recording
                    self._write("brightness", self._maximum if enabled else 0)
                if self._stop.wait(0.1 if enabled else 0.2):
                    break
                with self._lock:
                    self._write("brightness", 0)
                if enabled:
                    self._stop.wait(0.9)
        except OSError as exc:
            print(f"WARNING: ACT LED control failed; CSV logging continues: {exc}",
                  file=sys.stderr, flush=True)

    def _restore(self):
        if self.path is None or self._original_trigger is None:
            return
        try:
            self._write("brightness", self._original_brightness)
            self._write("trigger", self._original_trigger)
        except OSError as exc:
            print(f"WARNING: could not restore ACT LED: {exc}", file=sys.stderr, flush=True)

    def __exit__(self, *_):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._restore()
