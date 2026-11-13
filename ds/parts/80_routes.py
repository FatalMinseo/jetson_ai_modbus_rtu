# Auto-split from runtime.py: lines 3099-3933
def _scan_models():
    """Return the single production model exposed by this appliance."""
    return [FIXED_MODEL_NAME]


@app.route('/')
def index():
    startup = _startup_settings_snapshot()
    saved_tracker = str(
        startup.get('tracker_key', DEFAULT_TRACKER_KEY)
    ).strip().lower()
    if saved_tracker not in TRACKER_PROFILES:
        saved_tracker = DEFAULT_TRACKER_KEY
    return render_template('bench.html',
                           mode='Camera', mode_color='#b03c00',
                           camera_mode=True,
                           camera_name=CAMERA_NAME,
                           camera_device=CAMERA_DEVICE,
                           models=_scan_models(),
                           tracker_options=[
                               {'value': key, 'label': profile['label']}
                               for key, profile in TRACKER_PROFILES.items()
                           ],
                           default_tracker=saved_tracker,
                           default_use_peoplenet=_as_bool(
                               startup.get('use_peoplenet', True), True),
                           # Local/offline is the default. External STUN is
                           # opt-in and is never required for Modbus/inference.
                           webrtc_stun_server=os.environ.get(
                               'WEBRTC_STUN_SERVER', '').strip(),
                           preview_transport=_PREVIEW_TRANSPORT,
                           realtime_window_ms=int(round(_refresh_realtime_window() * 1000)))


@app.route('/stream.mjpg')
def local_mjpeg_stream():
    """On-device preview transported only through the local HTTP connection."""
    def generate():
        global _mjpeg_clients
        last_sequence = -1
        with _mjpeg_clients_lock:
            _mjpeg_clients += 1
            first_client = _mjpeg_clients == 1
        if first_client:
            _set_mjpeg_fallback_enabled(True)
        try:
            while True:
                with _preview_condition:
                    _preview_condition.wait_for(
                        lambda: _preview_sequence != last_sequence,
                        timeout=2.0)
                    jpeg = _preview_jpeg
                    last_sequence = _preview_sequence
                if not jpeg:
                    continue
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n'
                       b'Content-Length: ' + str(len(jpeg)).encode('ascii') + b'\r\n'
                       b'Cache-Control: no-store\r\n\r\n' + jpeg + b'\r\n')
        finally:
            with _mjpeg_clients_lock:
                _mjpeg_clients = max(0, _mjpeg_clients - 1)
                last_client = _mjpeg_clients == 0
            if last_client:
                _set_mjpeg_fallback_enabled(False)

    return Response(
        generate(),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={'Cache-Control': 'no-store, no-cache, must-revalidate'})


@app.route('/last_frame.jpg')
def local_last_frame():
    """Return the last completed preview frame while the live stream recovers."""
    with _preview_condition:
        jpeg = _preview_jpeg
    if not jpeg:
        return Response(status=204)
    return Response(
        jpeg,
        mimetype='image/jpeg',
        headers={'Cache-Control': 'no-store, no-cache, must-revalidate'})


_VIDEO_EXTS = {'.mp4', '.avi', '.mkv', '.mov', '.ts', '.h264', '.h265', '.hevc'}


@app.route('/connect_camera', methods=['POST'])
def connect_camera():
    """USB 카메라를 현재 입력으로 등록하고 중앙 프리뷰를 준비한다."""
    global _streams
    if _st['running']:
        return jsonify({'error': '추론 중에는 카메라를 변경할 수 없습니다'}), 400
    data = request.json or {}
    device = _find_camera_device(data.get('device') or CAMERA_DEVICE)
    if not device:
        return jsonify({'error': '카메라 장치를 찾을 수 없습니다 (/dev/video*)'}), 404
    width = int(data.get('width') or CAMERA_WIDTH)
    height = int(data.get('height') or CAMERA_HEIGHT)
    fps = int(data.get('fps') or CAMERA_FPS)
    # MJPEG 프리뷰가 device를 놓는 데 최대 1초 걸리므로 재시도
    jpeg = None
    for _attempt in range(4):
        jpeg = _pull_jpeg_once(device, width, height, fps)
        if jpeg:
            break
        import time as _t; _t.sleep(0.4)
    if not jpeg:
        return jsonify({'error': f'카메라 프레임을 가져오지 못했습니다: {device}'}), 500

    try:
        img = Image.open(_io.BytesIO(jpeg))
        width, height = img.size
    except Exception:
        pass

    _cleanup_video_temps()
    ss = _make_stream_state(device, CAMERA_NAME, fps, width, height, 0.0)
    ss.update({
        'stream_idx': 0,
        'is_camera': True,
        'camera_device': device,
        'source_uri': '',
        'expected_loop_frames': 0,
    })
    _apply_persisted_roi_to_stream(ss)
    _streams = [ss]
    _update_startup_settings(
        source='camera',
        camera_device=device,
        camera_width=width,
        camera_height=height,
        camera_fps=fps,
    )

    import base64
    from urllib.parse import quote
    thumb = _make_small_thumb(jpeg, max_dim=80)
    frame_b64 = base64.b64encode(jpeg).decode()
    stream_out = {
        'kind': 'camera',
        'name': CAMERA_NAME,
        'fps': fps,
        'width': width,
        'height': height,
        'duration': 0,
        'thumb': base64.b64encode(thumb).decode() if thumb else '',
        'frame': frame_b64,
        'preview_url': '/camera_preview.mjpg?device=' + quote(device, safe=''),
    }
    stream_out.update(_roi_response_fields(ss))
    return jsonify({
        'ok': True,
        'mode': 'camera',
        'camera_device': device,
        'streams': [stream_out],
        'composite_thumb': frame_b64,
        'grid_rows': 1,
        'grid_cols': 1,
    })


@app.route('/disconnect_camera', methods=['POST'])
def disconnect_camera():
    """현재 카메라 입력 등록을 해제한다. 추론 중에는 입력을 바꾸지 않는다."""
    global _streams
    if _st['running']:
        return jsonify({'error': '추론 중에는 카메라 연결을 끊을 수 없습니다'}), 400
    if any(ss.get('is_camera') for ss in _streams):
        _streams = []
    _update_startup_settings(desired_running=False)
    return jsonify({'ok': True, 'camera_connected': False})


@app.route('/camera_snapshot')
def camera_snapshot():
    """ROI 보정 모달용 현재 카메라 프레임 스냅샷."""
    device = _find_camera_device(request.args.get('device') or CAMERA_DEVICE)
    if not device:
        return jsonify({'error': '카메라 장치를 찾을 수 없습니다 (/dev/video*)'}), 404
    jpeg = _pull_jpeg_once(device, CAMERA_WIDTH, CAMERA_HEIGHT, CAMERA_FPS)
    if not jpeg:
        return jsonify({'error': '현재 카메라 프레임을 가져오지 못했습니다'}), 500
    width, height = CAMERA_WIDTH, CAMERA_HEIGHT
    try:
        img = Image.open(_io.BytesIO(jpeg))
        width, height = img.size
    except Exception:
        pass
    import base64
    return jsonify({
        'ok': True,
        'frame': base64.b64encode(jpeg).decode(),
        'width': width,
        'height': height,
        'device': device,
    })


@app.route('/camera_preview.mjpg')
def camera_preview_mjpg():
    device = request.args.get('device') or CAMERA_DEVICE
    return Response(
        _camera_mjpeg_frames(device, CAMERA_WIDTH, CAMERA_HEIGHT, CAMERA_FPS),
        mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/list_videos')
def list_videos():
    """VIDEO_DIR 안의 영상 파일 목록 반환."""
    os.makedirs(VIDEO_DIR, exist_ok=True)
    items = []
    for fname in sorted(os.listdir(VIDEO_DIR)):
        if os.path.splitext(fname)[1].lower() not in _VIDEO_EXTS:
            continue
        fpath = os.path.join(VIDEO_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        items.append({
            'name': fname,
            'path': fpath,
            'size': os.path.getsize(fpath),
        })
    return jsonify({'files': items, 'dir': VIDEO_DIR})


@app.route('/add_local', methods=['POST'])
def add_local():
    """서버 로컬 경로로 영상 등록 — HTTP 파일 전송 없이 즉시 처리."""
    if _st['running']:
        return jsonify({'error': '추론 중에는 변경 불가'}), 400

    data  = request.json or {}
    paths = [p.strip() for p in data.get('paths', []) if p and p.strip()]
    if not paths:
        return jsonify({'error': '경로 없음'}), 400

    video_root = os.path.abspath(VIDEO_DIR)
    valid = []
    for p in paths:
        p_abs = os.path.abspath(p)
        if os.path.commonpath([video_root, p_abs]) != video_root:
            return jsonify({'error': f'허용되지 않은 경로: {p}'}), 400
        if not os.path.isfile(p_abs):
            return jsonify({'error': f'파일 없음: {p}'}), 400
        use_path, norm_err, _normalized = _normalize_video_to_mp4(
            p_abs, os.path.basename(p_abs))
        if not use_path:
            return jsonify({'error': f'영상 변환 실패: {p} | {norm_err}'}), 400
        info = _get_video_info(use_path)
        if info is None:
            if use_path != p_abs and os.path.exists(use_path):
                try:
                    os.unlink(use_path)
                except Exception:
                    pass
            return jsonify({'error': f'영상 메타 읽기 실패: {p}'}), 400
        fps, w, h, dur = info
        name = os.path.splitext(os.path.basename(p_abs))[0]
        valid.append((use_path, name, fps, w, h, dur))

    _cleanup_video_temps()
    return jsonify(_activate_video_streams(valid))


@app.route('/upload', methods=['POST'])
def upload():
    global _streams
    if _st['running']:
        return jsonify({'error': '추론 중에는 업로드 불가'}), 400
    if any(ss.get('is_camera') for ss in _streams):
        return jsonify({'error': '카메라 연결 상태에서는 테스트 영상을 업로드할 수 없습니다'}), 400
    files = request.files.getlist('video')
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({'error': '파일 없음'}), 400

    _cleanup_video_temps()
    _streams = []

    # ── 1단계: 파일 저장 + 메타 추출 (순차 — 디스크 I/O 직렬화) ──
    saved = []   # (path, name, fps, w, h) 또는 skip
    os.makedirs(VIDEO_DIR, exist_ok=True)
    for f in files:
        ext  = os.path.splitext(f.filename)[1] or '.mp4'
        fd, path = tempfile.mkstemp(suffix=ext, prefix='upload_', dir=VIDEO_DIR)
        os.close(fd); f.save(path)
        use_path, norm_err, normalized = _normalize_video_to_mp4(path, f.filename)
        if normalized and path != use_path and os.path.exists(path):
            try:
                os.unlink(path)
            except Exception:
                pass
        elif not use_path:
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except Exception:
                    pass
            print(f'[UPLOAD] 변환 실패로 스킵: {f.filename} | {norm_err}', flush=True)
            continue
        else:
            use_path = path

        info = _get_video_info(use_path)
        if info is None:
            try:
                os.unlink(use_path)
            except Exception:
                pass
            continue
        fps, w, h, dur = info
        name = os.path.splitext(f.filename)[0]
        saved.append((use_path, name, fps, w, h, dur))

    if not saved:
        return jsonify({'error': '유효한 영상 없음'}), 400

    return jsonify(_activate_video_streams(saved))


@app.route('/start', methods=['POST'])
def start():
    global _stream_threads
    data = request.json or {}
    restore_saved_state = _as_bool(data.get('_restore_saved_state', False), False)
    if (restore_saved_state and not _as_bool(
            _startup_settings_snapshot().get('desired_running', False), False)):
        return jsonify({'ok': True, 'restore_cancelled': True})
    if _st.get('stopping'):
        return jsonify({'error': '이전 파이프라인 정리 중입니다'}), 409
    # 중복 시작 명령은 실행 중인 GStreamer graph를 건드리지 않는다.
    if _st.get('running'):
        if any(t.is_alive() for t in _stream_threads):
            return jsonify({
                'ok': True,
                'already_running': True,
                'num_streams': len(_streams),
                'model': _st.get('model_name', FIXED_MODEL_NAME),
                'tracker': _st.get('tracker_key', DEFAULT_TRACKER_KEY),
            })
        return jsonify({'error': '실행 상태 복구 중입니다'}), 409
    if not _streams:
        return jsonify({'error': '카메라 또는 테스트 영상이 준비되지 않았습니다'}), 400
    requested_conf_list = data.get('conf_list', [])
    requested_conf = float(
        requested_conf_list[0]
        if requested_conf_list
        else data.get('conf', _st.get('conf', 0.30))
    )
    if not 0.0 <= requested_conf <= 1.0:
        return jsonify({'error': 'confidence는 0.00~1.00 범위여야 합니다'}), 400
    requested_tracker = str(
        data.get('tracker', _st.get('tracker_key', DEFAULT_TRACKER_KEY))
    ).strip().lower()
    if requested_tracker not in TRACKER_PROFILES:
        return jsonify({
            'error': f'지원하지 않는 트래커: {requested_tracker}',
            'available_trackers': list(TRACKER_PROFILES),
        }), 400
    tracker_profile = _get_tracker_profile(requested_tracker)
    if not os.path.isfile(tracker_profile['ll_config_file']):
        return jsonify({'error': f'트래커 설정 파일 없음: {tracker_profile["ll_config_file"]}'}), 500
    if not tracker_profile['ll_lib_file']:
        return jsonify({'error': f'{tracker_profile["label"]} 트래커 라이브러리를 찾을 수 없습니다'}), 500
    if not _set_external_modbus_config(
        _modbus_comm.BBOX_CONFIDENCE_ADDRESS,
        [round(requested_conf * 100)],
    ):
        return jsonify({'error': 'Modbus confidence 동기화 실패'}), 503
    _st['show_stream']    = _as_bool(data.get('show_stream', _st.get('show_stream', True)), True)
    _set_output_enabled(_st['show_stream'])
    _st['conf']           = float(data.get('conf', _st.get('conf', 0.30)))
    # 채널별 conf 적용 (conf_list 없으면 전체 공통 conf 사용)
    conf_list = requested_conf_list
    for i, ss in enumerate(_streams):
        if i < len(conf_list):
            ss['conf'] = float(conf_list[i])
        elif 'conf' not in ss:
            ss['conf'] = _st['conf']
    _st['model_name']     = FIXED_MODEL_NAME
    _st['use_peoplenet']  = _as_bool(
        data.get('use_peoplenet', _st.get('use_peoplenet', True)), True)
    _st['peoplenet_active'] = False
    _st['tracker_key']    = tracker_profile['key']
    _st['tracker_name']   = tracker_profile['label']
    _st['infer_interval'] = int(data.get('infer_interval', 0))
    # 모델 입력 해상도 (기본 640 — YOLOv8s/m/l/x 공통, 1280 모델은 명시 필요)
    _st['infer_w']        = int(data.get('infer_w', 640))
    _st['infer_h']        = int(data.get('infer_h', 640))
    # mux 해상도 재정의 (0 = 스트림 최대 해상도 자동)
    _st['mux_w_override'] = int(data.get('mux_w', 0))
    _st['mux_h_override'] = int(data.get('mux_h', 0))
    _st['restart_requested'] = False
    _st['restart_reason'] = ''
    # 입력 준비 도중 사용자가 정지를 눌렀다면 오래된 부팅 복원 요청이
    # desired_running=true를 다시 덮어쓰지 못하게 한다.
    if (restore_saved_state and not _as_bool(
            _startup_settings_snapshot().get('desired_running', False), False)):
        return jsonify({'ok': True, 'restore_cancelled': True})
    _st['running']        = True

    _update_startup_settings(
        desired_running=True,
        show_stream=bool(_st['show_stream']),
        confidence=float(_st['conf']),
        conf_list=[float(ss.get('conf', _st['conf'])) for ss in _streams],
        use_peoplenet=bool(_st['use_peoplenet']),
        tracker_key=str(_st['tracker_key']),
        infer_interval=int(_st['infer_interval']),
        infer_w=int(_st['infer_w']),
        infer_h=int(_st['infer_h']),
        mux_w=int(_st['mux_w_override']),
        mux_h=int(_st['mux_h_override']),
    )

    t = threading.Thread(target=_infer_loop_single, daemon=True)
    t.start()
    _stream_threads = [t]
    return jsonify({
        'ok': True,
        'num_streams': len(_streams),
        'model': FIXED_MODEL_NAME,
        'tracker': _st['tracker_key'],
        'output': 'mjpeg',
    })


@app.route('/stop', methods=['POST'])
def stop():
    global _stream_threads
    if _st.get('stopping'):
        return jsonify({'ok': True, 'stopping': True})
    _st['running'] = False
    _st['stopping'] = True
    _st['restart_requested'] = False
    _st['restart_reason'] = ''
    _update_startup_settings(desired_running=False)
    threads_to_join = list(_stream_threads)
    def _stop_and_cleanup():
        try:
            for t in threads_to_join:
                if t.is_alive():
                    t.join(timeout=8.0)
            _stop_live_restreams()
            _cleanup_video_temps()
            _stream_threads[:] = []
        finally:
            _st['stopping'] = False
    threading.Thread(target=_stop_and_cleanup, daemon=True).start()
    return jsonify({'ok': True, 'stopping': True})


@app.route('/set_output', methods=['POST'])
def set_output():
    data = request.json or {}
    enabled = _as_bool(data.get('show_stream', True), True)
    _set_output_enabled(enabled)
    _update_startup_settings(show_stream=bool(enabled))
    return jsonify({
        'ok': True,
        'show_stream': bool(_st.get('show_stream', True)),
        'running': bool(_st.get('running', False)),
    })


@app.route('/db/send_now', methods=['POST'])
def db_send_now():
    result = _send_db_now()
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/db/payload')
def db_payload():
    snapshot = _build_db_payload_snapshot()
    return jsonify({
        'ok': snapshot.get('format_valid', False),
        **snapshot,
        'db_status': _get_db_status(),
    })


@app.route('/set_roi', methods=['POST'])
def set_roi():
    data  = request.json or {}
    si    = int(data.get('stream_idx', 0))
    roi   = data.get('roi_pts', [])
    l1    = data.get('line1', [])
    l2    = data.get('line2', [])
    width  = int(data.get('width', 0))
    height = int(data.get('height', 0))
    ref_dist = float(data.get('ref_distance', 10.0) or 10.0)
    if not 0 <= si < len(_streams):
        return jsonify({'error': '유효하지 않은 stream_idx'}), 404
    if not 0.1 <= ref_dist <= 655.35:
        return jsonify({'error': 'ROI 거리는 0.10~655.35m 범위여야 합니다'}), 400
    ready = len(roi) >= 3 and len(l1) == 2 and len(l2) == 2
    if not _set_external_modbus_config(
        _modbus_comm.ROI_REFERENCE_DISTANCE_ADDRESS,
        [round(ref_dist * 100)],
    ):
        return jsonify({'error': 'Modbus ROI 거리 동기화 실패'}), 503
    _streams[si].update({'roi_pts': roi, 'line1': l1,
                          'line2': l2, 'roi_ready': ready,
                          'ref_distance': ref_dist})
    _persist_stream_roi(_streams[si])
    return jsonify({'ok': True, 'roi_ready': ready, 'ref_distance': ref_dist})


@app.route('/internal/modbus/config', methods=['POST'])
def internal_modbus_config():
    """Apply a validated localhost modbusd configuration write to live streams."""
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'error': 'localhost only'}), 403
    data = request.json or {}
    try:
        start_address = int(data.get('start'))
        values = [int(value) for value in data.get('values', [])]
    except (TypeError, ValueError):
        return jsonify({'error': 'invalid register values'}), 400
    result = _handle_modbus_config_write(start_address, values)
    if result is False or result is None:
        return jsonify({'error': 'write rejected'}), 400
    return jsonify({'ok': True})


@app.route('/save_roi_log', methods=['POST'])
def save_roi_log():
    """버튼 클릭 시에만 설정 프리셋 저장. 동일 ROI+confidence면 duplicate 반환."""
    data   = request.json or {}
    si     = int(data.get('stream_idx', 0))
    roi    = data.get('roi_pts', [])
    l1     = data.get('line1', [])
    l2     = data.get('line2', [])
    width  = int(data.get('width', 0))
    height = int(data.get('height', 0))
    conf_threshold = round(float(data.get('conf_threshold', 0.30) or 0.30), 2)
    ref_distance = max(0.1, float(data.get('ref_distance', 10.0) or 10.0))
    if not (len(roi) >= 3 and len(l1) == 2 and len(l2) == 2):
        return jsonify({'error': 'ROI가 완성되지 않았습니다'}), 400
    key  = _roi_key(roi, l1, l2, width, height, conf_threshold)
    logs = _load_roi_logs()
    for entry in logs:
        if 'conf_threshold' not in entry:
            continue
        ekey = _roi_key(entry.get('roi_points', []), entry.get('line1', []),
                        entry.get('line2', []), entry.get('width', 0),
                        entry.get('height', 0), entry.get('conf_threshold'))
        if ekey == key:
            return jsonify({'duplicate': True})
    now = datetime.now().strftime('%Y%m%d_%H%M%S')
    name = data.get('name', '') or now
    logs.insert(0, {'name': name, 'roi_points': roi,
                    'line1': l1, 'line2': l2,
                    'width': int(width), 'height': int(height),
                    'conf_threshold': conf_threshold,
                    'ref_distance': ref_distance})
    _save_roi_logs(logs)
    return jsonify({'ok': True, 'name': name, 'conf_threshold': conf_threshold})


@app.route('/roi_logs')
def roi_logs():
    return jsonify(_load_roi_logs())


@app.route('/delete_roi', methods=['POST'])
def delete_roi():
    data = request.json or {}
    name = data.get('name', '')
    logs = _load_roi_logs()
    new_logs = [e for e in logs if e.get('name') != name]
    if len(new_logs) == len(logs):
        return jsonify({'error': '해당 설정 프리셋 없음'}), 404
    _save_roi_logs(new_logs)
    return jsonify({'ok': True})


@app.route('/rename_roi', methods=['POST'])
def rename_roi():
    data     = request.json or {}
    old_name = data.get('old_name', '')
    new_name = data.get('new_name', '').strip()
    if not new_name:
        return jsonify({'error': '이름이 비어있음'}), 400
    logs = _load_roi_logs()
    if any(e.get('name') == new_name for e in logs):
        return jsonify({'error': '이미 존재하는 이름'}), 409
    for entry in logs:
        if entry.get('name') == old_name:
            entry['name'] = new_name
            _save_roi_logs(logs)
            return jsonify({'ok': True})
    return jsonify({'error': '해당 설정 프리셋 없음'}), 404


@app.route('/load_roi', methods=['POST'])
def load_roi():
    data  = request.json or {}
    si    = int(data.get('stream_idx', 0))
    name  = data.get('name', '')
    logs  = _load_roi_logs()
    entry = next((e for e in logs if e.get('name') == name), None)
    if entry is None:
        return jsonify({'error': '해당 설정 프리셋 없음'}), 404
    if not 0 <= si < len(_streams):
        return jsonify({'error': '유효하지 않은 stream_idx'}), 404
    roi   = entry['roi_points']
    l1    = entry['line1']
    l2    = entry['line2']
    ready = len(roi) >= 3 and len(l1) == 2 and len(l2) == 2
    ref_distance = float(entry.get('ref_distance', 10.0) or 10.0)
    if not 0.1 <= ref_distance <= 655.35:
        return jsonify({'error': '프리셋 ROI 거리값이 유효하지 않습니다'}), 400
    if not _set_external_modbus_config(
        _modbus_comm.ROI_REFERENCE_DISTANCE_ADDRESS,
        [round(ref_distance * 100)],
    ):
        return jsonify({'error': 'Modbus ROI 거리 동기화 실패'}), 503
    _streams[si].update({'roi_pts': roi, 'line1': l1,
                          'line2': l2, 'roi_ready': ready,
                          'ref_distance': ref_distance})
    _persist_stream_roi(_streams[si])
    payload = {'ok': True, 'roi_ready': ready,
               'roi_pts': roi, 'line1': l1, 'line2': l2}
    if 'conf_threshold' in entry:
        payload['conf_threshold'] = round(float(entry.get('conf_threshold') or 0.30), 2)
    if 'ref_distance' in entry:
        payload['ref_distance'] = round(float(entry.get('ref_distance') or 10.0), 2)
    return jsonify(payload)


@app.route('/reset_counts', methods=['POST'])
def reset_counts():
    """영상 진행 상태는 유지하고 누적 카운트·속도 상태만 초기화."""
    data   = request.json or {}
    si_raw = data.get('stream_idx', None)

    def _reset_one(ss):
        with ss['_lock']:
            _reset_stream_interval_counters(ss, reset_tracking=True)
            _reset_stream_rolling_window(ss)

    if si_raw is not None and 0 <= int(si_raw) < len(_streams):
        _reset_one(_streams[int(si_raw)])
    else:
        for ss in _streams:
            _reset_one(ss)

    # inferd의 메모리 값은 무조건 즉시 초기화한다. 외부 Modbus 저장소 동기화
    # 실패가 영상/추론 또는 UI 초기화를 되돌리지는 않게 한다.
    modbus_reset_ok = _clear_external_modbus_slots(None)
    return jsonify({'ok': True, 'modbus_reset_ok': bool(modbus_reset_ok)})


@app.route('/internal/modbus/clear', methods=['POST'])
def internal_modbus_clear():
    """Mirror an authoritative Master/modbusd slot clear into WebUI counters."""
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'error': 'localhost only'}), 403
    data = request.json or {}
    try:
        start_address = int(data.get('start'))
        values = [int(value) for value in data.get('values', [])]
    except (TypeError, ValueError):
        return jsonify({'error': 'invalid register values'}), 400
    if not _clear_modbus_cumulative_slot(start_address, values):
        return jsonify({'error': 'clear rejected'}), 400
    return jsonify({'ok': True})


@app.route('/reset_roi', methods=['POST'])
def reset_roi():
    data   = request.json or {}
    si_raw = data.get('stream_idx', None)
    def _reset_one(ss):
        with ss['_lock']:
            ss.update({'roi_pts': [], 'line1': [], 'line2': [], 'roi_ready': False})
            ss['ref_distance'] = 10.0
            _clear_persisted_stream_roi(ss)
            _reset_stream_interval_counters(ss, reset_tracking=True)
            _reset_stream_rolling_window(ss)
            ss['last_osd_objs'] = 0
            ss['last_model_added'] = 0
    if si_raw is not None and 0 <= int(si_raw) < len(_streams):
        _reset_one(_streams[int(si_raw)])
    else:
        for ss in _streams: _reset_one(ss)
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════════
#  Local WebRTC signaling (HTTP polling; no WebSocket/network-online)
# ═══════════════════════════════════════════════════════════════
@app.route('/webrtc/prepare', methods=['POST'])
def webrtc_prepare():
    """Renegotiate only the WebRTC bin; never rebuild the AI pipeline."""
    global _webrtc_offer_sdp, _webrtc_ice_srv, _webrtc_gen
    if not _st.get('running'):
        return jsonify({'error': '추론 중이 아님'}), 400
    if not _WEBRTC_AVAILABLE:
        return jsonify({'error': 'WebRTC 미지원'}), 501
    with _webrtc_lock:
        wb = _webrtc_bin
        if wb is None:
            return jsonify({'error': 'webrtcbin 없음'}), 503
        _webrtc_offer_sdp = None
        _webrtc_ice_srv = []
        _webrtc_session['id'] = None
        _webrtc_gen += 1
        generation = _webrtc_gen
    try:
        options = Gst.Structure.new_empty('application/x-webrtc-offer-options')
        options.set_value('ice-restart', True)
        promise = Gst.Promise.new_with_change_func(
            _webrtc_on_offer_created, wb, None)
        wb.emit('create-offer', options, promise)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    return jsonify({'ok': True, 'gen': generation})


@app.route('/webrtc/offer')
def webrtc_offer():
    with _webrtc_lock:
        sdp = _webrtc_offer_sdp
        generation = _webrtc_gen
        if sdp and not _webrtc_session['id']:
            _webrtc_session['id'] = _secrets.token_urlsafe(18)
        session_id = _webrtc_session['id']
    if not sdp:
        return jsonify({'error': 'offer not ready'}), 503
    return jsonify({
        'type': 'offer', 'sdp': sdp,
        'gen': generation, 'session': session_id,
    })


@app.route('/webrtc/answer', methods=['POST'])
def webrtc_answer():
    if not _WEBRTC_AVAILABLE:
        return jsonify({'error': 'WebRTC 미지원'}), 501
    data = request.json or {}
    try:
        client_gen = int(data.get('gen'))
    except (TypeError, ValueError):
        return jsonify({'error': 'webrtc 세대값 없음'}), 400
    client_session = str(data.get('session') or '')
    sdp_text = str(data.get('sdp') or '')
    if not client_session or not sdp_text:
        return jsonify({'error': 'webrtc 세션 또는 sdp 없음'}), 400
    with _webrtc_lock:
        wb = _webrtc_bin
        current_gen = _webrtc_gen
        current_session = _webrtc_session['id']
    if client_gen != current_gen or client_session != current_session:
        return jsonify({'error': '만료된 webrtc 세션',
                        'current_gen': current_gen}), 409
    if wb is None:
        return jsonify({'error': 'webrtcbin 없음'}), 503
    try:
        _ret, sdp = GstSdp.SDPMessage.new_from_text(sdp_text)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdp)
        wb.emit('set-remote-description', answer, None)
        return jsonify({'ok': True})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/webrtc/ice', methods=['GET', 'POST'])
def webrtc_ice():
    if request.method == 'GET':
        try:
            client_gen = int(request.args.get('gen'))
        except (TypeError, ValueError):
            return jsonify({'error': 'webrtc 세대값 없음'}), 400
        client_session = str(request.args.get('session') or '')
        with _webrtc_lock:
            current_gen = _webrtc_gen
            current_session = _webrtc_session['id']
            candidates = list(_webrtc_ice_srv)
        if client_gen != current_gen or client_session != current_session:
            return jsonify({'error': '만료된 webrtc 세션',
                            'current_gen': current_gen}), 409
        return jsonify(candidates)

    if not _WEBRTC_AVAILABLE:
        return jsonify({'error': 'WebRTC 미지원'}), 501
    data = request.json or {}
    try:
        client_gen = int(data.get('gen'))
    except (TypeError, ValueError):
        return jsonify({'error': 'webrtc 세대값 없음'}), 400
    client_session = str(data.get('session') or '')
    with _webrtc_lock:
        wb = _webrtc_bin
        current_gen = _webrtc_gen
        current_session = _webrtc_session['id']
    if client_gen != current_gen or client_session != current_session:
        return jsonify({'error': '만료된 webrtc 세션',
                        'current_gen': current_gen}), 409
    if wb is not None:
        try:
            wb.emit('add-ice-candidate',
                    int(data.get('sdpMLineIndex', 0)),
                    str(data.get('candidate', '')))
        except Exception:
            pass
    return jsonify({'ok': True})


@app.route('/status')
def status():
    desired_running = _as_bool(
        _startup_settings_snapshot().get('desired_running', False), False)
    streams_out = []
    total_fps_sum = infer_fps_sum = 0.0
    total_count_all = 0
    n = len(_streams)
    authoritative_counts = _get_external_modbus_counts()
    for i, ss in enumerate(_streams):
        # The inference thread and FC16 clear both update these aggregates.
        # Keep the UI's count/speed snapshot internally consistent.
        with ss['_lock']:
            # This package operates one camera.  In external Modbus mode the
            # persistent modbusd slots are the sole source for both menu 10
            # and the WebUI; inferd's volatile mirror is diagnostic only.
            if i == 0 and authoritative_counts is not None:
                counts_out = authoritative_counts['counts']
                speed_stat = authoritative_counts['speed_stat']
            else:
                counts_out = _build_counts_out(ss)
                speed_stat = (
                    _build_modbus_ui_speed_stat(ss)
                    if _COMM_MODE == 'modbus_rtu'
                    else ss.get('speed_stat', _acc_empty())
                )
        tc = sum(v['합계'] for v in counts_out.values())
        total_count_all += tc
        total_fps_sum  += ss['total_fps']
        infer_fps_sum  += ss['infer_fps']
        streams_out.append({
            'idx':       i,
            'name':      ss['video_name'],
            'frame_w':   ss['frame_w'],
            'frame_h':   ss['frame_h'],
            'duration_sec': round(ss.get('duration_sec', 0.0), 2),
            'video_fps': round(ss['video_fps'], 1),
            'loop_count': ss['loop_count'],
            'frame_idx': ss['frame_idx'],
            'total_elapsed_sec': round(
                ss.get('_total_elapsed_sec', 0.0) + _stream_played_seconds(ss), 1),
            'total_fps': round(ss['total_fps'], 1),
            'infer_fps': round(ss['infer_fps'], 1),
            'fps_stat':   _acc_stat(ss['fps_stat'])
                          if isinstance(ss['fps_stat'], dict) and 'n' in ss['fps_stat']
                          else ss['fps_stat'],
            'infer_stat': _acc_stat(ss['infer_stat'])
                          if isinstance(ss['infer_stat'], dict) and 'n' in ss['infer_stat']
                          else ss['infer_stat'],
            'counts':       counts_out,
            'total_count':  tc,
            'gpu_pct':      round(ss.get('gpu_pct', 0.0), 1),
            'cpu_pct':      round(ss.get('cpu_pct', 0.0), 1),
            'conf':         round(float(ss.get('conf', _st.get('conf', 0.30))), 2),
            'roi_ready':    ss.get('roi_ready', False),
            'roi_pts':      ss.get('roi_pts', []),
            'line1':        ss.get('line1', []),
            'line2':        ss.get('line2', []),
            'ref_distance': round(float(ss.get('ref_distance', 0.0)), 2),
            'speed_stat':   _acc_stat(speed_stat)
                            if isinstance(speed_stat, dict) and speed_stat.get('n', 0) > 0
                            else None,
            'last_infer_dets': int(ss.get('last_infer_dets', 0)),
            'last_model_added': int(ss.get('last_model_added', 0)),
            'last_osd_objs':   int(ss.get('last_osd_objs', 0)),
            'tensor_seen':     bool(ss.get('tensor_seen', False)),
            'latest_loop_result': ss.get('latest_loop_result'),
            'latest_loop_completed_at': ss.get('latest_loop_completed_at', ''),
            'det_log': list(reversed(ss.get('det_log', [])[-30:])),  # 최신 30건 역순
        })
    gpu_pct = _read_gpu_pct(); cpu_pct = _read_cpu_pct()
    _st['gpu_pct'] = gpu_pct; _st['cpu_pct'] = cpu_pct
    camera_connected = any(ss.get('is_camera') for ss in _streams)
    source_mode = 'v4l2src(camera)' if camera_connected else (
        'test-video-loop' if _streams else 'camera-unconnected'
    )
    return jsonify({
        'running':         _st['running'],
        'desired_running': desired_running,
        'stopping':        bool(_st.get('stopping', False)),
        'source_mode':     source_mode,
        'camera_connected': camera_connected,
        'test_video_available': not camera_connected and not _st['running'],
        'live_restream':   False,
        'live_restream_backend': '',
        'roi_ready':       any(ss.get('roi_ready') for ss in _streams),
        'num_streams':     n,
        'streams':         streams_out,
        'total_count_all': total_count_all,
        'avg_total_fps':   round(total_fps_sum/n, 1) if n else 0.0,
        'avg_infer_fps':   round(infer_fps_sum/n, 1) if n else 0.0,
        'mux_w':           _st.get('mux_w', 0),
        'mux_h':           _st.get('mux_h', 0),
        'gpu_pct':       gpu_pct,
        'cpu_pct':       cpu_pct,
        'gpu_stat':      _acc_stat(_st['gpu_stat']),
        'cpu_stat':      _acc_stat(_st['cpu_stat']),
        'pipeline_gen':  _st.get('pipeline_gen', 0),
        'use_peoplenet': bool(_st.get('use_peoplenet', True)),
        'peoplenet_active': bool(_st.get('peoplenet_active', False)),
        'infer_interval': int(_st.get('infer_interval', 0)),
        'infer_w': int(_st.get('infer_w', 640)),
        'infer_h': int(_st.get('infer_h', 640)),
        'mux_w_override': int(_st.get('mux_w_override', 0)),
        'mux_h_override': int(_st.get('mux_h_override', 0)),
        'proc_rss_mb':   round(psutil.Process().memory_info().rss / 1024 / 1024, 1),
        'sys_total_mb':  round(psutil.virtual_memory().total / 1024 / 1024, 0),
        'sys_used_mb':   round(psutil.virtual_memory().used  / 1024 / 1024, 1),
        'model_name':     FIXED_MODEL_NAME,
        'tracker_key':    _st.get('tracker_key', DEFAULT_TRACKER_KEY),
        'tracker_name':   _st.get('tracker_name', TRACKER_PROFILES[DEFAULT_TRACKER_KEY]['label']),
        'rtsp_url':       '',
        'rtsp_available': False,
        'webrtc_ready':   bool(_WEBRTC_AVAILABLE and _webrtc_offer_sdp),
        'webrtc_gen':     _webrtc_gen,
        'stream_url':     '/webrtc/offer',
        'output_mode':    'webrtc' if _WEBRTC_AVAILABLE else 'mjpeg',
        'show_stream':    bool(_st.get('show_stream', True)),
        'modbus_counts_authoritative': bool(_MODBUS_EXTERNAL and authoritative_counts is not None),
        'modbus_counts_available': bool(authoritative_counts and authoritative_counts.get('available', True)),
        'modbus_counts_stale': bool(authoritative_counts and authoritative_counts.get('stale')),
        'db_status':      _get_db_status(),
    })


@app.route('/save_result', methods=['POST'])
def save_result():
    """채널 1개의 결과 저장. stream_idx 필수."""
    try:
        data = request.json or {}
        if data.get('stream_idx') is None:
            return jsonify({'error': 'stream_idx 필수'}), 400
        fname = _write_stream_result(int(data['stream_idx']))
        return jsonify({'ok': True, 'file': fname})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/result_logs_index')
def result_logs_index():
    entries = []
    try:
        files = sorted(
            [f for f in os.listdir(RESULT_LOG_DIR) if f.endswith('.json')],
            reverse=True
        )
        for fname in files:
            path = os.path.join(RESULT_LOG_DIR, fname)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                continue
            saved_at = data.get('saved_at', '')
            date_key = data.get('date_key', saved_at[:10] if len(saved_at) >= 10 else '')
            labels = []
            preview = []
            if 'loop_result' in data:
                # 새 포맷: 채널 1개 파일
                vname = data.get('video_name', '')
                lr    = data.get('loop_result') or {}
                if vname:
                    labels.append(vname)
                preview.append({
                    'video_name': vname,
                    'total_count': int(lr.get('total_count', 0)),
                    'counts': lr.get('counts', {}),
                    'latest_loop_result': lr,
                    'latest_loop_completed_at': lr.get('completed_at', ''),
                })
            elif 'stream' in data:
                # 구 포맷 A
                s     = data.get('stream', {})
                vname = data.get('video_name') or s.get('video_name', '')
                if vname:
                    labels.append(vname)
                preview.append({
                    'video_name': vname,
                    'total_count': int(s.get('total_count', 0)),
                    'counts': s.get('counts', {}),
                    'latest_loop_result': data.get('latest_loop_result'),
                    'latest_loop_completed_at': (data.get('latest_loop_result') or {}).get('completed_at', ''),
                })
            else:
                # 구 포맷 B (streams 배열)
                streams = data.get('streams', [])
                for s in streams:
                    vname = s.get('video_name', '')
                    if vname:
                        labels.append(vname)
                    preview.append({
                        'video_name': vname,
                        'total_count': int(s.get('total_count', 0)),
                        'counts': s.get('counts', {}),
                        'latest_loop_result': s.get('latest_loop_result'),
                        'latest_loop_completed_at': s.get('latest_loop_completed_at', ''),
                    })
            entries.append({
                'file': fname,
                'saved_at': saved_at,
                'date_key': data.get('date_key', date_key),
                'model_name': data.get('model_name', ''),
                'tracker_key': data.get('tracker_key', ''),
                'tracker_name': data.get('tracker_name', ''),
                'labels': labels,
                'preview': preview,
            })
        return jsonify({'ok': True, 'entries': entries})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/recent_detections')
def api_recent_detections():
    """스트림 메모리의 det_log에서 최신 50건 반환 (디스크 읽기 없음)."""
    try:
        n = max(1, min(200, int(request.args.get('n', 50))))
    except Exception:
        n = 50
    events = []
    for ss in _streams:
        events.extend(list(ss.get('det_log', [])))
    events.sort(
        key=lambda ev: (
            str(ev.get('logged_at', '')),
            float(ev.get('vtime', 0.0) or 0.0),
            int(ev.get('cycle', 0) or 0),
        ),
        reverse=True,
    )
    events = events[:n]
    return jsonify({
        'ok': True,
        'events': events,
        'count': len(events),
        'db_status': _get_db_status(),
    })


@app.route('/api/realtime_traffic_history')
def api_realtime_traffic_history():
    """Return in-memory rows for the two real-time verification histories."""
    try:
        limit = max(1, min(5000, int(request.args.get('limit', 1000))))
    except Exception:
        limit = 1000
    try:
        stream_idx = int(request.args.get('stream_idx', -1))
    except Exception:
        stream_idx = -1

    events = []
    analyses = []
    for ss in list(_streams):
        if stream_idx >= 0 and int(ss.get('stream_idx', -1)) != stream_idx:
            continue
        with ss['_lock']:
            events.extend(dict(row) for row in ss.get('realtime_roi_exit_history', ()))
            analyses.extend(dict(row) for row in ss.get('realtime_window_history', ()))

    events.sort(key=lambda row: int(row.get('event_id', 0)))
    analyses.sort(key=lambda row: int(row.get('analysis_id', 0)))
    return jsonify({
        'ok': True,
        'window_duration_ms': int(round(_refresh_realtime_window() * 1000)),
        'events': events[-limit:],
        'analyses': analyses[-limit:],
        'event_count': len(events),
        'analysis_count': len(analyses),
        'log_dir': REALTIME_WINDOW_LOG_DIR,
    })


@app.route('/debug_streams')
def debug_streams():
    """각 스트림의 ROI 상태를 덤프 — ROI 크로스 오염 진단용"""
    out = []
    for i, ss in enumerate(_streams):
        roi_pts = ss.get('roi_pts', [])
        l1      = ss.get('line1', [])
        l2      = ss.get('line2', [])
        roi_mux, l1_mux, l2_mux = _roi_to_mux_coords(
            ss, max(1, int(_st.get('mux_w', 640))), max(1, int(_st.get('mux_h', 480))))
        out.append({
            'idx':       i,
            'name':      ss.get('video_name', ''),
            'roi_ready': ss.get('roi_ready', False),
            'roi_pts':   roi_pts,
            'line1':     l1,
            'line2':     l2,
            'roi_pts_mux': roi_mux,
            'line1_mux':   l1_mux,
            'line2_mux':   l2_mux,
            'n_roi_pts': len(roi_pts),
            'frame_w':   ss.get('frame_w', 0),
            'frame_h':   ss.get('frame_h', 0),
            'duration_sec': round(ss.get('duration_sec', 0.0), 2),
            'last_infer_dets': int(ss.get('last_infer_dets', 0)),
            'last_model_added': int(ss.get('last_model_added', 0)),
            'last_osd_objs':   int(ss.get('last_osd_objs', 0)),
            'tensor_seen':     bool(ss.get('tensor_seen', False)),
        })
    return jsonify({
        'streams':  out,
        'mux_w':    _st.get('mux_w', 0),
        'mux_h':    _st.get('mux_h', 0),
        'infer_w':  _st.get('infer_w', 640),
        'infer_h':  _st.get('infer_h', 640),
        'running':  _st['running'],
        'use_peoplenet': bool(_st.get('use_peoplenet', True)),
        'peoplenet_active': bool(_st.get('peoplenet_active', False)),
    })
