# Modbus RTU communication and watchdog layer.

import os
import queue
import threading
import time
import urllib.request

import ds_modbus_rtu_comm as _modbus_comm
from modbus_bridge import publisher as _modbus_event_publisher
from gpio_pulse import output as _gpio_pulse_output

# This distribution intentionally supports Modbus RTU only. Keeping the mode
# fixed here also removes the aiocoap/lwm2m_client runtime dependency.
_COMM_MODE = 'modbus_rtu'
_MODBUS_EXTERNAL = os.environ.get('DS_CAMERA_MODBUS_EXTERNAL', '0').strip().lower() in (
    '1', 'true', 'yes', 'on'
)
_MODBUSD_URL = os.environ.get('DS_CAMERA_MODBUSD_URL', 'http://127.0.0.1:5123').rstrip('/')
_modbusd_counts_lock = threading.Lock()
_modbusd_counts_cache = None
_modbusd_counts_fetched_at = 0.0
_lwm2m_comm = None
LwM2MRuntime = None


_LWM2M_INSTANCE_TO_CLASSES = {
    0: ('person',),
    1: ('car',),
    2: ('bus',),
    3: ('truck',),
    4: ('motorbike', 'motorcycle'),
}

_LWM2M_SERVER = os.environ.get('DS_CAMERA_LWM2M_SERVER', '59.3.3.162')
_LWM2M_SERVER_PORT = int(os.environ.get('DS_CAMERA_LWM2M_SERVER_PORT', '5689'))
_LWM2M_ADDRESS = os.environ.get('DS_CAMERA_LWM2M_ADDRESS', '0.0.0.0')
_LWM2M_CLIENT_PORT = int(os.environ.get('DS_CAMERA_LWM2M_CLIENT_PORT', '5689'))
_LWM2M_INTERVAL_SEC = float(os.environ.get('DS_CAMERA_LWM2M_INTERVAL_SEC', '600'))
_LWM2M_HEALTH_TIMEOUT_SEC = max(60.0, float(os.environ.get('DS_CAMERA_LWM2M_HEALTH_TIMEOUT_SEC', '600')))
_WATCHDOG_TIMEOUT_SEC = max(20.0, float(os.environ.get('DS_CAMERA_WATCHDOG_TIMEOUT_SEC', '60')))
_MODBUS_PORT = os.environ.get('DS_CAMERA_MODBUS_PORT', _MODBUS_SETTINGS.get('port', '/dev/ttyUSB0'))
_MODBUS_BAUDRATE = int(os.environ.get('DS_CAMERA_MODBUS_BAUDRATE', _MODBUS_SETTINGS.get('baudrate', 460800)))
_modbus_slave_id_value = os.environ.get('DS_CAMERA_MODBUS_SLAVE_ID', _MODBUS_SETTINGS.get('slave_id', 101))
_MODBUS_SLAVE_ID = int(_modbus_slave_id_value, 0) if isinstance(_modbus_slave_id_value, str) else int(_modbus_slave_id_value)
_MODBUS_TIMEOUT_SEC = float(os.environ.get('DS_CAMERA_MODBUS_TIMEOUT_SEC', _MODBUS_SETTINGS.get('timeout_seconds', 0.005)))

_service_stop = threading.Event()
_lwm2m_runtime = None
_modbus_runtime = None
_modbus_snapshot_thread = None
_modbus_snapshot_lock = threading.Lock()
_modbus_snapshot = None
_modbus_window_condition = threading.Condition()
_modbus_window_version = 0
_roi_event_lock = threading.Lock()
_roi_event_sequence = 0
_roi_event_latest = {}
_roi_event_history = deque(maxlen=1024)
_MODBUS_WINDOW_REPORT_FILE = os.path.join(RESULT_LOG_DIR, 'modbus_moving_window.txt')
_MODBUS_WINDOW_REPORT_ENABLED = os.environ.get(
    'DS_CAMERA_MODBUS_WINDOW_REPORT_LOGGING', '0'
).strip().lower() in ('1', 'true', 'yes', 'on')
_MODBUS_CUMULATIVE_COUNT_MAX = 0xFFFF
_lwm2m_request_queue = queue.SimpleQueue()
_comm_thread = None
_watchdog_thread = None
_comm_heartbeat = time.time()
_lwm2m_start_lock = threading.Lock()
_db_lock = threading.Lock()
_db_next_auto_publish_at = 0.0
_db_last_publish = {
    'ok': False,
    'reason': '',
    'sent_at': '',
    'error': '',
    'format_valid': False,
    'format_errors': [],
}


def _read_board_temperature_c():
    paths = [
        '/sys/devices/virtual/thermal/thermal_zone0/temp',
        '/sys/class/thermal/thermal_zone0/temp',
    ]
    for path in paths:
        try:
            raw = open(path, 'r').read().strip()
            value = float(raw)
            return value / 1000.0 if value > 1000 else value
        except Exception:
            continue
    return 0.0


def _norm_class_name(name):
    name = str(name or '').strip().lower()
    if name in ('motorcycle', 'motorbike', 'bike'):
        return 'motorbike'
    return name


def _modbus_object_no(class_name):
    name = _norm_class_name(class_name)
    if name == 'person':
        return 1
    if name in ('car', 'bus', 'truck'):
        return 2
    if name == 'motorbike':
        return 3
    return 0


def _record_modbus_roi_event(class_name, direction, speed_kph):
    """Capture one confirmed ROI-exit event for the real-time Modbus register."""
    global _roi_event_sequence, _roi_event_latest
    object_no = _modbus_object_no(class_name)
    if object_no == 0:
        return
    direction_no = 1 if direction == '상행' else 2
    event_speed = 0 if object_no == 1 else int(round(float(speed_kph or 0)))
    with _roi_event_lock:
        _roi_event_sequence = (_roi_event_sequence + 1) & 0xFFFF
        if _roi_event_sequence == 0:
            _roi_event_sequence = 1
        _roi_event_latest = {
            'object_no': object_no,
            'speed_kph': event_speed,
            'direction': direction_no,
            'sequence': _roi_event_sequence,
            'event_at': time.time(),
        }
        _roi_event_history.append(dict(_roi_event_latest))
    _gpio_pulse_output.trigger()
    if _MODBUS_EXTERNAL:
        _modbus_event_publisher.publish(class_name, direction, speed_kph)
    _notify_modbus_window_changed()


def _get_latest_modbus_roi_event():
    with _roi_event_lock:
        return dict(_roi_event_latest)


def _read_edgeai_device_serial():
    """Return Jetson's numeric device-tree serial for System Register 0102H~0105H."""
    for path in ('/sys/firmware/devicetree/base/serial-number', '/proc/device-tree/serial-number'):
        try:
            raw = open(path, 'rb').read().rstrip(b'\0').decode('ascii').strip()
            return int(raw, 10)
        except Exception:
            continue
    return 0


def _modbus_slot_key(event):
    name = _norm_class_name(event.get('class_name'))
    if name == 'person':
        object_no = 0
    elif name in ('car', 'bus', 'truck', 'vehicle'):
        object_no = 1
    elif name == 'motorbike':
        object_no = 2
    else:
        return None
    return object_no, (1 if event.get('direction') == '상행' else 2)


def _empty_modbus_slot_stats():
    return {key: {'count': 0, 'speed_sum': 0.0, 'speed_square_sum': 0.0}
            for key in _modbus_comm.SLOT_DESCRIPTORS}


def _accumulate_modbus_cumulative_event(ss, event):
    """Add one ROI exit to its bounded FC16-reset cumulative slot.

    Each Modbus count register is uint16.  When a slot has reached 65535,
    the next event rolls that slot back to zero; the following event starts
    the next count cycle at one.
    """
    key = _modbus_slot_key(event)
    if key is None:
        return
    stats = ss.setdefault('modbus_cumulative_stats', _empty_modbus_slot_stats())
    item = stats.setdefault(key, {'count': 0, 'speed_sum': 0.0, 'speed_square_sum': 0.0})
    if item['count'] >= _MODBUS_CUMULATIVE_COUNT_MAX:
        stats[key] = {'count': 0, 'speed_sum': 0.0, 'speed_square_sum': 0.0}
        return
    item['count'] += 1
    if key[0] != 0:
        speed = float(event.get('speed_kph', 0.0) or 0.0)
        item['speed_sum'] += speed
        item['speed_square_sum'] += speed * speed


def _build_modbus_slot_stats(realtime=False):
    """Aggregate ROI-exit events by the v0.2 object/direction slot layout."""
    stats = _empty_modbus_slot_stats()
    if not realtime:
        for ss in list(_streams):
            with ss['_lock']:
                source = ss.get('modbus_cumulative_stats', {})
                for key, item in source.items():
                    target = stats[key]
                    target['count'] += int(item.get('count', 0))
                    target['speed_sum'] += float(item.get('speed_sum', 0.0))
                    target['speed_square_sum'] += float(item.get('speed_square_sum', 0.0))
        cutoff = None
    else:
        cutoff = time.monotonic() - _refresh_realtime_window()
    for ss in list(_streams):
        with ss['_lock']:
            events = _rolling_window_events(ss) if realtime else ()
        for event in events:
            if cutoff is not None and float(event.get('event_time', 0.0)) <= cutoff:
                continue
            key = _modbus_slot_key(event)
            if key is None:
                continue
            item = stats[key]
            item['count'] = min(_MODBUS_CUMULATIVE_COUNT_MAX, item['count'] + 1)
            # Person slots are passage-count only; speed fields remain zero.
            if key[0] == 0:
                continue
            speed = float(event.get('speed_kph', 0.0) or 0.0)
            item['speed_sum'] += speed
            item['speed_square_sum'] += speed * speed

    for key, item in stats.items():
        count = item['count']
        if count and key[0] != 0:
            average = item['speed_sum'] / count
            variance = max(0.0, item['speed_square_sum'] / count - average * average)
            item['average_speed_kph'] = average
            item['speed_stddev_kph'] = variance ** 0.5
        else:
            item['average_speed_kph'] = 0.0
            item['speed_stddev_kph'] = 0.0
    return stats


def _clear_modbus_cumulative_slot(start, values):
    """Clear only the cumulative object/direction slot selected by FC16 [0, 0]."""
    if values != [0, 0]:
        return False
    descriptor = None
    for base, candidate in zip(_modbus_comm.CUMULATIVE_SLOT_BASES, _modbus_comm.SLOT_DESCRIPTORS):
        if start == base + 3:
            descriptor = candidate
            break
    if descriptor is None:
        return False
    removed_count = 0
    for ss in list(_streams):
        with ss['_lock']:
            stats = ss.setdefault('modbus_cumulative_stats', _empty_modbus_slot_stats())
            item = stats.setdefault(descriptor, {'count': 0, 'speed_sum': 0.0, 'speed_square_sum': 0.0})
            removed_count += int(item['count'])
            stats[descriptor] = {'count': 0, 'speed_sum': 0.0, 'speed_square_sum': 0.0}
    _notify_modbus_window_changed()
    _refresh_modbus_snapshot()
    print(
        f'[ModbusRTU] cumulative clear start=0x{start:04X} '
        f'slot={descriptor} removed_events={removed_count}',
        flush=True,
    )
    return True


def _empty_opt_stats():
    return {
        'person': {'roi_count': 0, 'A2B': 0, 'B2A': 0, 'avg_speed': 0.0, 'avg_speed_A2B': 0.0, 'avg_speed_B2A': 0.0},
        'car': {'roi_count': 0, 'A2B': 0, 'B2A': 0, 'avg_speed': 0.0, 'avg_speed_A2B': 0.0, 'avg_speed_B2A': 0.0},
        'truck': {'roi_count': 0, 'A2B': 0, 'B2A': 0, 'avg_speed': 0.0, 'avg_speed_A2B': 0.0, 'avg_speed_B2A': 0.0},
        'bus': {'roi_count': 0, 'A2B': 0, 'B2A': 0, 'avg_speed': 0.0, 'avg_speed_A2B': 0.0, 'avg_speed_B2A': 0.0},
        'motorbike': {'roi_count': 0, 'A2B': 0, 'B2A': 0, 'avg_speed': 0.0, 'avg_speed_A2B': 0.0, 'avg_speed_B2A': 0.0},
    }


def _build_optimize_stats_from_streams():
    stats = _empty_opt_stats()
    speed_weight = {
        cname: {'A2B': [0.0, 0], 'B2A': [0.0, 0]}
        for cname in stats
    }
    for ss in _streams:
        with ss['_lock']:
            events = _rolling_window_events(ss)
        for event in events:
            cname = _norm_class_name(event.get('class_name'))
            direction = 'A2B' if event.get('direction') == '상행' else 'B2A'
            if cname not in stats:
                continue
            stats[cname][direction] += 1
            speed_weight[cname][direction][0] += float(event.get('speed_kph', 0.0) or 0.0)
            speed_weight[cname][direction][1] += 1

    for cname, dirs in speed_weight.items():
        a_sum, a_n = dirs['A2B']
        b_sum, b_n = dirs['B2A']
        if a_n:
            stats[cname]['avg_speed_A2B'] = a_sum / a_n
        if b_n:
            stats[cname]['avg_speed_B2A'] = b_sum / b_n
        total_n = a_n + b_n
        if total_n:
            stats[cname]['avg_speed'] = (a_sum + b_sum) / total_n
    return stats


def _validate_13432_payload(obj_13432):
    errors = []
    if not isinstance(obj_13432, dict):
        return False, ['13432 payload must be an object']
    expected = {
        '0': {'type': 47, 'speed': False},
        '1': {'type': 32, 'speed': True},
        '2': {'type': 6, 'speed': True},
        '3': {'type': 24, 'speed': True},
        '4': {'type': 28, 'speed': True},
    }
    for inst_id, spec in expected.items():
        inst = obj_13432.get(inst_id)
        if not isinstance(inst, dict):
            errors.append(f'13432/{inst_id} missing')
            continue
        if int(inst.get('0', -1)) != spec['type']:
            errors.append(f'13432/{inst_id}/0 type mismatch')
        counts = inst.get('1')
        if not isinstance(counts, dict):
            errors.append(f'13432/{inst_id}/1 counts missing')
        else:
            for direction_id in ('0', '1'):
                if not isinstance(counts.get(direction_id), int):
                    errors.append(f'13432/{inst_id}/1/{direction_id} must be int')
        speeds = inst.get('2')
        if spec['speed']:
            if not isinstance(speeds, dict):
                errors.append(f'13432/{inst_id}/2 speeds missing')
            else:
                for direction_id in ('0', '1'):
                    if not isinstance(speeds.get(direction_id), int):
                        errors.append(f'13432/{inst_id}/2/{direction_id} must be int')
        elif speeds is not None:
            errors.append(f'13432/{inst_id}/2 must be absent for person')
    return len(errors) == 0, errors


def _json_safe_modbus_snapshot(snapshot):
    """Convert tuple slot keys only at the HTTP/JSON boundary."""
    result = dict(snapshot or {})
    for field in ('realtime_slots', 'cumulative_slots'):
        slots = result.get(field, {})
        if isinstance(slots, dict):
            result[field] = {
                f'{key[0]}:{key[1]}' if isinstance(key, tuple) else str(key): value
                for key, value in slots.items()
            }
    return result


def _build_db_payload_snapshot():
    if _COMM_MODE == 'modbus_rtu':
        return _json_safe_modbus_snapshot(_build_modbus_payload_snapshot())
    stats = _build_optimize_stats_from_streams()
    obj_13432 = _lwm2m_comm.build_13432_from_stats(stats)
    ok, errors = _validate_13432_payload(obj_13432)
    return {
        'object_id': '13432',
        'stats': stats,
        'payload': {'13432': obj_13432},
        'format_valid': ok,
        'format_errors': errors,
        'format': 'LwM2M Object 13432 JSON model',
    }


def _build_modbus_payload_snapshot_live():
    total_fps_values = [float(ss.get('total_fps', 0.0) or 0.0) for ss in _streams]
    system = {
        'device_status': 1 if _st.get('running') else 2,
        'device_serial': _read_edgeai_device_serial(),
        'cpu_pct': _read_cpu_pct(),
        'gpu_pct': _read_gpu_pct(),
        'fps': sum(total_fps_values) / len(total_fps_values) if total_fps_values else 0.0,
        'temperature_c': _read_board_temperature_c(),
    }
    config = {
        'roi_distance_m': _ROI_DEFAULT_REF_DISTANCE_M,
        'confidence': _ROI_DEFAULT_CONFIDENCE,
    }
    realtime_slots = _build_modbus_slot_stats(realtime=True)
    cumulative_slots = _build_modbus_slot_stats(realtime=False)
    registers = _modbus_comm.build_register_map(
        system=system,
        config=config,
        realtime_slots=realtime_slots,
        cumulative_slots=cumulative_slots,
        slave_id=_MODBUS_SLAVE_ID,
    )
    return {
        'object_id': 'modbus_rtu',
        'system': system,
        'config': config,
        'realtime_slots': realtime_slots,
        'cumulative_slots': cumulative_slots,
        'registers': registers,
        'rolling_window_seconds': None,
        'format_valid': True,
        'format_errors': [],
        'format': 'Modbus RTU register map',
    }


def _build_speed_trends(current_stats, previous_stats):
    trends = {}
    for cname, current in current_stats.items():
        previous = previous_stats.get(cname, {}) if previous_stats else {}
        trends[cname] = {}
        for direction in ('A2B', 'B2A'):
            current_count = int(current.get(direction, 0) or 0)
            previous_count = int(previous.get(direction, 0) or 0)
            current_speed = float(current.get(f'avg_speed_{direction}', 0.0) or 0.0)
            previous_speed = float(previous.get(f'avg_speed_{direction}', 0.0) or 0.0)
            trends[cname][direction] = (
                current_speed - previous_speed if current_count > 0 and previous_count > 0 else None
            )
    return trends


def _write_modbus_window_report(snapshot):
    """Write the exact current event window for field verification."""
    if not _MODBUS_WINDOW_REPORT_ENABLED:
        return
    now_monotonic = time.monotonic()
    rows = []
    for stream_index, ss in enumerate(list(_streams)):
        with ss['_lock']:
            events = _rolling_window_events(ss, now_monotonic)
        for event in events:
            rows.append((stream_index, event))
    rows.sort(key=lambda item: float(item[1].get('event_time', 0.0)))

    lines = [
        'Modbus RTU Cumulative ROI Event Report',
        f'generated_at={time.strftime("%Y-%m-%d %H:%M:%S")}',
        'aggregation_boundary=FC16 cumulative slot clear',
        f'active_event_count={len(rows)}',
        '',
        'stream\trecorded_at\tclass\tdirection\tspeed_kph',
    ]
    for stream_index, event in rows:
        recorded_at = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(event.get('recorded_at', time.time())))
        lines.append(
            f'{stream_index}\t{recorded_at}\t{event.get("class_name", "")}\t'
            f'{event.get("direction", "")}\t{float(event.get("speed_kph", 0.0)):.1f}'
        )
    lines.extend(('', 'cumulative_slots=', repr(snapshot.get('cumulative_slots', {}))))

    tmp = _MODBUS_WINDOW_REPORT_FILE + '.tmp'
    try:
        os.makedirs(os.path.dirname(_MODBUS_WINDOW_REPORT_FILE), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        os.replace(tmp, _MODBUS_WINDOW_REPORT_FILE)
    except OSError as exc:
        # Diagnostic logging must never terminate the Modbus snapshot thread.
        print(f'[ModbusRTU] window report write skipped: {exc!r}', flush=True)


def _refresh_modbus_snapshot():
    """Freeze one complete analysis result for all Modbus reads in this interval."""
    global _modbus_snapshot
    snapshot = _build_modbus_payload_snapshot_live()
    with _modbus_snapshot_lock:
        snapshot['snapshot_at'] = time.time()
        _modbus_snapshot = snapshot
    _write_modbus_window_report(snapshot)
    return snapshot


def _build_modbus_payload_snapshot():
    # FC03 reads the configured real-time window rather than a stale cache.
    return _refresh_modbus_snapshot()


def _handle_modbus_config_write(start, values):
    """Validate, apply, and persist one FC16 System/Configuration write."""
    global _MODBUS_SLAVE_ID, _ROI_DEFAULT_REF_DISTANCE_M, _ROI_DEFAULT_CONFIDENCE
    if not values:
        return False

    end = start + len(values) - 1
    if _clear_modbus_cumulative_slot(start, values):
        return {}

    if start == _modbus_comm.SYSTEM_BASE:
        if len(values) != 1 or not 1 <= values[0] <= 247:
            return False
        new_slave_id = int(values[0])
        _mutate_operation_settings(
            lambda settings: settings.setdefault('modbus_rtu', {}).update(
                {'slave_id': new_slave_id}
            )
        )
        old_slave_id = _MODBUS_SLAVE_ID
        _MODBUS_SLAVE_ID = new_slave_id
        _refresh_modbus_snapshot()
        print(
            f'[ModbusRTU] slave id changed 0x{old_slave_id:02X} -> 0x{new_slave_id:02X}',
            flush=True,
        )
        # The runtime sends this FC16 acknowledgement with the old address,
        # then changes its address before it accepts the next request.
        return {'slave_id': new_slave_id}

    if start == _modbus_comm.DEVICE_COMMAND_ADDRESS:
        # Command execution is deliberately deferred to the daemon/service layer.
        # Accepting 0 keeps the command register writable without side effects.
        return {} if values and values[0] == 0 else False

    if start == _modbus_comm.COMMAND_SEQUENCE_ADDRESS:
        return {}

    if start < _modbus_comm.ROI_REFERENCE_DISTANCE_ADDRESS or end > _modbus_comm.BBOX_CONFIDENCE_ADDRESS:
        return False

    candidate_distance = _ROI_DEFAULT_REF_DISTANCE_M
    candidate_confidence = _ROI_DEFAULT_CONFIDENCE
    for offset, value in enumerate(values):
        address = start + offset
        if address == _modbus_comm.ROI_REFERENCE_DISTANCE_ADDRESS:
            candidate_distance = value / 100.0
            if not 0.1 <= candidate_distance <= 655.35:
                return False
        elif address == _modbus_comm.BBOX_CONFIDENCE_ADDRESS:
            candidate_confidence = value / 100.0
            if not 0.0 <= candidate_confidence <= 1.0:
                return False

    def update_settings(settings):
        settings.setdefault('roi_defaults', {}).update({
            'ref_distance_m': candidate_distance,
            'confidence_threshold': candidate_confidence,
        })
        startup = settings.setdefault('startup', {})
        startup['confidence'] = candidate_confidence
        existing_conf_list = startup.get('conf_list')
        list_size = max(1, len(existing_conf_list)) if isinstance(existing_conf_list, list) else 1
        startup['conf_list'] = [candidate_confidence] * list_size

    _mutate_operation_settings(update_settings)

    _ROI_DEFAULT_REF_DISTANCE_M = candidate_distance
    _ROI_DEFAULT_CONFIDENCE = candidate_confidence
    _st['conf'] = candidate_confidence
    streams_to_persist = []
    for ss in list(_streams):
        with ss['_lock']:
            ss['ref_distance'] = candidate_distance
            ss['conf'] = candidate_confidence
        streams_to_persist.append(ss)
    for ss in streams_to_persist:
        _persist_stream_roi(ss)
    _notify_modbus_window_changed()
    _refresh_modbus_snapshot()
    return {}


def _ensure_lwm2m_runtime_started(initial_obj_13432=None):
    """Start LwM2M only when real data should be exposed to the server."""
    global _lwm2m_runtime
    if _COMM_MODE != 'lwm2m':
        return False
    with _lwm2m_start_lock:
        if _lwm2m_runtime is not None:
            return True
        if initial_obj_13432 is not None:
            try:
                _lwm2m_comm.write_lwm2m_13432_data_json(initial_obj_13432)
            except Exception as exc:
                print(f'[LwM2M] initial data.json seed failed: {exc!r}', flush=True)
        _lwm2m_runtime = LwM2MRuntime(
            server=_LWM2M_SERVER,
            server_port=_LWM2M_SERVER_PORT,
            address=_LWM2M_ADDRESS,
            client_port=_LWM2M_CLIENT_PORT,
            on_13432_request=_on_lwm2m_13432_request,
        )
        _lwm2m_runtime.start()
        print(
            f'[LwM2M] started server={_LWM2M_SERVER}:{_LWM2M_SERVER_PORT} '
            f'bind={_LWM2M_ADDRESS}:{_LWM2M_CLIENT_PORT}',
            flush=True,
        )
        return True


def _stop_lwm2m_runtime(reason=''):
    global _lwm2m_runtime
    with _lwm2m_start_lock:
        runtime = _lwm2m_runtime
        _lwm2m_runtime = None
    if runtime is not None:
        try:
            runtime.stop()
        finally:
            extra = f' ({reason})' if reason else ''
            print(f'[LwM2M] stopped{extra}', flush=True)


def _publish_latest_lwm2m(sync=False, reason='auto'):
    global _db_last_publish
    snapshot = _build_db_payload_snapshot()
    if not snapshot['format_valid']:
        err = '; '.join(snapshot['format_errors'])
        with _db_lock:
            _db_last_publish = {
                'ok': False,
                'reason': reason,
                'sent_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'error': err,
                'format_valid': False,
                'format_errors': list(snapshot['format_errors']),
            }
        raise RuntimeError(f'DB payload format invalid: {err}')
    obj_13432 = snapshot['payload']['13432']
    if _COMM_MODE != 'lwm2m' or not _ensure_lwm2m_runtime_started(obj_13432):
        with _db_lock:
            _db_last_publish = {
                'ok': False,
                'reason': reason,
                'sent_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'error': 'LwM2M runtime disabled',
                'format_valid': True,
                'format_errors': [],
            }
        raise RuntimeError('LwM2M runtime disabled')
    if _lwm2m_runtime is None:
        raise RuntimeError('LwM2M runtime unavailable')
    if sync:
        _lwm2m_runtime.publish_13432_sync(obj_13432)
    else:
        _lwm2m_runtime.publish_13432(obj_13432)
    with _db_lock:
        _db_last_publish = {
            'ok': True,
            'reason': reason,
            'sent_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'error': '',
            'format_valid': True,
            'format_errors': [],
        }
    return snapshot


def _send_db_now():
    if _COMM_MODE == 'modbus_rtu':
        snapshot = _json_safe_modbus_snapshot(_build_modbus_payload_snapshot())
        return {'ok': True, **snapshot, 'db_status': _get_db_status()}
    if not _st.get('running'):
        snapshot = _build_db_payload_snapshot()
        return {
            'ok': False,
            'error': '추론 시작 후 전송할 수 있습니다',
            **snapshot,
            'db_status': _get_db_status(),
        }
    try:
        snapshot = _publish_latest_lwm2m(sync=True, reason='manual')
        return {'ok': True, **snapshot, 'db_status': _get_db_status()}
    except Exception as exc:
        snapshot = _build_db_payload_snapshot()
        return {
            'ok': False,
            'error': str(exc),
            **snapshot,
            'db_status': _get_db_status(),
        }


def _get_db_status():
    now = time.time()
    with _db_lock:
        last_publish = dict(_db_last_publish)
        next_auto_ts = float(_db_next_auto_publish_at or 0.0)
    health = {}
    if _COMM_MODE == 'modbus_rtu' and _MODBUS_EXTERNAL:
        try:
            with urllib.request.urlopen(_MODBUSD_URL + '/status', timeout=0.25) as response:
                health = json.loads(response.read().decode('utf-8'))
        except Exception:
            health = {'ok': False, 'error': 'external modbusd unavailable'}
    elif _COMM_MODE == 'modbus_rtu' and _modbus_runtime is not None:
        try:
            health = _modbus_runtime.get_health_snapshot()
        except Exception:
            health = {}
    elif _lwm2m_runtime is not None:
        try:
            health = _lwm2m_runtime.get_health_snapshot()
        except Exception:
            health = {}
    return {
        'mode': _COMM_MODE,
        'auto_interval_sec': None if _COMM_MODE == 'modbus_rtu' else _LWM2M_INTERVAL_SEC,
        'next_auto_in_sec': None if _COMM_MODE == 'modbus_rtu' else (max(0.0, next_auto_ts - now) if _st.get('running') and next_auto_ts else None),
        'next_auto_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_auto_ts)) if next_auto_ts else '',
        'last_publish': last_publish,
        'runtime_deferred': _COMM_MODE == 'lwm2m' and _lwm2m_runtime is None,
        'rolling_window_seconds': _ROLLING_WINDOW_SECONDS if _COMM_MODE == 'modbus_rtu' else None,
        'health': health,
    }


def _clear_external_modbus_slots(slot_indexes=None):
    """Clear authoritative modbusd counters after a WebUI count reset."""
    if not _MODBUS_EXTERNAL:
        return True
    payload = json.dumps({'slot_indexes': slot_indexes}).encode('utf-8')
    request_obj = urllib.request.Request(
        _MODBUSD_URL + '/clear',
        data=payload,
        method='POST',
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=2.0) as response:
            result = json.loads(response.read().decode('utf-8'))
        return bool(result.get('ok'))
    except Exception as exc:
        print(f'[ModbusRTU] external cumulative clear failed: {exc!r}', flush=True)
        return False


def _set_external_modbus_config(start, values):
    """Synchronize a WebUI configuration change into authoritative modbusd."""
    if not _MODBUS_EXTERNAL:
        return True
    payload = json.dumps({
        'start': int(start),
        'values': [int(value) for value in values],
    }).encode('utf-8')
    request_obj = urllib.request.Request(
        _MODBUSD_URL + '/config',
        data=payload,
        method='POST',
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=2.0) as response:
            result = json.loads(response.read().decode('utf-8'))
        return bool(result.get('ok'))
    except Exception as exc:
        print(f'[ModbusRTU] external configuration sync failed: {exc!r}', flush=True)
        return False


def _get_external_modbus_counts():
    """Read the same persistent cumulative slots exposed by FC03/menu 10.

    The last valid response is retained during a short modbusd outage so the
    WebUI never jumps back to inferd's process-local counters.
    """
    global _modbusd_counts_cache, _modbusd_counts_fetched_at
    if not _MODBUS_EXTERNAL:
        return None
    now = time.monotonic()
    with _modbusd_counts_lock:
        if _modbusd_counts_cache is not None and now - _modbusd_counts_fetched_at < 0.4:
            return {**_modbusd_counts_cache, 'stale': False}
        try:
            with urllib.request.urlopen(_MODBUSD_URL + '/counts', timeout=0.25) as response:
                payload = json.loads(response.read().decode('utf-8'))
            slots = payload.get('cumulative_slots') or []
            indexed = {
                (int(item['object_no']), int(item['direction'])): item
                for item in slots
                if isinstance(item, dict)
            }
            counts = {}
            speed_n = 0
            speed_sum = 0.0
            for object_no, name in ((0, 'person'), (1, 'vehicle'), (2, 'motorbike')):
                up = indexed.get((object_no, 1), {})
                down = indexed.get((object_no, 2), {})
                up_count = int(up.get('count', 0) or 0)
                down_count = int(down.get('count', 0) or 0)
                counts[name] = {
                    '상행': up_count,
                    '하행': down_count,
                    '합계': up_count + down_count,
                    '상행_avg_kph': round(float(up.get('average_speed_kph', 0.0)), 1)
                    if object_no and up_count else None,
                    '하행_avg_kph': round(float(down.get('average_speed_kph', 0.0)), 1)
                    if object_no and down_count else None,
                }
                if object_no:
                    speed_n += up_count + down_count
                    speed_sum += (
                        float(up.get('average_speed_kph', 0.0)) * up_count
                        + float(down.get('average_speed_kph', 0.0)) * down_count
                    )
            _modbusd_counts_cache = {
                'counts': counts,
                'speed_stat': {'n': speed_n, 'sum': speed_sum, 'min': None, 'max': None},
                'snapshot_at': payload.get('snapshot_at'),
            }
            _modbusd_counts_fetched_at = now
            return {**_modbusd_counts_cache, 'stale': False}
        except Exception as exc:
            if _modbusd_counts_cache is not None:
                return {**_modbusd_counts_cache, 'stale': True}
            print(f'[ModbusRTU] authoritative count read failed: {exc!r}', flush=True)
            # Do not fall back to inferd's unrelated volatile counter.  Until
            # modbusd becomes reachable, expose an explicitly stale zero view.
            return {
                'counts': {
                    name: {
                        '상행': 0, '하행': 0, '합계': 0,
                        '상행_avg_kph': None, '하행_avg_kph': None,
                    }
                    for name in ('person', 'vehicle', 'motorbike')
                },
                'speed_stat': {'n': 0, 'sum': 0.0, 'min': None, 'max': None},
                'snapshot_at': None,
                'stale': True,
                'available': False,
            }


def _on_lwm2m_13432_request(path):
    try:
        _publish_latest_lwm2m(sync=True, reason='read-refresh')
    except Exception as exc:
        print(f'[LwM2M] sync refresh before read failed: {exc!r}', flush=True)
    # 외부 GET/Observe 요청은 최신 값을 반영만 한다.
    # 카운트 초기화는 10분 자동 전송 성공 후에만 수행한다.


def _classes_for_lwm2m_path(request_path):
    parts = [p for p in str(request_path or '').strip().split('/') if p]
    if not parts or parts[0] != '13432':
        return None
    if len(parts) == 1:
        classes = []
        for values in _LWM2M_INSTANCE_TO_CLASSES.values():
            classes.extend(values)
        return set(_norm_class_name(c) for c in classes)
    try:
        inst_id = int(parts[1])
    except ValueError:
        return set()
    return set(_norm_class_name(c) for c in _LWM2M_INSTANCE_TO_CLASSES.get(inst_id, ()))


def _reset_counts_for_classes(class_names=None):
    target = None if class_names is None else set(_norm_class_name(c) for c in class_names)
    for ss in _streams:
        with ss['_lock']:
            if not target:
                _reset_stream_interval_counters(ss, reset_tracking=True)
            else:
                drop_ids = [
                    cid for cid in list(ss.get('counts', {}).keys())
                    if _norm_class_name(CLASS_NAMES.get(cid, str(cid))) in target
                ]
                for cid in drop_ids:
                    ss['counts'].pop(cid, None)
                    ss.get('speed_by_cls_dir', {}).pop(cid, None)
                _reset_stream_tracking_state(ss, reset_frame_idx=False)


def _classes_for_modbus_slots(slot_indexes):
    mapping = {
        0: ('person',),
        1: ('person',),
        2: ('car', 'bus', 'truck'),
        3: ('car', 'bus', 'truck'),
        4: ('motorbike', 'motorcycle'),
        5: ('motorbike', 'motorcycle'),
    }
    if slot_indexes is None:
        return None
    classes = []
    for idx in slot_indexes:
        classes.extend(mapping.get(int(idx), ()))
    return set(_norm_class_name(c) for c in classes)


def _consume_lwm2m_reset_requests():
    while True:
        try:
            request_path = _lwm2m_request_queue.get_nowait()
        except Exception:
            return
        classes = _classes_for_lwm2m_path(request_path)
        if classes is None:
            continue
        _reset_counts_for_classes(classes)


def _lwm2m_writer_loop():
    global _comm_heartbeat, _db_next_auto_publish_at
    print('[LwM2M] writer started', flush=True)
    while not _service_stop.is_set():
        _comm_heartbeat = time.time()
        now = time.time()
        try:
            if _st.get('running'):
                if _db_next_auto_publish_at <= 0:
                    _db_next_auto_publish_at = now + max(1.0, _LWM2M_INTERVAL_SEC)
                if now >= _db_next_auto_publish_at:
                    _publish_latest_lwm2m(sync=False, reason='auto')
                    _reset_counts_for_classes(None)
                    print('[LwM2M] auto publish complete → 10분 집계 카운트 초기화', flush=True)
                    _db_next_auto_publish_at = now + max(1.0, _LWM2M_INTERVAL_SEC)
            else:
                _db_next_auto_publish_at = 0.0
                _stop_lwm2m_runtime('inference stopped')
            _consume_lwm2m_reset_requests()
        except Exception as exc:
            print(f'[LwM2M] publish failed: {exc!r}', flush=True)
            _db_next_auto_publish_at = time.time() + max(10.0, min(_LWM2M_INTERVAL_SEC, 60.0))
        wait_sec = 1.0
        if _st.get('running') and _db_next_auto_publish_at:
            wait_sec = min(1.0, max(0.1, _db_next_auto_publish_at - time.time()))
        _service_stop.wait(wait_sec)


def _check_lwm2m_health(now):
    if _COMM_MODE != 'lwm2m' or _lwm2m_runtime is None:
        return
    snapshot = _lwm2m_runtime.get_health_snapshot()
    if not snapshot.get('thread_alive', False):
        print('[FATAL] LwM2M runtime thread stopped unexpectedly', flush=True)
        os._exit(1)
    last_success_age = snapshot.get('last_success_age')
    started_at = snapshot.get('started_at', now)
    boot_age = now - started_at
    failures = snapshot.get('consecutive_failures', 0)
    if last_success_age is None:
        if boot_age > _LWM2M_HEALTH_TIMEOUT_SEC and failures > 0:
            print(f'[FATAL] LwM2M never registered successfully for {boot_age:.1f}s: {snapshot}', flush=True)
            os._exit(1)


def _ensure_modbus_runtime_started():
    global _modbus_runtime
    if _COMM_MODE != 'modbus_rtu' or _MODBUS_EXTERNAL:
        return False
    if _modbus_runtime is not None:
        return True
    _ensure_modbus_snapshot_started()
    _modbus_runtime = _modbus_comm.ModbusRTUSlaveRuntime(
        port=_MODBUS_PORT,
        baudrate=_MODBUS_BAUDRATE,
        slave_id=_MODBUS_SLAVE_ID,
        timeout_sec=_MODBUS_TIMEOUT_SEC,
        snapshot_provider=_build_modbus_payload_snapshot,
        write_handler=_handle_modbus_config_write,
    )
    _modbus_runtime.start()
    return True


def _notify_modbus_window_changed():
    """Wake analysis immediately after an ROI-exit event or a manual reset."""
    global _modbus_window_version
    with _modbus_window_condition:
        _modbus_window_version += 1
        _modbus_window_condition.notify_all()


def _next_modbus_window_expiry():
    # Cumulative Modbus data no longer expires by time; FC16 clear defines reset.
    return None


def _modbus_snapshot_loop():
    print('[ModbusRTU] event-driven analysis started cumulative-reset=FC16', flush=True)
    while not _service_stop.is_set():
        with _modbus_window_condition:
            observed_version = _modbus_window_version

        _refresh_modbus_snapshot()
        next_expiry = _next_modbus_window_expiry()
        timeout = None if next_expiry is None else max(0.0, next_expiry - time.monotonic())

        with _modbus_window_condition:
            if observed_version != _modbus_window_version:
                continue
            _modbus_window_condition.wait(timeout)


def _ensure_modbus_snapshot_started():
    global _modbus_snapshot_thread
    _refresh_modbus_snapshot()
    if _modbus_snapshot_thread is not None and _modbus_snapshot_thread.is_alive():
        return
    _modbus_snapshot_thread = threading.Thread(
        target=_modbus_snapshot_loop,
        daemon=True,
        name='ModbusSnapshot',
    )
    _modbus_snapshot_thread.start()


def _stop_modbus_snapshot():
    global _modbus_snapshot_thread
    thread = _modbus_snapshot_thread
    _modbus_snapshot_thread = None
    _notify_modbus_window_changed()
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)


def _stop_modbus_runtime(reason=''):
    global _modbus_runtime
    runtime = _modbus_runtime
    _modbus_runtime = None
    if runtime is not None:
        runtime.stop()
        extra = f' ({reason})' if reason else ''
        print(f'[ModbusRTU] stopped{extra}', flush=True)


def _watchdog_loop():
    print('[Watchdog] monitoring started', flush=True)
    while not _service_stop.is_set():
        now = time.time()
        if _COMM_MODE == 'lwm2m' and (now - _comm_heartbeat) > _WATCHDOG_TIMEOUT_SEC:
            print(f'[FATAL] COMM thread frozen for {now - _comm_heartbeat:.1f}s', flush=True)
            os._exit(1)
        if _COMM_MODE == 'modbus_rtu' and _modbus_runtime is not None:
            health = _modbus_runtime.get_health_snapshot()
            if not health.get('thread_alive', False):
                print(f'[FATAL] Modbus RTU runtime stopped unexpectedly: {health}', flush=True)
                os._exit(1)
        perf_now = time.perf_counter()
        if _st.get('running'):
            for ss in list(_streams):
                if int(ss.get('frame_idx', 0) or 0) <= 0:
                    continue
                last_progress = float(ss.get('_last_progress_ts', perf_now) or perf_now)
                if perf_now - last_progress > _WATCHDOG_TIMEOUT_SEC:
                    name = ss.get('video_name', 'stream')
                    print(f'[FATAL] DeepStream stream frozen: {name} ({perf_now - last_progress:.1f}s)', flush=True)
                    os._exit(1)
        _check_lwm2m_health(now)
        _service_stop.wait(2.0)


def _start_background_services():
    global _comm_thread, _watchdog_thread
    _gpio_pulse_output.start()
    if _COMM_MODE == 'modbus_rtu':
        if _MODBUS_EXTERNAL:
            _modbus_event_publisher.start()
            print('[ModbusRTU] external modbusd bridge enabled', flush=True)
        else:
            _ensure_modbus_runtime_started()
            print(
                f'[ModbusRTU] embedded port={_MODBUS_PORT} baudrate={_MODBUS_BAUDRATE} '
                f'slave=0x{_MODBUS_SLAVE_ID:02X}',
                flush=True,
            )
    elif _COMM_MODE == 'lwm2m':
        _comm_thread = threading.Thread(target=_lwm2m_writer_loop, daemon=True, name='LwM2MWriter')
        _comm_thread.start()
        print(
            f'[LwM2M] deferred until first {_LWM2M_INTERVAL_SEC:.0f}s aggregation '
            f'server={_LWM2M_SERVER}:{_LWM2M_SERVER_PORT}',
            flush=True,
        )
    else:
        print('[COMM] disabled by DS_CAMERA_COMM', flush=True)
    _watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True, name='DsCameraWatchdog')
    _watchdog_thread.start()


def _stop_background_services():
    _service_stop.set()
    _gpio_pulse_output.stop()
    if _MODBUS_EXTERNAL:
        _modbus_event_publisher.stop()
    _stop_lwm2m_runtime('service stop')
    _stop_modbus_runtime('service stop')
    _stop_modbus_snapshot()
    for thread in (_comm_thread, _watchdog_thread):
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
