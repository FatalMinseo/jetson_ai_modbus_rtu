# Auto-split from runtime.py: lines 245-496
import fcntl
from modbus_daemon import RealtimeWindowConfig


def _display_class_name(class_name):
    name = str(class_name or '').strip()
    if name.lower() in ('car', 'truck', 'bus'):
        return 'car'
    return name


def _class_color_bgr(cls_id: int):
    color_id = int(cls_id)
    class_name = str(CLASS_NAMES.get(color_id, '')).strip().lower()
    if class_name in ('car', 'bus', 'truck'):
        color_id = next(
            (cid for cid, name in CLASS_NAMES.items()
             if str(name).strip().lower() == 'car'),
            color_id)
    return CLASS_COLORS[color_id % len(CLASS_COLORS)]


def _class_color_rgb(cls_id: int):
    b, g, r = _class_color_bgr(cls_id)
    return (r, g, b)


def _set_rect_bbox(rect, x1, y1, x2, y2, cls_id: int, border_width: int = 2):
    rect.left = float(x1)
    rect.top = float(y1)
    rect.width = float(x2) - float(x1)
    rect.height = float(y2) - float(y1)
    rect.border_width = int(border_width)
    color = _class_color_bgr(cls_id)
    rect.border_color.set(color[2]/255.0, color[1]/255.0, color[0]/255.0, 1.0)
    rect.has_bg_color = 0
    return color


def _store_detector_bbox(obj_meta, x1, y1, x2, y2):
    bbox = obj_meta.detector_bbox_info.org_bbox_coords
    bbox.left = float(x1)
    bbox.top = float(y1)
    bbox.width = float(x2) - float(x1)
    bbox.height = float(y2) - float(y1)


_RED_COLOR   = (1.0, 0.2, 0.2, 1.0)   # 빨간색 (speed 표시 시)
_WHITE_COLOR = (1.0, 1.0, 1.0, 1.0)   # 기본 흰색


def _pt_in_poly(px, py, poly):
    """Ray casting 점-폴리곤 포함 검사."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(poly[i][0]), float(poly[i][1])
        xj, yj = float(poly[j][0]), float(poly[j][1])
        if ((yi > py) != (yj > py)) and \
                (px < (xj - xi) * (py - yi) / (yj - yi + 1e-10) + xi):
            inside = not inside
        j = i
    return inside

def _set_osd_label(text_params, text: str, x, y, cls_id: int,
                   font_size=None, font_color=None):
    if font_size is None:
        try:
            font_size = max(8, min(32, int(os.environ.get('DS_OSD_FONT_SIZE', '10'))))
        except (TypeError, ValueError):
            font_size = 10
    color = _class_color_bgr(cls_id)
    class_name = str(CLASS_NAMES.get(int(cls_id), '')).strip()
    display_name = _display_class_name(class_name)
    if display_name != class_name:
        if text == class_name:
            text = display_name
        elif text.startswith(class_name + ' '):
            text = display_name + text[len(class_name):]
    text_params.display_text = text
    text_params.x_offset = int(x)
    text_params.y_offset = max(0, int(y) - int(font_size) - 3)
    text_params.font_params.font_name = 'DejaVu Sans'
    text_params.font_params.font_size = int(font_size)
    fc = font_color if font_color is not None else _WHITE_COLOR
    text_params.font_params.font_color.set(*fc)
    text_params.set_bg_clr = 1
    text_params.text_bg_clr.set(color[2]/255.0, color[1]/255.0, color[0]/255.0, 1.0)


# ── 설정 프리셋 (setting_preset.json) ─────────────────────────
from collections import deque
from datetime import datetime

_CURRENT_ROI_STATE_LOCK = threading.Lock()
_OPERATION_SETTINGS_LOCK = threading.Lock()


def _load_operation_settings():
    if not os.path.exists(OPERATION_SETTINGS_FILE):
        return {}
    try:
        with open(OPERATION_SETTINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f'[WARN] operation settings ignored: {exc!r}', flush=True)
        return {}


_OPERATION_SETTINGS = _load_operation_settings()
_MODBUS_SETTINGS = _OPERATION_SETTINGS.get('modbus_rtu', {})
_ANALYTICS_SETTINGS = _OPERATION_SETTINGS.get('analytics', {})
_ROI_DEFAULT_SETTINGS = _OPERATION_SETTINGS.get('roi_defaults', {})
_LOGGING_SETTINGS = _OPERATION_SETTINGS.get('logging', {})
_STARTUP_SETTINGS = _OPERATION_SETTINGS.get('startup', {})
_ROI_DEFAULT_CONFIDENCE = float(_ROI_DEFAULT_SETTINGS.get('confidence_threshold', 0.30))
_ROI_DEFAULT_REF_DISTANCE_M = max(0.1, float(_ROI_DEFAULT_SETTINGS.get('ref_distance_m', 10.0)))
_REALTIME_WINDOW_DISK_LOGGING_ENABLED = str(
    os.environ.get(
        'DS_CAMERA_REALTIME_WINDOW_DISK_LOGGING',
        _LOGGING_SETTINGS.get('realtime_window_disk_enabled', False),
    )
).strip().lower() in ('1', 'true', 'yes', 'on')


def _save_operation_settings(data):
    data['version'] = 1
    tmp = OPERATION_SETTINGS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, OPERATION_SETTINGS_FILE)


def _mutate_operation_settings(mutator):
    """Apply one partial update without overwriting another process's fields."""
    lock_path = OPERATION_SETTINGS_FILE + '.lock'
    os.makedirs(os.path.dirname(OPERATION_SETTINGS_FILE) or '.', exist_ok=True)
    with _OPERATION_SETTINGS_LOCK:
        with open(lock_path, 'a+', encoding='utf-8') as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            latest = _load_operation_settings()
            mutator(latest)
            _save_operation_settings(latest)
            _OPERATION_SETTINGS.clear()
            _OPERATION_SETTINGS.update(latest)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return latest


def _startup_settings_snapshot():
    with _OPERATION_SETTINGS_LOCK:
        startup = _OPERATION_SETTINGS.get('startup', {})
        return json.loads(json.dumps(startup)) if isinstance(startup, dict) else {}


def _update_startup_settings(**values):
    """Persist the operator-selected input and desired runtime state."""
    def update(settings):
        startup = settings.setdefault('startup', {})
        startup.update(values)
    settings = _mutate_operation_settings(update)
    return dict(settings.get('startup', {}))

def _load_roi_logs():
    src = SETTING_PRESET_FILE if os.path.exists(SETTING_PRESET_FILE) else LEGACY_ROI_LOG_FILE
    if not os.path.exists(src):
        return []
    try:
        with open(src, 'r', encoding='utf-8') as f:
            logs = json.load(f)
            if isinstance(logs, list):
                return logs
            return []
    except Exception:
        return []

def _save_roi_logs(logs):
    with open(SETTING_PRESET_FILE, 'w', encoding='utf-8') as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)


def _roi_points_valid(roi, line1, line2):
    return isinstance(roi, list) and isinstance(line1, list) and isinstance(line2, list) \
        and len(roi) >= 3 and len(line1) == 2 and len(line2) == 2


def _roi_source_key(ss):
    """Stable key for restoring the last applied ROI for the same source."""
    is_camera = bool(ss.get('is_camera'))
    width = int(ss.get('frame_w') or ss.get('width') or 0)
    height = int(ss.get('frame_h') or ss.get('height') or 0)
    if is_camera:
        device = ss.get('camera_device') or ss.get('video_path') or CAMERA_DEVICE
        return f'camera:{device}:{width}x{height}'
    path = ss.get('video_path') or ss.get('path') or ''
    try:
        path = os.path.abspath(path) if path else ''
    except Exception:
        path = str(path or '')
    name = ss.get('video_name') or ss.get('name') or ''
    return f'video:{path or name}:{width}x{height}'


def _load_current_roi_state():
    if not os.path.exists(CURRENT_ROI_STATE_FILE):
        return {'version': 1, 'streams': {}}
    try:
        with open(CURRENT_ROI_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {'version': 1, 'streams': {}}
        streams = data.get('streams')
        if not isinstance(streams, dict):
            data['streams'] = {}
        data.setdefault('version', 1)
        return data
    except Exception:
        return {'version': 1, 'streams': {}}


def _save_current_roi_state(data):
    data['version'] = 1
    data['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    tmp = CURRENT_ROI_STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CURRENT_ROI_STATE_FILE)


def _current_roi_entry_from_stream(ss):
    roi = ss.get('roi_pts', [])
    line1 = ss.get('line1', [])
    line2 = ss.get('line2', [])
    ready = _roi_points_valid(roi, line1, line2)
    if not ready:
        return None
    return {
        'roi_pts': roi,
        'line1': line1,
        'line2': line2,
        'roi_ready': True,
        'ref_distance': max(0.1, float(ss.get('ref_distance', _ROI_DEFAULT_REF_DISTANCE_M) or _ROI_DEFAULT_REF_DISTANCE_M)),
        'width': int(ss.get('frame_w') or 0),
        'height': int(ss.get('frame_h') or 0),
        'name': ss.get('video_name', ''),
        'is_camera': bool(ss.get('is_camera')),
        'camera_device': ss.get('camera_device', ''),
        'confidence': round(float(ss.get('conf', _ROI_DEFAULT_CONFIDENCE)), 4),
    }


def _persist_stream_roi(ss):
    with _CURRENT_ROI_STATE_LOCK:
        state = _load_current_roi_state()
        streams = state.setdefault('streams', {})
        key = _roi_source_key(ss)
        entry = _current_roi_entry_from_stream(ss)
        if entry is None:
            streams.pop(key, None)
        else:
            streams[key] = entry
        _save_current_roi_state(state)


def _clear_persisted_stream_roi(ss):
    with _CURRENT_ROI_STATE_LOCK:
        state = _load_current_roi_state()
        streams = state.setdefault('streams', {})
        streams.pop(_roi_source_key(ss), None)
        _save_current_roi_state(state)


def _apply_persisted_roi_to_stream(ss):
    entry = _load_current_roi_state().get('streams', {}).get(_roi_source_key(ss))
    if not isinstance(entry, dict):
        return False
    roi = entry.get('roi_pts') or entry.get('roi_points') or []
    line1 = entry.get('line1') or []
    line2 = entry.get('line2') or []
    ready = _roi_points_valid(roi, line1, line2)
    if not ready:
        return False
    ss.update({
        'roi_pts': roi,
        'line1': line1,
        'line2': line2,
        'roi_ready': True,
        'ref_distance': max(0.1, float(entry.get('ref_distance', _ROI_DEFAULT_REF_DISTANCE_M) or _ROI_DEFAULT_REF_DISTANCE_M)),
        'conf': float(entry.get('confidence', ss.get('conf', _ROI_DEFAULT_CONFIDENCE))),
    })
    return True


def _roi_response_fields(ss):
    return {
        'roi_ready': bool(ss.get('roi_ready', False)),
        'roi_pts': ss.get('roi_pts', []),
        'line1': ss.get('line1', []),
        'line2': ss.get('line2', []),
        'ref_distance': round(float(ss.get('ref_distance', _ROI_DEFAULT_REF_DISTANCE_M) or _ROI_DEFAULT_REF_DISTANCE_M), 2),
        'conf': round(float(ss.get('conf', _st.get('conf', _ROI_DEFAULT_CONFIDENCE))), 2) if '_st' in globals() else _ROI_DEFAULT_CONFIDENCE,
    }


def _roi_key(roi_points, line1, line2, width=0, height=0, conf_threshold=0.30):
    def norm(pts): return tuple(tuple(p) for p in pts)
    return (
        int(width), int(height),
        None if conf_threshold is None else round(float(conf_threshold), 4),
        norm(roi_points), norm(line1), norm(line2)
    )


# ── 추론 결과 JSON 저장 ────────────────────────────────────────
RESULT_LOG_DIR = os.path.join(BASE_DIR, 'result_logs')
os.makedirs(RESULT_LOG_DIR, exist_ok=True)

# Field verification keeps the raw ROI-exit events separate from the derived
# moving-window rows.  The files are hourly NDJSON so a long camera session
# does not create one impractically large file.
REALTIME_WINDOW_LOG_DIR = os.path.join(RESULT_LOG_DIR, 'realtime_window')
os.makedirs(REALTIME_WINDOW_LOG_DIR, exist_ok=True)
_realtime_window_config = RealtimeWindowConfig(OPERATION_SETTINGS_FILE)
_REALTIME_WINDOW_SECONDS = _realtime_window_config.seconds()


def _refresh_realtime_window():
    global _REALTIME_WINDOW_SECONDS
    _REALTIME_WINDOW_SECONDS = _realtime_window_config.seconds()
    return _REALTIME_WINDOW_SECONDS


_REALTIME_MEMORY_ROWS = max(
    100,
    int(os.environ.get('DS_CAMERA_REALTIME_MEMORY_ROWS', 5000)),
)
_realtime_log_lock = threading.Lock()
_realtime_log_handles = {}
_realtime_event_sequence = 0
_realtime_analysis_sequence = 0


def _realtime_wall_time():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def _next_realtime_id(kind):
    global _realtime_event_sequence, _realtime_analysis_sequence
    with _realtime_log_lock:
        if kind == 'event':
            _realtime_event_sequence += 1
            return _realtime_event_sequence
        _realtime_analysis_sequence += 1
        return _realtime_analysis_sequence


def _append_realtime_ndjson(kind, row):
    """Append one verification row, rotating the file at each wall-clock hour."""
    if not _REALTIME_WINDOW_DISK_LOGGING_ENABLED:
        return
    hour_key = datetime.now().strftime('%Y-%m-%d_%H')
    with _realtime_log_lock:
        current = _realtime_log_handles.get(kind)
        if current is None or current['hour_key'] != hour_key:
            if current is not None:
                current['file'].close()
            path = os.path.join(REALTIME_WINDOW_LOG_DIR, f'{kind}_{hour_key}.ndjson')
            current = {
                'hour_key': hour_key,
                'path': path,
                'file': open(path, 'a', encoding='utf-8', buffering=1),
            }
            _realtime_log_handles[kind] = current
        current['file'].write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')


def _variance_from_speeds(speeds, mean):
    if len(speeds) < 2:
        return None
    return sum((speed - mean) ** 2 for speed in speeds) / len(speeds)

# Modbus cumulative statistics are maintained as bounded accumulators.  The
# Event deque below uses the shared configurable real-time window.
_ROLLING_WINDOW_SECONDS = max(
    1,
    int(os.environ.get(
        'DS_CAMERA_WINDOW_SECONDS',
        os.environ.get('DS_CAMERA_ROLLING_WINDOW_SECONDS', _ANALYTICS_SETTINGS.get('window_seconds', 600)),
    )),
)

def _prune_rolling_window(ss, now=None):
    buckets = ss.setdefault('rolling_window', deque())
    now = time.monotonic() if now is None else float(now)
    cutoff = now - _refresh_realtime_window()
    while buckets and float(buckets[0].get('event_time', 0.0)) <= cutoff:
        buckets.popleft()
    return buckets


def _record_rolling_event(ss, class_name, direction, speed_kph, event_time=None):
    """Record one confirmed ROI-exit event at the moment its speed is finalized.

    The caller already owns ``ss['_lock']`` in the counting probe.
    """
    event_time = time.monotonic() if event_time is None else float(event_time)
    events = _prune_rolling_window(ss, event_time)
    event = {
        'event_time': event_time,
        'recorded_at': time.time(),
        'class_name': str(class_name),
        'direction': str(direction),
        'speed_kph': float(speed_kph),
    }
    events.append(event)
    accumulate = globals().get('_accumulate_modbus_cumulative_event')
    if callable(accumulate):
        accumulate(ss, event)
    notify = globals().get('_notify_modbus_window_changed')
    if callable(notify):
        notify()


def _rolling_window_events(ss, now=None):
    """Return only ROI-exit events inside the configured real-time window."""
    return [dict(event) for event in _prune_rolling_window(ss, now)]


def _reset_stream_rolling_window(ss):
    ss['rolling_window'] = deque()
    empty_stats = globals().get('_empty_modbus_slot_stats')
    ss['modbus_cumulative_stats'] = empty_stats() if callable(empty_stats) else {}
    notify = globals().get('_notify_modbus_window_changed')
    if callable(notify):
        notify()


def _vehicle_trend_direction_code(ss, direction):
    """Map the current ROI direction label to an extensible direction code."""
    mapping = ss.get('direction_code_map', {})
    try:
        code = mapping.get(direction)
    except AttributeError:
        code = None
    if code is None:
        code = 1 if direction == '상행' else 2
    return int(code)


def _is_vehicle_for_trend(class_name):
    name = str(class_name or '').strip().lower()
    return name in ('car', 'bus', 'truck', 'vehicle', '승용차', '버스', '트럭')


def _record_vehicle_trend_event(ss, class_name, direction, speed_kph, event_time, track_id=None):
    """Record one vehicle ROI-exit event for real-time window analysis.

    The caller owns ``ss['_lock']``. Emit once per completed ROI traversal;
    the same track ID may emit again on a later traversal.
    """
    if not _is_vehicle_for_trend(class_name):
        return
    event = {
        'event_id': _next_realtime_id('event'),
        'logged_at': _realtime_wall_time(),
        'stream_idx': int(ss.get('stream_idx', -1)),
        'event_time': float(event_time),
        'track_id': None if track_id is None else int(track_id),
        'class_name': str(class_name),
        'direction': str(direction),
        'direction_code': _vehicle_trend_direction_code(ss, direction),
        'speed_kph': float(speed_kph),
        'confidence': None,
    }
    events = ss.setdefault('vehicle_trend_events', deque())
    events.append(event)
    ss.setdefault('realtime_pending_events', []).append(event)
    history = ss.setdefault('realtime_roi_exit_history', deque(maxlen=_REALTIME_MEMORY_ROWS))
    history.append(dict(event))
    _append_realtime_ndjson('roi_exit_events', event)


def _update_vehicle_speed_trends(ss, analysis_time):
    """Build one frame-shifted real-time window row for every direction."""
    now = float(analysis_time)
    window_seconds = _refresh_realtime_window()
    if ss.get('realtime_window_seconds') != window_seconds:
        # Already-pruned events cannot be recovered after enlarging a window.
        # Re-enter warmup instead of presenting a partial window as complete.
        ss['realtime_window_seconds'] = window_seconds
        ss['realtime_window_started_at'] = now
    cutoff = now - window_seconds
    pending_events = ss.setdefault('realtime_pending_events', [])
    frame_events = list(pending_events)
    pending_events.clear()

    frames = ss.setdefault('realtime_window_frames', deque())
    if ss.get('realtime_window_started_at') is None:
        ss['realtime_window_started_at'] = now
    frames.append({'frame_time': now, 'event_ids': [event['event_id'] for event in frame_events]})
    while frames and float(frames[0]['frame_time']) <= cutoff:
        frames.popleft()

    events = ss.setdefault('vehicle_trend_events', deque())
    while events and float(events[0]['event_time']) <= cutoff:
        events.popleft()

    events_by_direction = {}
    for event in events:
        if float(event['event_time']) > now:
            continue
        direction_code = int(event['direction_code'])
        events_by_direction.setdefault(direction_code, []).append(event)

    previous = ss.setdefault('vehicle_trend_previous_stats', {})
    active_codes = {1, 2} | set(previous) | set(events_by_direction)
    trends = {}
    for direction_code in sorted(active_codes):
        direction_events = events_by_direction.get(direction_code, [])
        speeds = [float(event['speed_kph']) for event in direction_events]
        event_count = len(speeds)
        mean = sum(speeds) / event_count if event_count else None
        variance = _variance_from_speeds(speeds, mean) if mean is not None else None
        previous_stats = previous.get(direction_code, {})
        previous_mean = previous_stats.get('average_speed_kph')
        previous_variance = previous_stats.get('speed_variance')
        average_delta = None if mean is None or previous_mean is None else mean - previous_mean
        variance_delta = None if variance is None or previous_variance is None else variance - previous_variance
        window_ready = now - float(ss.get('realtime_window_started_at', now)) >= window_seconds
        if not window_ready:
            status = '워밍업'
        elif event_count == 0:
            status = '이벤트없음'
        elif variance is None:
            status = '표본부족'
        else:
            status = '정상'

        row = {
            'analysis_id': _next_realtime_id('analysis'),
            'logged_at': _realtime_wall_time(),
            'stream_idx': int(ss.get('stream_idx', -1)),
            'window_end_pts_sec': now,
            'window_start_pts_sec': cutoff,
            'window_duration_ms': int(round(window_seconds * 1000)),
            'direction_code': direction_code,
            'frame_count': len(frames),
            'roi_exit_vehicle_count': event_count,
            'speed_sum_kph': sum(speeds),
            'speed_square_sum': sum(speed * speed for speed in speeds),
            'average_speed_kph': mean,
            'speed_variance': variance,
            'average_delta_kph': average_delta,
            'variance_delta': variance_delta,
            'status': status,
            'event_ids': [event['event_id'] for event in direction_events],
        }
        trends[direction_code] = dict(row)
        previous[direction_code] = {
            'average_speed_kph': mean,
            'speed_variance': variance,
        }
        history = ss.setdefault('realtime_window_history', deque(maxlen=_REALTIME_MEMORY_ROWS))
        history.append(dict(row))
        _append_realtime_ndjson('moving_window_analysis', row)

    ss['vehicle_speed_trends'] = trends
    ss['vehicle_trend_last_time'] = now
    return trends


def _reset_stream_vehicle_speed_trends(ss):
    ss['vehicle_trend_events'] = deque()
    ss['realtime_pending_events'] = []
    ss['realtime_window_frames'] = deque()
    ss['realtime_window_started_at'] = None
    ss['realtime_roi_exit_history'] = deque(maxlen=_REALTIME_MEMORY_ROWS)
    ss['realtime_window_history'] = deque(maxlen=_REALTIME_MEMORY_ROWS)
    ss['vehicle_trend_previous_stats'] = {}
    ss['vehicle_speed_trends'] = {}
    ss['vehicle_trend_last_time'] = None

def _build_counts_out(ss):
    # In Modbus mode the UI and FC03 registers must describe the same
    # cumulative interval.  ``rolling_window`` is cleared by the FC16 handler.
    if globals().get('_COMM_MODE') == 'modbus_rtu':
        stats = ss.get('modbus_cumulative_stats', {})
        result = {}
        for object_no, name in ((0, 'person'), (1, 'vehicle'), (2, 'motorbike')):
            up, down = stats.get((object_no, 1), {}), stats.get((object_no, 2), {})
            up_count, down_count = int(up.get('count', 0)), int(down.get('count', 0))
            result[name] = {
                '상행': up_count, '하행': down_count, '합계': up_count + down_count,
                '상행_avg_kph': round(up['speed_sum'] / up_count, 1) if up_count and object_no else None,
                '하행_avg_kph': round(down['speed_sum'] / down_count, 1) if down_count and object_no else None,
            }
        return result

    counts_out = {}
    speed_by = ss.get('speed_by_cls_dir', {})
    for cid, v in ss.get('counts', {}).items():
        name = CLASS_NAMES.get(cid, str(cid))
        up_acc = speed_by.get(cid, {}).get('상행', _acc_empty())
        dn_acc = speed_by.get(cid, {}).get('하행', _acc_empty())
        counts_out[name] = {
            '상행': int(v.get('상행', 0)),
            '하행': int(v.get('하행', 0)),
            '합계': int(v.get('상행', 0)) + int(v.get('하행', 0)),
            '상행_avg_kph': round(up_acc['sum'] / up_acc['n'], 1) if up_acc.get('n', 0) > 0 else None,
            '하행_avg_kph': round(dn_acc['sum'] / dn_acc['n'], 1) if dn_acc.get('n', 0) > 0 else None,
        }
    return counts_out


def _build_modbus_ui_speed_stat(ss):
    """Build the UI speed aggregate from the same events exposed by Modbus."""
    acc = _acc_empty()
    for (object_no, _), item in ss.get('modbus_cumulative_stats', {}).items():
        if object_no == 0:
            continue
        count = int(item.get('count', 0))
        if count:
            acc['n'] += count
            acc['sum'] += float(item.get('speed_sum', 0.0))
    return acc


def _reset_stream_tracking_state(ss, reset_frame_idx=True):
    """Reset per-track/transient state without clearing interval counters."""
    ss['track_state'] = {}
    ss['pos_history'] = {}
    ss['live_speed'] = {}
    if reset_frame_idx:
        ss['frame_idx'] = 0
    ss['_last_frame_num'] = -1
    ss['_last_buf_pts'] = -1
    ss['_first_buf_pts'] = -1
    ss['_last_frame_time_sec'] = 0.0


def _reset_stream_interval_counters(ss, reset_tracking=True):
    """Reset counters that define one DB aggregation window."""
    ss['counts'] = {}
    ss['speed_stat'] = _acc_empty()
    ss['speed_by_cls_dir'] = {}
    _reset_stream_vehicle_speed_trends(ss)
    if reset_tracking:
        _reset_stream_tracking_state(ss, reset_frame_idx=False)


def _snapshot_loop_result(ss):
    counts_out = _build_counts_out(ss)
    spd_acc = (
        _build_modbus_ui_speed_stat(ss)
        if globals().get('_COMM_MODE') == 'modbus_rtu'
        else ss.get('speed_stat', _acc_empty())
    )
    snapshot = {
        'completed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'loop_no': int(ss.get('loop_count', 1)),
        'video_name': ss.get('video_name', ''),
        'counts': counts_out,
        'total_count': sum(v['상행'] + v['하행'] for v in counts_out.values()),
        'frame_idx': int(ss.get('frame_idx', 0)),
        'video_fps': round(float(ss.get('video_fps', 0.0)), 2),
        'ref_distance': round(float(ss.get('ref_distance', 0.0)), 2),
        'speed_stat': _acc_stat(spd_acc) if spd_acc.get('n', 0) > 0 else None,
    }
    ss['latest_loop_result'] = snapshot
    ss['latest_loop_completed_at'] = snapshot['completed_at']
    return snapshot


def _stream_played_seconds(ss):
    """Return elapsed seconds for current loop, preferring GStreamer PTS."""
    try:
        pts_sec = float(ss.get('_last_frame_time_sec', 0.0) or 0.0)
        if pts_sec > 0:
            return pts_sec
    except Exception:
        pass
    return ss.get('frame_idx', 0) / max(float(ss.get('video_fps', 30.0)), 1.0)


def _advance_stream_loop(ss, reason=''):
    now_ts = time.perf_counter()
    last_ts = float(ss.get('_last_loop_transition_ts', 0.0) or 0.0)
    if last_ts > 0 and (now_ts - last_ts) < 0.75:
        return False
    ss['_total_elapsed_sec'] = ss.get('_total_elapsed_sec', 0.0) + _stream_played_seconds(ss)
    _snapshot_loop_result(ss)
    ss['loop_count'] = int(ss.get('loop_count', 1)) + 1
    _reset_stream_tracking_state(ss, reset_frame_idx=True)
    ss['_last_loop_transition_ts'] = now_ts
    extra = f' ({reason})' if reason else ''
    print(f'[DS] {ss.get("video_name","stream")} 루프 {ss["loop_count"]} 시작{extra} — 카운트 누적 유지', flush=True)
    return True


def _finalize_stream_cycle(ss, reason=''):
    """현재 스트림의 한 사이클 완료 스냅샷 저장 후 다음 사이클 준비 상태로 전환."""
    # 이번 루프 재생 시간 누적 (GStreamer PTS 우선, 없으면 frame_idx / fps)
    played = _stream_played_seconds(ss)
    ss['_total_elapsed_sec'] = ss.get('_total_elapsed_sec', 0.0) + played
    _snapshot_loop_result(ss)
    ss['loop_count'] = int(ss.get('loop_count', 1)) + 1
    _reset_stream_tracking_state(ss, reset_frame_idx=True)
    ss['_last_loop_transition_ts'] = time.perf_counter()
    if reason:
        print(f'[DS] {ss.get("video_name","stream")} 사이클 완료 처리 ({reason}) — 카운트 누적 유지', flush=True)


def _slugify_result_name(name: str) -> str:
    s = re.sub(r'[^\w\s\-\(\)\[\]가-힣]+', '_', str(name or '').strip())
    s = re.sub(r'\s+', '_', s).strip('._')
    return s or 'result'


def _write_stream_result(stream_idx: int):
    """채널 1개의 최근 완료 루프 결과를 저장.
    파일명: 영상파일명_모델_트래커_YYYY-MM-DD_HH-MM-SS.json
    """
    if not (0 <= stream_idx < len(_streams)):
        raise ValueError('invalid stream_idx')
    ss = _streams[stream_idx]
    now = datetime.now()
    ts  = now.strftime('%Y-%m-%d_%H-%M-%S')
    stem = _slugify_result_name(ss.get('video_name', f'stream_{stream_idx+1}'))
    model_stem = _slugify_result_name(_st.get('model_name', 'model'))
    tracker_stem = _slugify_result_name(_st.get('tracker_name', 'tracker'))
    fname = f'{stem}_{model_stem}_{tracker_stem}_{ts}.json'
    # 저장할 데이터: 최근 완료 루프만 저장 (완료된 루프 없으면 오류)
    loop_result = ss.get('latest_loop_result')
    if not loop_result:
        raise ValueError(f'stream {stream_idx}: 아직 완료된 루프가 없습니다')
    out = {
        'saved_at': ts,
        'date_key': now.strftime('%Y-%m-%d'),
        'stream_idx': stream_idx,
        'video_name': ss.get('video_name', ''),
        'model_name': _st.get('model_name', ''),
        'tracker_key': _st.get('tracker_key', ''),
        'tracker_name': _st.get('tracker_name', ''),
        'loop_result': loop_result,
    }
    out_path = os.path.join(RESULT_LOG_DIR, fname)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'[RESULT] 저장: {fname}')
    return fname


# ── MIN/AVG/MAX 누산기 ─────────────────────────────────────────
# maxlen 지정 시 슬라이딩 윈도우 평균 (오래된 샘플 자동 제거)
def _acc_update(acc, val):
    buf = acc.get('buf')
    if buf is not None:
        if len(buf) == buf.maxlen:
            acc['sum'] -= buf[0]
            acc['n'] -= 1
        buf.append(val)
    acc['sum'] += val; acc['n'] += 1
    acc['min'] = val if acc['min'] is None else min(acc['min'], val)
    acc['max'] = val if acc['max'] is None else max(acc['max'], val)

def _acc_stat(acc):
    avg = round(acc['sum'] / acc['n'], 1) if acc['n'] else 0.0
    return {
        'min': round(acc['min'], 1) if acc['min'] is not None else None,
        'max': round(acc['max'], 1) if acc['max'] is not None else None,
        'avg': avg,
    }

def _acc_empty(maxlen=None):
    return {'sum': 0.0, 'n': 0, 'min': None, 'max': None,
            'buf': deque(maxlen=maxlen) if maxlen else None}


# ═══════════════════════════════════════════════════════════════
#  cv2 대체 유틸리티
# ═══════════════════════════════════════════════════════════════
