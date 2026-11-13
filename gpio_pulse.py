"""Non-blocking GPIO pulse output for confirmed ROI counting events."""

import os
import queue
import threading


def _env_bool(name, default):
    value = os.environ.get(name, '1' if default else '0').strip().lower()
    return value in ('1', 'true', 'yes', 'on')


class GpioPulseOutput:
    """Serialize GPIO pulses on a worker so the DeepStream probe never sleeps."""

    def __init__(self):
        self.enabled = _env_bool('DS_CAMERA_GPIO_PULSE_ENABLED', True)
        self.channel = int(os.environ.get('DS_CAMERA_GPIO_BOARD_PIN', '29'))
        self.pulse_seconds = max(
            0.001,
            min(10.0, float(os.environ.get('DS_CAMERA_GPIO_PULSE_MS', '100')) / 1000.0),
        )
        self.active_high = _env_bool('DS_CAMERA_GPIO_ACTIVE_HIGH', True)
        # Do not build a long pulse backlog when several events arrive together.
        self._requests = queue.Queue(maxsize=1)
        self._pulse_in_flight = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._failed = False
        self._dropped = 0

    def start(self):
        if not self.enabled or self._failed:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._worker,
                daemon=True,
                name='GpioPulseOutput',
            )
            self._thread.start()

    def trigger(self):
        """Queue one pulse without blocking the inference/probe thread."""
        if not self.enabled or self._failed:
            return
        self.start()
        if self._pulse_in_flight.is_set():
            self._dropped += 1
            return
        self._pulse_in_flight.set()
        try:
            self._requests.put_nowait(1)
        except queue.Full:
            self._pulse_in_flight.clear()
            self._dropped += 1

    def stop(self):
        self._stop.set()
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _worker(self):
        gpio = None
        inactive = 0 if self.active_high else 1
        active = 1 - inactive
        try:
            # JetPack 6.2 reports the Super compatible string with a "-super"
            # suffix that Jetson.GPIO 2.1.7 does not recognize yet.
            os.environ.setdefault('JETSON_MODEL_NAME', 'JETSON_ORIN_NANO')
            import Jetson.GPIO as gpio

            gpio.setwarnings(False)
            gpio.setmode(gpio.BOARD)
            gpio.setup(self.channel, gpio.OUT, initial=inactive)
            print(
                f'[GPIO] ready board_pin={self.channel} '
                f'pulse_ms={self.pulse_seconds * 1000:.0f} '
                f'active_high={self.active_high}',
                flush=True,
            )
            while not self._stop.is_set():
                try:
                    request = self._requests.get(timeout=0.5)
                except queue.Empty:
                    continue
                if request is None:
                    break
                try:
                    gpio.output(self.channel, active)
                    self._stop.wait(self.pulse_seconds)
                finally:
                    gpio.output(self.channel, inactive)
                    self._stop.wait(0.010)
                    self._pulse_in_flight.clear()
        except Exception as exc:
            self._failed = True
            print(f'[GPIO] disabled after setup/output failure: {exc!r}', flush=True)
        finally:
            self._pulse_in_flight.clear()
            if gpio is not None:
                try:
                    gpio.output(self.channel, inactive)
                except Exception:
                    pass
                try:
                    gpio.cleanup(self.channel)
                except Exception:
                    pass


output = GpioPulseOutput()
