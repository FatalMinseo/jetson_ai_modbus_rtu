# Auto-split from runtime.py: lines 1323-1722
_st = {
    'running':        False,
    'stopping':       False,
    'restart_requested': False,
    'restart_reason': '',
    'show_stream':    True,
    'conf':           _ROI_DEFAULT_CONFIDENCE,
    'model_name':     FIXED_MODEL_NAME,
    # 새 설치 또는 저장값이 없는 경우 PeopleNet을 기본으로 사용한다.
    'use_peoplenet':  True,
    'peoplenet_active': False,
    'tracker_key':    FIXED_TRACKER_KEY,
    'tracker_name':   TRACKER_PROFILES[FIXED_TRACKER_KEY]['label'],
    'infer_interval': 0,
    # 추론 입력 해상도 (모델에 따라 변경 가능 — 640/1280 등)
    'infer_w':        640,
    'infer_h':        640,
    # mux 해상도 재정의 (0 = 스트림 해상도 자동 결정)
    'mux_w_override': 0,
    'mux_h_override': 0,
    # 내부 상태 (자동 계산)
    'mux_w': 640, 'mux_h': 480,
    'total_fps': 0.0,
    'gpu_pct': 0.0, 'cpu_pct': 0.0,
    'gpu_stat': _acc_empty(maxlen=60), 'cpu_stat': _acc_empty(maxlen=60),
    'lock': threading.Lock(),
    'pipeline_gen': 0,   # 파이프라인 재빌드 횟수 (프론트 영상 재연결 감지용)
}

_streams: list = []          # per-stream state dicts
_stream_threads: list = []   # pipeline thread(s)

# GStreamer WebRTC promises and ICE callbacks need a running GLib context.
_glib_loop = _GLib.MainLoop()
_glib_thread = threading.Thread(
    target=_glib_loop.run, daemon=True, name='glib-main')
_glib_thread.start()


def _as_bool(v, default=False):
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ('1', 'true', 'yes', 'y', 'on')
    return bool(v)

_rtsp_server = None


def _setup_rtsp_server():
    """RTSP is intentionally disabled; local WebRTC/MJPEG handle preview."""
    return

# ── 로컬 미리보기 / WebRTC 시그널링 상태 ───────────────────────
import secrets as _secrets

_webrtc_lock      = threading.Lock()
_webrtc_bin       = None
_webrtc_offer_sdp = None
_webrtc_ice_srv   = []
_webrtc_gen       = 0
_webrtc_session   = {'id': None}
_output_valve     = None          # 전체 화면 출력 브랜치 on/off gate
_output_lock      = threading.Lock()
_mjpeg_valve      = None          # fallback client가 있을 때만 JPEG 인코딩
_mjpeg_clients    = 0
_mjpeg_clients_lock = threading.Lock()

# Local UI preview uses only HTTP over 127.0.0.1, independent of physical NICs.
_preview_condition = threading.Condition()
_preview_jpeg      = None
_preview_sequence  = 0
_preview_sink_lock = threading.Lock()
_preview_sink      = None
_preview_pull_thread = None


def _webrtc_on_offer_created(promise, wb, _):
    """Publish an offer only when it belongs to the active pipeline."""
    reply = promise.get_reply()
    if reply is None:
        return
    offer = reply.get_value('offer')
    if offer is None:
        return
    with _webrtc_lock:
        if wb is not _webrtc_bin:
            print('[WebRTC] 이전 파이프라인 offer 폐기', flush=True)
            return
    # Do not hold the lock here: set-local-description can synchronously emit
    # ICE callbacks that acquire the same lock.
    wb.emit('set-local-description', offer, None)
    with _webrtc_lock:
        if wb is not _webrtc_bin:
            return
        global _webrtc_offer_sdp
        _webrtc_offer_sdp = offer.sdp.as_text()
    print('[WebRTC] 로컬 offer 생성 완료', flush=True)


def _set_output_enabled(enabled: bool):
    """Enable/disable display output while inference and Modbus keep running."""
    global _output_valve
    enabled = bool(enabled)
    _st['show_stream'] = enabled
    with _output_lock:
        valve = _output_valve
    if valve is not None:
        try:
            valve.set_property('drop', not enabled)
        except Exception as exc:
            print(f'[DS] output valve update failed: {exc!r}', flush=True)
    return enabled


def _set_mjpeg_fallback_enabled(enabled: bool):
    """Gate the fallback encoder without affecting WebRTC or inference."""
    with _mjpeg_clients_lock:
        valve = _mjpeg_valve
    if valve is not None:
        try:
            valve.set_property('drop', not bool(enabled))
        except Exception as exc:
            print(f'[MJPEG] fallback valve update failed: {exc!r}', flush=True)


def _wait_live_restreams_ready(timeout: float = 3.0) -> bool:
    return _RELAY_SERVICE.wait_ready(_streams, timeout)


def _start_live_restreams():
    _RELAY_SERVICE.start(_streams)


def _stop_live_restreams():
    _RELAY_SERVICE.stop(_streams)
    for ss in _streams:
        ss['_uri_decode_bin'] = None


def _make_stream_state(video_path, video_name, fps, fw, fh, duration_sec=0.0):
    eff_fps = min(_MAX_STREAM_FPS, max(1.0, float(fps or 0.0)))
    expected_loop_frames = 0
    try:
        expected_loop_frames = int(round(max(0.0, float(duration_sec or 0.0)) * eff_fps))
    except Exception:
        expected_loop_frames = 0
    return {
        'video_path':   video_path,
        'stream_idx':   -1,
        'source_uri':   '',
        'video_name':   video_name,
        'video_fps_src': max(1.0, float(fps or 0.0)),
        'video_fps':    eff_fps,
        'frame_w':      fw,
        'frame_h':      fh,
        'duration_sec': max(0.0, float(duration_sec or 0.0)),
        'expected_loop_frames': max(0, expected_loop_frames),
        'counts':       {},
        'track_state':  {},
        'loop_count':   1,
        'frame_idx':    0,
        '_total_elapsed_sec': 0.0,  # 이전 루프들의 누적 재생 시간
        'total_fps':    0.0,
        'infer_fps':    0.0,
        'fps_stat':     _acc_empty(),
        'infer_stat':   _acc_empty(),
        'gpu_pct':      0.0,
        'cpu_pct':      0.0,
        'last_infer_dets': 0,
        'last_model_added': 0,
        'last_osd_objs':   0,
        'tensor_seen':     False,
        'roi_pts':      [],
        'line1':        [],
        'line2':        [],
        'roi_ready':    False,
        'ref_distance': _ROI_DEFAULT_REF_DISTANCE_M,
        'speed_stat':   _acc_empty(),
        'speed_by_cls_dir': {},
        'rolling_window': deque(),  # confirmed ROI-exit events in time order
        'pos_history':  {},   # {tid: (frame_idx, time_sec, cx, cy)} 실시간 속도 계산용
        'live_speed':   {},   # {tid: speed_kph} EMA 평활화된 순간 속도
        'latest_loop_result': None,
        'latest_loop_completed_at': '',
        '_eos_seen': False,
        '_last_frame_num': -1,
        '_last_buf_pts': -1,
        '_first_buf_pts': -1,
        '_last_frame_time_sec': 0.0,
        '_last_loop_transition_ts': 0.0,
        '_last_progress_ts': time.perf_counter(),
        '_uri_decode_bin': None,
        '_tracker_reset_requested': False,
        '_tracker_max_id_seen': -1,
        '_tracker_empty_since': 0.0,
        '_tracker_reset_last_attempt_ts': 0.0,
        '_restream_port': 0,
        '_restream_proc': None,
        '_restream_thread': None,
        '_restream_stop': None,
        '_restream_log_path': '',
        '_lock':                 threading.Lock(),   # loop probe ↔ counting probe 경쟁 방지
        '_probe_frame_times':    deque(maxlen=90),
        '_probe_delivery_times': deque(maxlen=90),
        'last_jpeg':             None,  # 마지막 프레임 JPEG bytes
    }


def _activate_video_streams(stream_specs):
    """(path, name, fps, w, h, duration) 목록을 현재 입력 스트림으로 등록하고 프리뷰 응답을 만든다."""
    global _streams
    import base64
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _extract(spec):
        path, name, fps, w, h, dur = spec
        return path, name, fps, w, h, dur, _get_first_frame_jpeg(path)

    ordered = [None] * len(stream_specs)
    with ThreadPoolExecutor(max_workers=max(1, len(stream_specs))) as pool:
        fut_to_idx = {pool.submit(_extract, s): i for i, s in enumerate(stream_specs)}
        for fut in as_completed(fut_to_idx):
            ordered[fut_to_idx[fut]] = fut.result()

    _streams = []
    result = []
    frames_jpeg_and_names = []
    for si, (path, name, fps, w, h, dur, jpeg) in enumerate(ordered):
        ss = _make_stream_state(path, name, fps, w, h, dur)
        ss['stream_idx'] = si
        _apply_persisted_roi_to_stream(ss)
        _streams.append(ss)
        frames_jpeg_and_names.append((jpeg, name))
        thumb_small = _make_small_thumb(jpeg, max_dim=80)
        thumb_b64 = base64.b64encode(thumb_small).decode() if thumb_small else ''
        frame_b64 = base64.b64encode(jpeg).decode() if jpeg else ''
        stream_out = {
            'name': name, 'fps': round(fps, 2),
            'width': w, 'height': h, 'duration': round(dur, 2),
            'thumb': thumb_b64, 'frame': frame_b64,
        }
        stream_out.update(_roi_response_fields(ss))
        result.append(stream_out)

    composite_bytes = _make_static_composite_pil(frames_jpeg_and_names)
    composite_b64 = base64.b64encode(composite_bytes).decode() if composite_bytes else ''
    rows, cols = _compute_grid_layout(len(_streams))
    video_files = []
    for ss in _streams:
        path = os.path.abspath(ss.get('video_path') or '')
        try:
            if path and os.path.commonpath([os.path.abspath(VIDEO_DIR), path]) == os.path.abspath(VIDEO_DIR):
                video_files.append(os.path.relpath(path, VIDEO_DIR))
            elif path:
                video_files.append(path)
        except Exception:
            if path:
                video_files.append(path)
    _update_startup_settings(source='video', video_files=video_files)
    return {
        'ok': True,
        'mode': 'test_video',
        'test_video': True,
        'streams': result,
        'composite_thumb': composite_b64,
        'grid_rows': rows,
        'grid_cols': cols,
    }


# ═══════════════════════════════════════════════════════════════
#  nvinfer config 동적 생성
# ═══════════════════════════════════════════════════════════════
def _engine_cache_key(onnx_path: str, batch_size: int, infer_w: int, infer_h: int) -> str:
    import hashlib
    mtime = int(os.path.getmtime(onnx_path))
    key = f'{onnx_path}|{mtime}|b{batch_size}|{infer_w}x{infer_h}|jetson={_IS_JETSON}'
    return hashlib.md5(key.encode()).hexdigest()


def _write_temp_config(lines, prefix: str) -> str:
    fd, path = tempfile.mkstemp(suffix='.txt', prefix=prefix)
    os.write(fd, ('\n'.join(lines) + '\n').encode())
    os.close(fd)
    return path


def _make_nvinfer_cfg(model_name: str, interval: int = 1,
                      batch_size: int = 1,
                      infer_w: int = 640, infer_h: int = 640) -> str:
    """Create nvinfer config for the fixed bundled TensorRT engine."""
    del model_name
    engine_path = FIXED_MODEL_ENGINE
    if not os.path.exists(engine_path):
        raise FileNotFoundError(
            f'고정 모델 엔진을 찾을 수 없습니다: {engine_path}'
        )
    print(f'[DS] 고정 엔진 사용: {engine_path} (batch={batch_size})')
    lines = [
        '[property]',
        'gpu-id=0',
        'net-scale-factor=0.0039215697906911373',
        f'model-engine-file={engine_path}',
        f'labelfile-path={LABELS_FILE}',
        f'batch-size={batch_size}',
        'network-type=100',
        f'num-detected-classes={NUM_CLASSES}',
        'gie-unique-id=1',
        'process-mode=1',
        'output-tensor-meta=1',
        'output-blob-names=output0',
        'cluster-mode=4',
        f'infer-dims=3;{infer_h};{infer_w}',
        'maintain-aspect-ratio=1',
        'symmetric-padding=1',
        'model-color-format=1',
        f'interval={interval}',
    ]
    if _IS_JETSON:
        lines += ['scaling-compute-hw=2', 'scaling-filter=0']
    return _write_temp_config(lines, 'nvinfer_cfg_')


def _cleanup_cfg(path):
    if path and os.path.exists(path):
        try: os.unlink(path)
        except: pass


PEOPLENET_DIR      = os.path.join(BASE_DIR, 'people_net')
_PEOPLENET_CALIB   = os.path.join(PEOPLENET_DIR, 'resnet34_peoplenet_int8.txt')
_PEOPLENET_LABELS  = os.path.join(PEOPLENET_DIR, 'labels.txt')
_PEOPLENET_ENGINE = os.path.join(PEOPLENET_DIR, 'peoplenet_explicit_fp16.engine')
_PEOPLENET_AVAILABLE = False
_PN_INPUT_W = 960
_PN_INPUT_H = 544
_PN_GRID_W = 60
_PN_GRID_H = 34
_PN_CONF_THRESH = 0.4


def _make_peoplenet_cfg(batch_size: int = 1) -> str:
    """PeopleNet v2.6.3 nvinfer config 생성.

    EXPLICIT_BATCH로 미리 빌드된 엔진을 직접 사용.
    엔진이 없으면 people_net/build_engine.py 를 먼저 실행해야 한다.
    """
    if not os.path.exists(_PEOPLENET_ENGINE):
        raise FileNotFoundError(
            f'PeopleNet 엔진 없음: {_PEOPLENET_ENGINE}\n'
            f'  → people_net/build_engine.py 를 먼저 실행하세요.')

    print(f'[DS] PeopleNet 엔진 재사용: {os.path.basename(_PEOPLENET_ENGINE)}')

    lines = [
        '[property]',
        'gpu-id=0',
        'net-scale-factor=0.00392156862745098',
        'offsets=0.0;0.0;0.0',
        'model-color-format=0',     # 0=RGB (NGC 공식)
        f'model-engine-file={_PEOPLENET_ENGINE}',
        f'labelfile-path={_PEOPLENET_LABELS}',
        f'batch-size={batch_size}',
        'network-type=0',           # DetectNet_v2 내장 파서
        'num-detected-classes=3',
        'gie-unique-id=2',
        'process-mode=1',
        'interval=2',               # 3프레임마다 1회 추론 (GPU 부담 경감)
        'maintain-aspect-ratio=0',
        'input-object-min-width=4',
        'input-object-min-height=4',
        'output-blob-names=output_cov/Sigmoid:0;output_bbox/BiasAdd:0',
    ]
    # threshold는 DeepStream 7.x에서 [class-attrs-all] 섹션으로 설정
    lines += [
        '[class-attrs-all]',
        'pre-cluster-threshold=0.4',
        'nms-iou-threshold=0.5',
        'topk=20',
    ]
    return _write_temp_config(lines, 'peoplenet_cfg_')


# ═══════════════════════════════════════════════════════════════
#  Probe 1 — nvinfer src pad: 출력 텐서 → NvDsObjectMeta 생성
#  nvtracker가 이 ObjectMeta를 받아 track_id를 할당함
