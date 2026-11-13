#!/usr/bin/env python3
"""Independent Modbus RTU daemon.

This process exclusively owns the serial adapter and keeps cumulative counts
available while the DeepStream inference process is restarting or stopped.
"""

import fcntl
import json
import math
import os
import subprocess
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ds_modbus_rtu_comm as modbus


# Shared by inferd via import. Definitions only: importing this module must
# never start ModbusDaemon, its serial worker, or its HTTP server.
DEFAULT_REALTIME_WINDOW_MS = 500


def realtime_window_seconds(settings):
    """Read the sole window setting: analytics.realtime_window_ms."""
    if not isinstance(settings, dict):
        raise ValueError('operation settings must be an object')
    analytics = settings.get('analytics', {})
    if not isinstance(analytics, dict):
        raise ValueError('analytics must be an object')
    value = analytics.get('realtime_window_ms', DEFAULT_REALTIME_WINDOW_MS)
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        raise ValueError('analytics.realtime_window_ms must be a number >= 50') from None
    if isinstance(value, bool) or not math.isfinite(milliseconds) or milliseconds < 50:
        raise ValueError('analytics.realtime_window_ms must be a finite number >= 50')
    return milliseconds / 1000.0


class RealtimeWindowConfig:
    """Poll the shared settings file; retain the last valid value on bad saves."""
    def __init__(self, path, poll_interval=0.5):
        self.path = path
        self.poll_interval = poll_interval
        self._seconds = DEFAULT_REALTIME_WINDOW_MS / 1000.0
        self._next_check = 0.0
        self._failed_signature = None
        self._lock = threading.Lock()

    def seconds(self):
        with self._lock:
            now = time.monotonic()
            if self.poll_interval > 0 and now < self._next_check:
                return self._seconds
            self._next_check = now + self.poll_interval
            signature = None
            try:
                stat = os.stat(self.path)
                signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                # Read at the polling interval even if metadata is unchanged:
                # some filesystems have coarse timestamps and edits can keep
                # the same size (e.g. 200 -> 500). Never read on every frame.
                with open(self.path, encoding='utf-8') as handle:
                    value = realtime_window_seconds(json.load(handle))
                self._seconds = value
                self._failed_signature = None
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                # Warn once per failed file revision, not once per video frame.
                if signature is None or signature != self._failed_signature:
                    print(f'[WARN] real-time window setting not applied; keeping '
                          f'{self._seconds * 1000:g}ms: {exc}', flush=True)
                self._failed_signature = signature
            return self._seconds


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "operation_settings.json")
STATE_FILE = os.environ.get("DS_CAMERA_MODBUSD_STATE_FILE", os.path.join(BASE_DIR, "run", "modbus_state.json"))
INFERD_URL = os.environ.get("DS_CAMERA_DAEMON_URL", "http://127.0.0.1:5122").rstrip("/")
HTTP_HOST = os.environ.get("DS_CAMERA_MODBUSD_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("DS_CAMERA_MODBUSD_PORT", "5123"))
INFER_RESTART_CMD = os.environ.get(
    "DS_CAMERA_MODBUSD_INFER_RESTART_CMD",
    "sudo -n /usr/bin/systemctl restart jetson-ai-modbus-rtu-inferd.service",
).split()
REBOOT_CMD = os.environ.get(
    "DS_CAMERA_MODBUSD_REBOOT_CMD",
    "sudo -n /usr/bin/systemctl start jetson-ai-modbus-rtu-reboot.service",
).split()


def _slot_key(class_name, direction):
    name = str(class_name or "").strip().lower()
    if name == "person":
        object_no = 0
    elif name in ("car", "bus", "truck", "vehicle"):
        object_no = 1
    elif name in ("motorcycle", "motorbike", "bike"):
        object_no = 2
    else:
        return None
    direction_no = 1 if str(direction) in ("상행", "1", "D1", "d1") else 2
    return object_no, direction_no


def _empty_slots():
    return {key: {"count": 0, "speed_sum": 0.0, "speed_square_sum": 0.0}
            for key in modbus.SLOT_DESCRIPTORS}


def _slot_output(stats):
    output = {}
    for key, value in stats.items():
        count = int(value.get("count", 0) or 0)
        speed_sum = float(value.get("speed_sum", 0.0) or 0.0)
        speed_square_sum = float(value.get("speed_square_sum", 0.0) or 0.0)
        mean = speed_sum / count if count else 0.0
        variance = max(0.0, speed_square_sum / count - mean * mean) if count else 0.0
        output[key] = {
            "count": count,
            "average_speed_kph": mean,
            "speed_stddev_kph": math.sqrt(variance),
        }
    return output


def _read_device_serial():
    for path in ("/sys/firmware/devicetree/base/serial-number", "/proc/device-tree/serial-number"):
        try:
            with open(path, "rb") as handle:
                return int(handle.read().rstrip(b"\0").decode("ascii").strip(), 10)
        except Exception:
            pass
    return 0


def _read_temperature_c():
    for path in ("/sys/devices/virtual/thermal/thermal_zone0/temp", "/sys/class/thermal/thermal_zone0/temp"):
        try:
            with open(path, "r", encoding="ascii") as handle:
                value = float(handle.read().strip())
            return value / 1000.0 if value > 1000 else value
        except Exception:
            pass
    return 0.0


class ModbusDaemon:
    def __init__(self, start_serial=True):
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.settings = self._load_json(SETTINGS_FILE, {})
        self.realtime_window_config = RealtimeWindowConfig(SETTINGS_FILE)
        self.realtime_window_sec = self.realtime_window_config.seconds()
        modbus_settings = self.settings.get("modbus_rtu", {})
        roi_defaults = self.settings.get("roi_defaults", {})
        self.port = os.environ.get("DS_CAMERA_MODBUS_PORT", modbus_settings.get("port", "/dev/ttyUSB0"))
        self.baudrate = int(os.environ.get("DS_CAMERA_MODBUS_BAUDRATE", modbus_settings.get("baudrate", 460800)))
        self.slave_id = int(os.environ.get("DS_CAMERA_MODBUS_SLAVE_ID", modbus_settings.get("slave_id", 101)))
        self.timeout = float(os.environ.get("DS_CAMERA_MODBUS_TIMEOUT_SEC", modbus_settings.get("timeout_seconds", 0.005)))
        self.confidence = float(roi_defaults.get("confidence_threshold", 0.30))
        self.roi_distance = float(roi_defaults.get("ref_distance_m", 10.0))
        self.cumulative = _empty_slots()
        self.realtime = deque(maxlen=512)
        self.seen_event_ids = deque(maxlen=128)
        self._seen_event_set = set()
        self.command_sequence = 0
        self.command_value = 0
        self.infer_status = {}
        self.infer_reachable = False
        self.last_frame_progress_at = 0.0
        self.last_frame_signature = None
        self.started_at = time.time()
        self._load_state()
        self.runtime = None
        if start_serial:
            self.runtime = modbus.ModbusRTUSlaveRuntime(
                port=self.port,
                baudrate=self.baudrate,
                slave_id=self.slave_id,
                timeout_sec=self.timeout,
                snapshot_provider=self.snapshot,
                write_handler=self.handle_write,
            )

    @staticmethod
    def _load_json(path, default):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else default
        except Exception:
            return default

    def _load_state(self):
        state = self._load_json(STATE_FILE, {})
        for key_text, value in state.get("cumulative", {}).items():
            try:
                key = tuple(int(part) for part in key_text.split(":"))
            except Exception:
                continue
            if key in self.cumulative and isinstance(value, dict):
                self.cumulative[key].update(value)
        for event_id in state.get("seen_event_ids", [])[-128:]:
            event_id = str(event_id)
            self.seen_event_ids.append(event_id)
            self._seen_event_set.add(event_id)
        self.command_sequence = int(state.get("command_sequence", 0) or 0) & 0xFFFF

    def _save_state(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        state = {
            "version": 1,
            "saved_at": time.time(),
            "command_sequence": self.command_sequence,
            "cumulative": {f"{key[0]}:{key[1]}": value for key, value in self.cumulative.items()},
            "seen_event_ids": list(self.seen_event_ids),
        }
        temporary = STATE_FILE + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, STATE_FILE)

    def _save_settings(self):
        lock_path = SETTINGS_FILE + ".lock"
        os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            latest = self._load_json(SETTINGS_FILE, {})
            latest["version"] = 1
            latest.setdefault("modbus_rtu", {}).update({
                "port": self.port,
                "baudrate": self.baudrate,
                "slave_id": self.slave_id,
            })
            latest.setdefault("roi_defaults", {}).update({
                "ref_distance_m": self.roi_distance,
                "confidence_threshold": self.confidence,
            })
            temporary = SETTINGS_FILE + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(latest, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, SETTINGS_FILE)
            self.settings = latest
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _set_inference_desired_running(self):
        """Make an RTU inference restart resume the saved input on startup."""
        lock_path = SETTINGS_FILE + ".lock"
        os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            latest = self._load_json(SETTINGS_FILE, {})
            latest["version"] = 1
            latest.setdefault("startup", {})["desired_running"] = True
            temporary = SETTINGS_FILE + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(latest, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, SETTINGS_FILE)
            self.settings = latest
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def apply_config(self, start, values, notify_inferd=False):
        """Validate and persist ROI distance/confidence register values."""
        values = [int(value) for value in values]
        if not values:
            return False
        end = start + len(values) - 1
        if start < modbus.ROI_REFERENCE_DISTANCE_ADDRESS or end > modbus.BBOX_CONFIDENCE_ADDRESS:
            return False
        with self.lock:
            new_distance = self.roi_distance
            new_confidence = self.confidence
            for offset, value in enumerate(values):
                address = start + offset
                if address == modbus.ROI_REFERENCE_DISTANCE_ADDRESS:
                    new_distance = value / 100.0
                    if not 0.1 <= new_distance <= 655.35:
                        return False
                elif address == modbus.BBOX_CONFIDENCE_ADDRESS:
                    new_confidence = value / 100.0
                    if not 0.0 <= new_confidence <= 1.0:
                        return False
            self.roi_distance = new_distance
            self.confidence = new_confidence
            self._save_settings()
        if notify_inferd:
            self._post_infer_config(start, values)
        return True

    def record_event(self, event):
        event_id = str(event.get("event_id", "")).strip()
        key = _slot_key(event.get("class_name"), event.get("direction"))
        if not event_id or key is None:
            return False, "invalid event"
        with self.lock:
            if event_id in self._seen_event_set:
                return True, "duplicate"
            if len(self.seen_event_ids) == self.seen_event_ids.maxlen:
                self._seen_event_set.discard(self.seen_event_ids[0])
            self.seen_event_ids.append(event_id)
            self._seen_event_set.add(event_id)
            speed = 0.0 if key[0] == 0 else max(0.0, float(event.get("speed_kph", 0.0) or 0.0))
            recorded_at = float(event.get("recorded_at", time.time()) or time.time())
            self.realtime.append({"key": key, "speed_kph": speed, "recorded_at": recorded_at})
            item = self.cumulative[key]
            if int(item["count"]) >= 0xFFFF:
                item.update({"count": 0, "speed_sum": 0.0, "speed_square_sum": 0.0})
            item["count"] += 1
            item["speed_sum"] += speed
            item["speed_square_sum"] += speed * speed
            self._save_state()
        return True, "recorded"

    def _realtime_stats(self):
        self.realtime_window_sec = self.realtime_window_config.seconds()
        now = time.time()
        cutoff = now - self.realtime_window_sec
        stats = _empty_slots()
        while self.realtime and self.realtime[0]["recorded_at"] <= cutoff:
            self.realtime.popleft()
        for event in self.realtime:
            item = stats[event["key"]]
            speed = event["speed_kph"]
            item["count"] += 1
            item["speed_sum"] += speed
            item["speed_square_sum"] += speed * speed
        return stats

    def _device_status(self):
        status = self.infer_status
        if not self.infer_reachable or not status.get("running"):
            return 2
        streams = status.get("streams") or []
        if not streams or not any(int(stream.get("frame_idx", 0) or 0) > 0 for stream in streams):
            return 2
        return 1 if time.time() - self.last_frame_progress_at <= 15.0 else 2

    def snapshot(self):
        with self.lock:
            status = dict(self.infer_status)
            registers = modbus.build_register_map(
                system={
                    "device_status": self._device_status(),
                    "device_serial": _read_device_serial(),
                    "cpu_pct": status.get("cpu_pct", 0),
                    "gpu_pct": status.get("gpu_pct", 0),
                    "fps": status.get("avg_total_fps", 0),
                    "temperature_c": _read_temperature_c(),
                },
                config={
                    "device_command": self.command_value,
                    "command_sequence": self.command_sequence,
                    "roi_distance_m": self.roi_distance,
                    "confidence": self.confidence,
                },
                realtime_slots=_slot_output(self._realtime_stats()),
                cumulative_slots=_slot_output(self.cumulative),
                slave_id=self.slave_id,
            )
            return {"registers": registers,
                    "window_duration_ms": int(round(self.realtime_window_sec * 1000))}

    def cumulative_snapshot(self):
        """Return the cumulative values used by FC03/menu 10 in JSON form."""
        with self.lock:
            slots = _slot_output(self.cumulative)
            return {
                "ok": True,
                "snapshot_at": time.time(),
                "cumulative_slots": [
                    {
                        "slot_index": index,
                        "object_no": key[0],
                        "direction": key[1],
                        **slots[key],
                    }
                    for index, key in enumerate(modbus.SLOT_DESCRIPTORS)
                ],
            }

    def _post_infer_config(self, start, values):
        payload = json.dumps({"start": int(start), "values": [int(v) for v in values]}).encode("utf-8")
        request = urllib.request.Request(
            INFERD_URL + "/internal/modbus/config",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=2.0):
                pass
        except Exception:
            # inferd may legitimately be stopped; it reloads the persisted
            # operation settings at the next process start.
            pass

    def _post_infer_clear(self, start):
        """Mirror one Master-originated cumulative clear into inferd's UI state."""
        payload = json.dumps({"start": int(start), "values": [0, 0]}).encode("utf-8")
        request = urllib.request.Request(
            INFERD_URL + "/internal/modbus/clear",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        last_error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=2.0):
                    return
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.2)
        # modbusd remains authoritative.  If inferd is down, its volatile UI
        # counters disappear on restart anyway.
        print(f"[modbusd] inferd clear sync skipped: {last_error!r}", flush=True)

    def _clear_slot(self, start, values):
        for base, key in zip(modbus.CUMULATIVE_SLOT_BASES, modbus.SLOT_DESCRIPTORS):
            if start == base + 3 and len(values) == 2 and values == [0, 0]:
                self.cumulative[key] = {"count": 0, "speed_sum": 0.0, "speed_square_sum": 0.0}
                self._save_state()
                return True
        return False

    def clear_slots(self, slot_indexes=None):
        """Clear selected authoritative cumulative slots for an inferd UI reset."""
        indexes = range(len(modbus.SLOT_DESCRIPTORS)) if slot_indexes is None else slot_indexes
        normalized = sorted({int(index) for index in indexes})
        if any(index < 0 or index >= len(modbus.SLOT_DESCRIPTORS) for index in normalized):
            raise ValueError("slot index must be between 0 and 5")
        with self.lock:
            for index in normalized:
                key = modbus.SLOT_DESCRIPTORS[index]
                self.cumulative[key] = {"count": 0, "speed_sum": 0.0, "speed_square_sum": 0.0}
            self._save_state()
        return normalized

    def _run_control(self, command, label, resume_inference=False):
        with self.lock:
            self.command_value = 0
        try:
            if resume_inference:
                self._set_inference_desired_running()
            subprocess.run(command, check=True, timeout=20)
            print(f"[modbusd] {label} requested", flush=True)
        except Exception as exc:
            print(f"[modbusd] {label} failed: {exc!r}", flush=True)

    def _schedule_control(self, command, label, resume_inference=False):
        # Keep the serial worker responsive after the acknowledgement.  The
        # privileged systemd action runs independently in a short-lived thread.
        threading.Thread(
            target=self._run_control,
            args=(command, label, resume_inference),
            daemon=True,
            name="ModbusControlAction",
        ).start()

    def handle_write(self, start, values):
        values = [int(value) for value in values]
        end = start + len(values) - 1 if values else start - 1
        if (values and start >= modbus.ROI_REFERENCE_DISTANCE_ADDRESS
                and end <= modbus.BBOX_CONFIDENCE_ADDRESS):
            return {} if self.apply_config(start, values, notify_inferd=True) else False
        with self.lock:
            if self._clear_slot(start, values):
                threading.Thread(
                    target=self._post_infer_clear,
                    args=(start,),
                    daemon=True,
                    name="ModbusClearSync",
                ).start()
                return {}
            if start == modbus.SYSTEM_BASE and len(values) == 1 and 1 <= values[0] <= 247:
                self.slave_id = values[0]
                self._save_settings()
                return {"slave_id": self.slave_id}
            if start == modbus.DEVICE_COMMAND_ADDRESS:
                command = values[0] if values else -1
                if command == 0:
                    self.command_value = 0
                    return {}
                if len(values) != 2 or command not in (3, 4):
                    return False
                sequence = values[1] & 0xFFFF
                if sequence == 0 or sequence == self.command_sequence:
                    return False
                self.command_value = command
                self.command_sequence = sequence
                self._save_state()
                action_cmd = INFER_RESTART_CMD if command == 3 else REBOOT_CMD
                label = "inference restart" if command == 3 else "OS reboot"
                return {
                    "post_response_action": lambda: self._schedule_control(
                        action_cmd,
                        label,
                        resume_inference=(command == 3),
                    )
                }
            if start == modbus.COMMAND_SEQUENCE_ADDRESS:
                return False
            return False

    def _poll_infer(self):
        while not self.stop_event.wait(1.0):
            try:
                with urllib.request.urlopen(INFERD_URL + "/status", timeout=2.0) as response:
                    status = json.loads(response.read().decode("utf-8"))
                signature = tuple(
                    (int(item.get("idx", 0)), int(item.get("loop_count", 0)), int(item.get("frame_idx", 0)))
                    for item in status.get("streams", [])
                )
                with self.lock:
                    self.infer_reachable = True
                    self.infer_status = status
                    if signature and signature != self.last_frame_signature:
                        self.last_frame_signature = signature
                        self.last_frame_progress_at = time.time()
            except Exception:
                with self.lock:
                    self.infer_reachable = False
                    self.last_frame_signature = None
                    self.last_frame_progress_at = 0.0

    def start(self):
        if self.runtime is not None:
            self.runtime.start()
        threading.Thread(target=self._poll_infer, daemon=True, name="InferStatusPoller").start()

    def stop(self):
        self.stop_event.set()
        if self.runtime is not None:
            self.runtime.stop()

    def health(self):
        with self.lock:
            self.realtime_window_sec = self.realtime_window_config.seconds()
            return {
                "ok": True,
                "device_status": self._device_status(),
                "infer_reachable": self.infer_reachable,
                "window_duration_ms": int(round(self.realtime_window_sec * 1000)),
                "command_sequence": self.command_sequence,
                "serial": self.runtime.get_health_snapshot() if self.runtime else {},
            }


class ApiHandler(BaseHTTPRequestHandler):
    daemon_ref = None

    def _json_response(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            return self._json_response(200, self.daemon_ref.health())
        if self.path == "/snapshot":
            return self._json_response(200, self.daemon_ref.snapshot())
        if self.path == "/counts":
            return self._json_response(200, self.daemon_ref.cumulative_snapshot())
        self._json_response(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 65536)
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if self.path == "/event":
                ok, reason = self.daemon_ref.record_event(payload)
                return self._json_response(200 if ok else 400, {"ok": ok, "reason": reason})
            if self.path == "/clear":
                indexes = payload.get("slot_indexes")
                cleared = self.daemon_ref.clear_slots(indexes)
                return self._json_response(200, {"ok": True, "cleared_slot_indexes": cleared})
            if self.path == "/config":
                start = int(payload.get("start"))
                values = [int(value) for value in payload.get("values", [])]
                ok = self.daemon_ref.apply_config(start, values, notify_inferd=False)
                return self._json_response(200 if ok else 400, {"ok": bool(ok)})
            self._json_response(404, {"error": "not found"})
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": str(exc)})

    def log_message(self, _format, *_args):
        return


def main():
    daemon = ModbusDaemon()
    ApiHandler.daemon_ref = daemon
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), ApiHandler)
    daemon.start()
    print(f"[modbusd] HTTP {HTTP_HOST}:{HTTP_PORT}; serial {daemon.port} @ {daemon.baudrate}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        daemon.stop()


if __name__ == "__main__":
    main()
