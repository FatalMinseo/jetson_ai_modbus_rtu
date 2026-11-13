# Auto-split from runtime.py: lines 3935-3967
_RESTORE_ERROR_LOG_INTERVAL_SEC = max(
    60.0,
    float(os.environ.get('DS_CAMERA_RESTORE_ERROR_LOG_INTERVAL_SEC', '3600')),
)
_restore_last_error = ''
_restore_last_error_log_at = 0.0
_restore_suppressed_logs = 0


def _log_restore_failure(message):
    global _restore_last_error, _restore_last_error_log_at, _restore_suppressed_logs
    now = time.monotonic()
    message = str(message)
    if (message == _restore_last_error
            and now - _restore_last_error_log_at < _RESTORE_ERROR_LOG_INTERVAL_SEC):
        _restore_suppressed_logs += 1
        return
    suppressed = f' suppressed={_restore_suppressed_logs}' if _restore_suppressed_logs else ''
    print(f'[RESTORE] {message}; retrying{suppressed}', flush=True)
    _restore_last_error = message
    _restore_last_error_log_at = now
    _restore_suppressed_logs = 0


def _log_restore_success(source, source_name):
    global _restore_last_error, _restore_last_error_log_at, _restore_suppressed_logs
    suppressed = f' suppressed_retry_logs={_restore_suppressed_logs}' if _restore_suppressed_logs else ''
    print(f'[RESTORE] inference resumed source={source} input={source_name}{suppressed}', flush=True)
    _restore_last_error = ''
    _restore_last_error_log_at = 0.0
    _restore_suppressed_logs = 0


def _resolve_saved_video_paths(configured_files):
    """Resolve saved video names after clone/pull or project-directory moves.

    Runtime settings may contain a basename, a path relative to the project, or
    an absolute path saved on another Jetson.  Only files below VIDEO_DIR are
    accepted by /add_local, so normalize every form back into that directory.
    """
    video_root = os.path.abspath(VIDEO_DIR)
    resolved = []
    missing = []
    for raw in configured_files or []:
        value = str(raw or '').strip()
        if not value:
            continue
        candidates = []
        if os.path.isabs(value):
            candidates.append(os.path.abspath(value))
        else:
            candidates.extend((
                os.path.abspath(os.path.join(video_root, value)),
                os.path.abspath(os.path.join(BASE_DIR, value)),
            ))
        # An absolute path saved by another installation is still recoverable
        # when the same tracked video exists under this installation's VIDEO_DIR.
        candidates.append(os.path.join(video_root, os.path.basename(value)))

        selected = None
        for candidate in candidates:
            try:
                inside_video_root = os.path.commonpath(
                    [video_root, candidate]) == video_root
            except ValueError:
                inside_video_root = False
            if inside_video_root and os.path.isfile(candidate):
                selected = candidate
                break
        if selected and selected not in resolved:
            resolved.append(selected)
        elif not selected:
            missing.append(value)

    # A freshly cloned appliance is allowed to start from the bundled test
    # video even if an empty/stale runtime setting was committed previously.
    if not resolved:
        bundled = os.path.join(video_root, 'street_ds.mp4')
        if os.path.isfile(bundled):
            resolved.append(bundled)
            print('[RESTORE] 저장 영상 없음 → 기본 street_ds.mp4 사용', flush=True)
    return resolved, missing


def _cleanup_video_temps():
    _stop_live_restreams()
    video_dir = os.path.abspath(globals().get('VIDEO_DIR', ''))
    for ss in _streams:
        lp = ss.get('_restream_log_path', '')
        if lp and os.path.exists(lp):
            try:
                os.unlink(lp)
            except Exception:
                pass
            ss['_restream_log_path'] = ''
        vp = ss.get('video_path', '')
        vp_abs = os.path.abspath(vp) if vp else ''
        vp_name = os.path.basename(vp_abs)
        if vp and os.path.exists(vp) and (
            vp.startswith('/tmp/') or
            (video_dir and vp_abs.startswith(video_dir + os.sep) and
             (vp_name.startswith('upload_') or vp_name.startswith('norm_')))
        ):
            try: os.unlink(vp)
            except: pass

# 서버 시작 시 이전 세션 잔여 파일 정리
import glob as _glob
_video_dir = globals().get('VIDEO_DIR', '')
for _p in (_glob.glob('/tmp/tmp*.mp4') + _glob.glob('/tmp/norm_*.mp4') +
           _glob.glob('/tmp/tmp*.avi') +
           _glob.glob('/tmp/tmp*.mkv') + _glob.glob('/tmp/tmp*.mov') +
           _glob.glob('/tmp/nvinfer_cfg_*.txt') +
           (_glob.glob(os.path.join(_video_dir, 'upload_*')) if _video_dir else []) +
           (_glob.glob(os.path.join(_video_dir, 'norm_*.mp4')) if _video_dir else [])):
    try: os.unlink(_p)
    except: pass


def _restore_saved_inference():
    """Restore the operator's last running input after daemon restart/reboot."""
    startup = _startup_settings_snapshot()
    # 추론이 중지된 상태에서도 /status와 WebUI는 저장된 모델 선택을 보여야 한다.
    # 기존에는 desired_running=false이면 아래에서 즉시 반환하여, JSON의
    # use_peoplenet 값과 런타임 _st 값이 서로 달라질 수 있었다.
    _st['use_peoplenet'] = _as_bool(
        startup.get('use_peoplenet', True), True)
    if not _as_bool(startup.get('desired_running', False), False):
        print('[RESTORE] saved state is stopped; Modbus remains ready', flush=True)
        return None

    source = str(startup.get('source', 'camera')).strip().lower()
    client = app.test_client()

    if source == 'camera':
        response = client.post('/connect_camera', json={
            'device': startup.get('camera_device') or CAMERA_DEVICE,
            'width': int(startup.get('camera_width') or CAMERA_WIDTH),
            'height': int(startup.get('camera_height') or CAMERA_HEIGHT),
            'fps': int(startup.get('camera_fps') or CAMERA_FPS),
        })
        source_name = startup.get('camera_device') or CAMERA_DEVICE
    elif source == 'video':
        configured_files = startup.get('video_files') or []
        video_paths, missing_files = _resolve_saved_video_paths(configured_files)
        if not video_paths:
            detail = f' configured={missing_files!r}' if missing_files else ''
            _log_restore_failure(f'no saved video input under {VIDEO_DIR}{detail}')
            return False
        normalized_files = [os.path.relpath(path, VIDEO_DIR) for path in video_paths]
        if normalized_files != configured_files:
            _update_startup_settings(video_files=normalized_files)
        if missing_files:
            print(f'[RESTORE] 찾지 못한 저장 영상 제외: {missing_files}', flush=True)
        response = client.post('/add_local', json={'paths': video_paths})
        source_name = ','.join(video_paths)
    else:
        _log_restore_failure(f'unsupported saved source={source!r}')
        return False

    if response.status_code != 200:
        message = response.get_json(silent=True) or {'error': response.get_data(as_text=True)}
        _log_restore_failure(f'input prepare failed source={source_name}: {message}')
        return False

    response = client.post('/start', json={
        '_restore_saved_state': True,
        'show_stream': _as_bool(startup.get('show_stream', True), True),
        'conf': float(startup.get('confidence', _ROI_DEFAULT_CONFIDENCE)),
        'conf_list': startup.get('conf_list') or [],
        'use_peoplenet': _as_bool(startup.get('use_peoplenet', True), True),
        'tracker': startup.get('tracker_key', DEFAULT_TRACKER_KEY),
        'infer_interval': int(startup.get('infer_interval', 0)),
        'infer_w': int(startup.get('infer_w', 640)),
        'infer_h': int(startup.get('infer_h', 640)),
        'mux_w': int(startup.get('mux_w', 0)),
        'mux_h': int(startup.get('mux_h', 0)),
    })
    if response.status_code != 200:
        message = response.get_json(silent=True) or {'error': response.get_data(as_text=True)}
        _log_restore_failure(f'inference start failed source={source_name}: {message}')
        return False

    result = response.get_json(silent=True) or {}
    if result.get('restore_cancelled'):
        print('[RESTORE] cancelled because saved state changed to stopped', flush=True)
        return None

    _log_restore_success(source, source_name)
    return True


def _restore_retry_loop():
    retry_seconds = max(1.0, float(os.environ.get('DS_CAMERA_RESTORE_RETRY_SEC', '5')))
    while not _service_stop.wait(retry_seconds):
        if _st.get('running'):
            return
        result = _restore_saved_inference()
        if result is not False:
            return

def run(host=None, port=None, debug=False, threaded=True):
    """Run the DeepStream camera pipeline web app."""
    host = host or os.environ.get('HOST', '0.0.0.0')
    port = int(port or os.environ.get('PORT', '5022'))
    _start_background_services()
    restore_result = _restore_saved_inference()
    if restore_result is False:
        threading.Thread(
            target=_restore_retry_loop,
            daemon=True,
            name='SavedInputRestore',
        ).start()
    try:
        app.run(host=host, port=port, debug=debug, threaded=threaded)
    finally:
        _stop_background_services()


if __name__ == '__main__':
    run()
