"""Non-blocking localhost bridge from the DeepStream probe to modbusd."""

import json
import os
import queue
import threading
import time
import urllib.request
import uuid


MODBUSD_URL = os.environ.get("DS_CAMERA_MODBUSD_URL", "http://127.0.0.1:5123").rstrip("/")


class ModbusEventPublisher:
    def __init__(self, base_url=MODBUSD_URL, max_queue=2048):
        self.base_url = base_url.rstrip("/")
        self.boot_id = uuid.uuid4().hex
        self.sequence = 0
        self.queue = queue.Queue(maxsize=max_queue)
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="ModbusBridge")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)

    def publish(self, class_name, direction, speed_kph):
        self.sequence += 1
        event = {
            "event_id": f"{self.boot_id}:{self.sequence}",
            "class_name": str(class_name),
            "direction": str(direction),
            "speed_kph": float(speed_kph or 0.0),
            "recorded_at": time.time(),
        }
        try:
            self.queue.put_nowait(event)
            return True
        except queue.Full:
            print("[ModbusBridge] event queue full; dropping oldest event", flush=True)
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(event)
                return True
            except queue.Empty:
                return False

    def _post(self, event):
        data = json.dumps(event, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/event",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=1.0) as response:
            return response.status == 200

    def _run(self):
        pending = None
        while not self.stop_event.is_set():
            if pending is None:
                try:
                    pending = self.queue.get(timeout=0.5)
                except queue.Empty:
                    continue
            try:
                if self._post(pending):
                    pending = None
            except Exception:
                self.stop_event.wait(0.5)


publisher = ModbusEventPublisher()
