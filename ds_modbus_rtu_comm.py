import os
import threading
import time
from dataclasses import dataclass, field

import serial


DEFAULT_SLAVE_ID = int(os.environ.get("DS_CAMERA_MODBUS_SLAVE_ID", "101"), 0)
DEFAULT_PORT = os.environ.get("DS_CAMERA_MODBUS_PORT", "/dev/ttyUSB0")
DEFAULT_BAUDRATE = int(os.environ.get("DS_CAMERA_MODBUS_BAUDRATE", "460800"))
DEFAULT_TIMEOUT_SEC = float(os.environ.get("DS_CAMERA_MODBUS_TIMEOUT_SEC", "0.005"))
# USB RS-485 adapters need a brief TX-to-RX turnaround margin.  The delay is
# intentionally configurable for adapters with different automatic direction logic.
RESPONSE_TURNAROUND_SEC = float(os.environ.get("DS_CAMERA_MODBUS_RESPONSE_TURNAROUND_SEC", "0.003"))
SERIAL_RECONNECT_DELAY_SEC = 1.0
SERIAL_ERROR_LOG_INTERVAL_SEC = max(
    60.0,
    float(os.environ.get("DS_CAMERA_MODBUS_ERROR_LOG_INTERVAL_SEC", "3600")),
)

FC_READ_HOLDING_REGISTERS = 0x03
FC_WRITE_MULTIPLE_REGISTERS = 0x10

EX_ILLEGAL_FUNCTION = 0x01
EX_ILLEGAL_DATA_ADDRESS = 0x02
EX_ILLEGAL_DATA_VALUE = 0x03

SYSTEM_BASE = 0x0100
SYSTEM_END = 0x010D
DEVICE_COMMAND_ADDRESS = 0x010A
COMMAND_SEQUENCE_ADDRESS = 0x010B
ROI_REFERENCE_DISTANCE_ADDRESS = 0x010C
BBOX_CONFIDENCE_ADDRESS = 0x010D
REALTIME_SLOT_BASES = (0x0500, 0x0600, 0x0700, 0x0800, 0x0900, 0x0A00)
CUMULATIVE_SLOT_BASES = (0x1000, 0x1100, 0x1200, 0x1300, 0x1400, 0x1500)
BLOCK_REGISTER_COUNT = 55
MAX_READ_REGISTERS = 125
MAX_WRITE_REGISTERS = 123

# Slot order is fixed by the v0.2 protocol document: object x direction.
SLOT_DESCRIPTORS = (
    (0, 1),  # person, direction 1
    (0, 2),  # person, direction 2
    (1, 1),  # vehicle, direction 1
    (1, 2),  # vehicle, direction 2
    (2, 1),  # motorbike, direction 1
    (2, 2),  # motorbike, direction 2
)


def crc16_modbus(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def append_crc(data):
    crc = crc16_modbus(data)
    return data + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def _u16(value):
    return max(0, min(0xFFFF, int(round(float(value or 0)))))


def _slot_registers(object_no, direction_code, values):
    """Encode one v0.2 slot's first six holding registers."""
    values = values or {}
    count = int(values.get("count", 0) or 0)
    registers = [0] * BLOCK_REGISTER_COUNT
    if count <= 0:
        return registers

    average_speed = float(values.get("average_speed_kph", 0.0) or 0.0)
    stddev_speed = float(values.get("speed_stddev_kph", 0.0) or 0.0)
    registers[0] = 5
    registers[1] = _u16(object_no)
    registers[2] = _u16(direction_code)
    registers[3] = _u16(count)
    registers[4] = _u16(average_speed * 10)
    registers[5] = _u16(stddev_speed * 10)
    return registers


def build_register_map(system=None, config=None, realtime_slots=None,
                       cumulative_slots=None, slave_id=DEFAULT_SLAVE_ID):
    """Build the holding-register map defined by the v0.2 protocol document."""
    system = system or {}
    config = config or {}
    realtime_slots = realtime_slots or {}
    cumulative_slots = cumulative_slots or {}
    serial_number = max(0, int(system.get("device_serial", 0) or 0))
    registers = {
        0x0100: _u16(slave_id),
        0x0101: _u16(system.get("device_status", 2)),
        0x0102: (serial_number >> 48) & 0xFFFF,
        0x0103: (serial_number >> 32) & 0xFFFF,
        0x0104: (serial_number >> 16) & 0xFFFF,
        0x0105: serial_number & 0xFFFF,
        0x0106: _u16(system.get("cpu_pct", 0)),
        0x0107: _u16(system.get("gpu_pct", 0)),
        0x0108: _u16(float(system.get("fps", 0) or 0) * 10),
        0x0109: _u16(float(system.get("temperature_c", 0) or 0) * 10),
        0x010A: _u16(config.get("device_command", 0)),
        0x010B: _u16(config.get("command_sequence", 0)),
        0x010C: _u16(float(config.get("roi_distance_m", 0) or 0) * 100),
        0x010D: _u16(float(config.get("confidence", 0) or 0) * 100),
    }

    for bases, slot_stats in (
        (REALTIME_SLOT_BASES, realtime_slots),
        (CUMULATIVE_SLOT_BASES, cumulative_slots),
    ):
        for base, descriptor in zip(bases, SLOT_DESCRIPTORS):
            for offset, value in enumerate(_slot_registers(*descriptor, slot_stats.get(descriptor))):
                registers[base + offset] = value
    return registers


def registers_to_bytes(values):
    out = bytearray()
    for value in values:
        out += _u16(value).to_bytes(2, "big")
    return bytes(out)


def read_registers(registers, start, quantity):
    return [int(registers.get(start + offset, 0)) for offset in range(quantity)]


def validate_address_range(start, quantity):
    if quantity <= 0:
        return False
    end = start + quantity - 1
    ranges = [(SYSTEM_BASE, SYSTEM_END)]
    ranges.extend((base, base + BLOCK_REGISTER_COUNT - 1) for base in REALTIME_SLOT_BASES)
    ranges.extend((base, base + BLOCK_REGISTER_COUNT - 1) for base in CUMULATIVE_SLOT_BASES)
    return any(start >= lo and end <= hi for lo, hi in ranges)


def validate_write_address_range(start, quantity):
    if quantity <= 0:
        return False
    if start == SYSTEM_BASE and quantity == 1:
        return True
    if start == DEVICE_COMMAND_ADDRESS and quantity in (1, 2):
        return True
    if start == COMMAND_SEQUENCE_ADDRESS and quantity == 1:
        return True
    if ROI_REFERENCE_DISTANCE_ADDRESS <= start and start + quantity - 1 <= BBOX_CONFIDENCE_ADDRESS:
        return True
    return any(start == base + 3 and quantity == 2 for base in CUMULATIVE_SLOT_BASES)


@dataclass
class ModbusStatus:
    ok: bool = False
    port: str = DEFAULT_PORT
    baudrate: int = DEFAULT_BAUDRATE
    slave_id: int = DEFAULT_SLAVE_ID
    started_at: float = field(default_factory=time.time)
    last_request_at: float = 0.0
    last_response_at: float = 0.0
    last_error: str = ""
    rx_count: int = 0
    tx_count: int = 0
    crc_errors: int = 0
    exception_count: int = 0
    thread_alive: bool = False

    def snapshot(self):
        now = time.time()
        return {
            "ok": self.ok,
            "port": self.port,
            "baudrate": self.baudrate,
            "slave_id": self.slave_id,
            "thread_alive": self.thread_alive,
            "started_at": self.started_at,
            "last_request_at": self.last_request_at,
            "last_response_at": self.last_response_at,
            "last_request_age": None if not self.last_request_at else now - self.last_request_at,
            "last_error": self.last_error,
            "rx_count": self.rx_count,
            "tx_count": self.tx_count,
            "crc_errors": self.crc_errors,
            "exception_count": self.exception_count,
        }


class ModbusRTUSlaveRuntime:
    def __init__(
        self,
        port=DEFAULT_PORT,
        baudrate=DEFAULT_BAUDRATE,
        slave_id=DEFAULT_SLAVE_ID,
        snapshot_provider=None,
        write_handler=None,
        timeout_sec=DEFAULT_TIMEOUT_SEC,
    ):
        self.port = port
        self.baudrate = int(baudrate)
        self.slave_id = int(slave_id)
        self.snapshot_provider = snapshot_provider
        self.write_handler = write_handler
        self.timeout_sec = float(timeout_sec)
        self.status = ModbusStatus(port=port, baudrate=self.baudrate, slave_id=self.slave_id)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._serial = None
        self._post_response_action = None
        self._last_disconnect_log_at = 0.0
        self._last_disconnect_error = ""
        self._suppressed_disconnect_logs = 0
        self._was_disconnected = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ModbusRTUSlave")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def get_health_snapshot(self):
        with self._lock:
            self.status.thread_alive = bool(self._thread and self._thread.is_alive())
            return self.status.snapshot()

    def _record_error(self, message):
        with self._lock:
            self.status.ok = False
            self.status.last_error = str(message)

    def _log_serial_disconnect(self, exc):
        now = time.monotonic()
        error_text = repr(exc)
        should_log = (
            error_text != self._last_disconnect_error
            or now - self._last_disconnect_log_at >= SERIAL_ERROR_LOG_INTERVAL_SEC
        )
        self._was_disconnected = True
        if not should_log:
            self._suppressed_disconnect_logs += 1
            return
        suppressed = (
            f"; suppressed={self._suppressed_disconnect_logs} retries"
            if self._suppressed_disconnect_logs else ""
        )
        print(
            f"[ModbusRTU] serial disconnected: {error_text}; "
            f"retrying every {SERIAL_RECONNECT_DELAY_SEC:.0f}s{suppressed}",
            flush=True,
        )
        self._last_disconnect_log_at = now
        self._last_disconnect_error = error_text
        self._suppressed_disconnect_logs = 0

    def _log_serial_reconnected(self):
        if not self._was_disconnected:
            return
        print(
            f"[ModbusRTU] serial reconnected; "
            f"suppressed={self._suppressed_disconnect_logs} retry logs",
            flush=True,
        )
        self._was_disconnected = False
        self._last_disconnect_error = ""
        self._last_disconnect_log_at = 0.0
        self._suppressed_disconnect_logs = 0

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    self._serial = serial.Serial(
                        self.port,
                        baudrate=self.baudrate,
                        bytesize=8,
                        parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE,
                        timeout=self.timeout_sec,
                        write_timeout=1.0,
                    )
                    with self._lock:
                        self.status.ok = True
                        self.status.last_error = ""
                    self._log_serial_reconnected()
                    print(
                        f"[ModbusRTU] slave ready port={self.port} baudrate={self.baudrate} "
                        f"slave=0x{self.slave_id:02X}",
                        flush=True,
                    )
                    self._serve()
                except (serial.SerialException, OSError) as exc:
                    if not self._stop.is_set():
                        self._record_error(repr(exc))
                        self._log_serial_disconnect(exc)
                except Exception as exc:
                    if not self._stop.is_set():
                        self._record_error(repr(exc))
                        print(f"[ModbusRTU] stopped: {exc!r}", flush=True)
                    break
                finally:
                    serial_port = self._serial
                    self._serial = None
                    if serial_port is not None:
                        try:
                            serial_port.close()
                        except Exception:
                            pass
                if not self._stop.is_set():
                    self._stop.wait(SERIAL_RECONNECT_DELAY_SEC)
        finally:
            with self._lock:
                self.status.ok = False

    def _serve(self):
        buffer = bytearray()
        last_rx = 0.0
        while not self._stop.is_set():
            chunk = self._serial.read(256)
            now = time.time()
            if chunk:
                buffer.extend(chunk)
                last_rx = now
                continue
            if buffer and (now - last_rx) >= self.timeout_sec:
                frame = bytes(buffer)
                buffer.clear()
                response = self.handle_frame(frame)
                if response:
                    # Detach the action before touching the UART.  If write or
                    # flush fails, it is intentionally discarded and can only
                    # be retried with a new command sequence.
                    action = self._post_response_action
                    self._post_response_action = None
                    if RESPONSE_TURNAROUND_SEC > 0:
                        self._stop.wait(RESPONSE_TURNAROUND_SEC)
                    self._serial.write(response)
                    self._serial.flush()
                    with self._lock:
                        self.status.tx_count += 1
                        self.status.last_response_at = time.time()
                    # Commands such as process restart/reboot must run only
                    # after the FC16 acknowledgement has left the UART.
                    if callable(action):
                        try:
                            action()
                        except Exception as exc:
                            self._record_error(f"post-response action failed: {exc!r}")
                            print(f"[ModbusRTU] post-response action failed: {exc!r}", flush=True)

    def _exception_response(self, function_code, exception_code):
        with self._lock:
            self.status.exception_count += 1
        return append_crc(bytes((self.slave_id, function_code | 0x80, exception_code)))

    def handle_frame(self, frame):
        if len(frame) < 4:
            return b""
        address = frame[0]
        # Slave ID 0 is the Modbus broadcast address.  This product does not
        # accept broadcast writes because a restart/reboot must be addressed
        # explicitly to one device.  Broadcast reads are also silent per RTU.
        if address == 0:
            return b""
        if address != self.slave_id:
            return b""
        body = frame[:-2]
        got_crc = frame[-2] | (frame[-1] << 8)
        if crc16_modbus(body) != got_crc:
            with self._lock:
                self.status.crc_errors += 1
                self.status.last_error = f"CRC error frame={frame.hex(' ')}"
            return b""

        with self._lock:
            self.status.rx_count += 1
            self.status.last_request_at = time.time()

        function_code = frame[1]
        if function_code == FC_READ_HOLDING_REGISTERS:
            response = self._handle_read(function_code, frame)
        elif function_code == FC_WRITE_MULTIPLE_REGISTERS:
            response = self._handle_fc16(frame)
        else:
            response = self._exception_response(function_code, EX_ILLEGAL_FUNCTION)
        return response

    def _snapshot_registers(self):
        if callable(self.snapshot_provider):
            snapshot = self.snapshot_provider() or {}
            registers = snapshot.get("registers")
            if isinstance(registers, dict):
                return registers
        return build_register_map(slave_id=self.slave_id)

    def _handle_read(self, function_code, frame):
        if len(frame) != 8:
            return self._exception_response(function_code, EX_ILLEGAL_DATA_VALUE)
        start = int.from_bytes(frame[2:4], "big")
        quantity = int.from_bytes(frame[4:6], "big")
        if quantity <= 0 or quantity > MAX_READ_REGISTERS:
            return self._exception_response(function_code, EX_ILLEGAL_DATA_VALUE)
        if not validate_address_range(start, quantity):
            return self._exception_response(function_code, EX_ILLEGAL_DATA_ADDRESS)

        values = read_registers(self._snapshot_registers(), start, quantity)
        payload = registers_to_bytes(values)
        return append_crc(bytes((self.slave_id, function_code, len(payload))) + payload)

    def _handle_fc16(self, frame):
        if len(frame) < 9:
            return self._exception_response(FC_WRITE_MULTIPLE_REGISTERS, EX_ILLEGAL_DATA_VALUE)
        start = int.from_bytes(frame[2:4], "big")
        quantity = int.from_bytes(frame[4:6], "big")
        byte_count = frame[6]
        if len(frame) != 9 + byte_count or quantity <= 0 or quantity > MAX_WRITE_REGISTERS:
            return self._exception_response(FC_WRITE_MULTIPLE_REGISTERS, EX_ILLEGAL_DATA_VALUE)
        if byte_count != quantity * 2 or not validate_write_address_range(start, quantity):
            return self._exception_response(FC_WRITE_MULTIPLE_REGISTERS, EX_ILLEGAL_DATA_ADDRESS)
        values = [
            int.from_bytes(frame[7 + index * 2:9 + index * 2], "big")
            for index in range(quantity)
        ]
        result = self.write_handler(start, values) if callable(self.write_handler) else None
        if result is False or result is None:
            return self._exception_response(FC_WRITE_MULTIPLE_REGISTERS, EX_ILLEGAL_DATA_VALUE)
        response = append_crc(
            bytes((self.slave_id, FC_WRITE_MULTIPLE_REGISTERS))
            + start.to_bytes(2, "big")
            + quantity.to_bytes(2, "big")
        )
        new_slave_id = result.get("slave_id") if isinstance(result, dict) else None
        post_response_action = result.get("post_response_action") if isinstance(result, dict) else None
        if new_slave_id is not None:
            self.slave_id = int(new_slave_id)
            with self._lock:
                self.status.slave_id = self.slave_id
        if callable(post_response_action):
            self._post_response_action = post_response_action
        return response
