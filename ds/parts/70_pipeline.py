# Auto-split from runtime.py: lines 2330-3097
def _build_single_pipeline():
    global _output_valve, _mjpeg_valve
    global _webrtc_bin, _webrtc_offer_sdp, _webrtc_ice_srv, _webrtc_gen
    n = len(_streams)
    if n == 0:
        return None, None

    ss0     = _streams[0]
    src_fps = max(1.0, ss0.get('video_fps', 30.0))
    max_src_fps = max(max(1.0, ss.get('video_fps', 30.0)) for ss in _streams)

    # mux 해상도: override > 스트림 최대 해상도 (다해상도 스트림 모두 수용)
    _ow = int(_st.get('mux_w_override', 0))
    _oh = int(_st.get('mux_h_override', 0))
    mux_w = _ow if _ow > 0 else max(ss.get('frame_w', 640) for ss in _streams)
    mux_h = _oh if _oh > 0 else max(ss.get('frame_h', 480) for ss in _streams)
    mux_w = max(1, mux_w); mux_h = max(1, mux_h)
    _st['mux_w'] = mux_w
    _st['mux_h'] = mux_h

    camera_input = any(ss.get('is_camera') for ss in _streams)

    # 소스 루프 방식 결정. 카메라는 v4l2src를 직접 사용한다.
    use_live_restream = _USE_LIVE_RESTREAM and not camera_input
    use_nvuri_loop = (not camera_input) and (not use_live_restream) and _USE_NVURI_FILE_LOOP and \
        Gst.ElementFactory.find('nvurisrcbin') is not None

    pipeline = Gst.Pipeline()

    def _make(name, alias):
        el = Gst.ElementFactory.make(name, alias)
        if el is None:
            print(f'[DS] !! 엘리먼트 생성 실패: {name} ({alias})')
        return el

    mux    = _make('nvstreammux',        'mux')
    infer  = _make('nvinfer',            'infer')
    nvosd  = _make('nvdsosd',            'nvosd')
    tiler  = _make('nvmultistreamtiler', 'tiler')
    q_post_track = _make('queue',        'q_post_track')
    q_post_tiler = _make('queue',        'q_post_tiler')
    q_post_osd   = _make('queue',        'q_post_osd')
    preview_tee  = _make('tee',          'preview_tee')
    mjpeg_valve  = _make('valve',        'mjpeg_fallback_valve')
    q_mjpeg      = _make('queue',        'q_mjpeg')
    mjpeg_conv   = _make('nvvideoconvert', 'mjpeg_conv')
    mjpeg_caps   = _make('capsfilter',    'mjpeg_caps')
    mjpeg_enc    = _make('jpegenc',       'mjpeg_enc')
    mjpeg_sink   = _make('appsink',       'mjpeg_sink')
    post_tee     = _make('tee',          'post_infer_tee')
    q_infer_sink = _make('queue',        'q_infer_sink')
    infer_sink   = _make('fakesink',     'infer_sink')
    q_output_gate = _make('queue',       'q_output_gate')
    output_valve = _make('valve',        'output_valve')

    # Preferred local display: H.264 → RTP → WebRTC. This is a leaky output
    # branch, not part of the inference/counting path. MJPEG remains available
    # as an automatic localhost fallback when ICE cannot connect offline.
    q_webrtc      = _make('queue',        'q_webrtc')
    webrtc_conv   = _make('nvvideoconvert', 'webrtc_conv')
    webrtc_caps   = _make('capsfilter',   'webrtc_caps')
    h264parse     = _make('h264parse',    'h264parse')
    pay_webrtc    = _make('rtph264pay',   'pay_webrtc')
    wb_capsf      = _make('capsfilter',   'wb_capsf')
    webrtcbin     = _make('webrtcbin',    'webrtcbin') if _WEBRTC_AVAILABLE else None
    h264enc = None
    for enc_factory in ('nvv4l2h264enc', 'x264enc', 'openh264enc'):
        h264enc = Gst.ElementFactory.make(enc_factory, 'h264enc')
        if h264enc is not None:
            print(f'[WebRTC] H.264 encoder: {enc_factory}', flush=True)
            break
    webrtc_enabled = all(el is not None for el in (
        q_webrtc, webrtc_conv, webrtc_caps, h264enc, h264parse,
        pay_webrtc, wb_capsf, webrtcbin))
    if not webrtc_enabled:
        print('[WebRTC] plugin/encoder unavailable → localhost MJPEG fallback', flush=True)
    tracker_profile = _get_tracker_profile(_st.get('tracker_key'))
    tracker_cfg = tracker_profile['ll_config_file']
    tracker_lib = tracker_profile['ll_lib_file']

    # PeopleNet 2차 추론기 (사용자 선택 + 엔진 존재 시 활성화)
    infer2 = None
    peoplenet_cfg = None
    _st['peoplenet_active'] = False
    _use_pn = bool(_st.get('use_peoplenet', True)) and os.path.exists(_PEOPLENET_ENGINE)
    if _use_pn:
        infer2 = _make('nvinfer', 'infer2')
        if infer2 is not None:
            try:
                peoplenet_cfg = _make_peoplenet_cfg(batch_size=n)
                _st['peoplenet_active'] = True
                print('[DS] PeopleNet(infer2) 활성화: class0(person) 탐지')
            except Exception as e:
                infer2 = None
                peoplenet_cfg = None
                _st['peoplenet_active'] = False
                print(f'[DS] PeopleNet config 실패 → 비활성, class0은 기본 모델 처리: {e}', flush=True)
        else:
            print('[DS] PeopleNet infer2 생성 실패 → 비활성')
    else:
        if not _st.get('use_peoplenet', True):
            print('[DS] PeopleNet 미사용 → class0 탐지는 기본 모델이 처리')
        else:
            print('[DS] PeopleNet 엔진 없음 → infer2 비활성')

    # nvtracker: 선택된 설정/라이브러리가 있으면 활성화
    use_tracker = os.path.exists(tracker_cfg) and os.path.exists(tracker_lib)
    tracker = None
    if use_tracker:
        tracker = _make('nvtracker', 'tracker')
        if tracker is None:
            use_tracker = False
    if not use_tracker:
        print(f'[DS] nvtracker 비활성 (tracker={tracker_profile["label"]} cfg={tracker_cfg} lib={tracker_lib})')
        print('[DS] 경고: nvtracker 없음 → object_id 미할당, ROI 카운팅 비활성')
        for ss in _streams:
            ss['roi_ready'] = False

    if any(el is None for el in [mux, infer, nvosd, tiler,
                                 q_post_track, q_post_tiler, q_post_osd,
                                 preview_tee, mjpeg_valve, q_mjpeg, mjpeg_conv,
                                 mjpeg_caps, mjpeg_enc, mjpeg_sink,
                                 post_tee, q_infer_sink, infer_sink,
                                 q_output_gate, output_valve]):
        return None, None

    _apply_nvvideoconvert_jetson_war(mjpeg_conv)
    if webrtc_enabled:
        _apply_nvvideoconvert_jetson_war(webrtc_conv)

    # Raw NVMM 큐는 작게 유지한다. Jetson의 SurfaceArray는 일반 가상 메모리가
    # 아니라 제한된 연속 CMA 영역을 사용하므로, 여러 큐가 각 8프레임을 잡으면
    # 출력이 잠깐 밀리는 것만으로도 새 VIC/nvinfer 표면 할당이 실패할 수 있다.
    # 3프레임(30 FPS에서 약 100ms)은 짧은 출력 지연을 흡수하면서
    # NVMM 보유량을 제한한다.
    for q in (q_infer_sink, q_post_track, q_post_tiler, q_post_osd,
              q_output_gate):
        _safe_set_property(q, 'max-size-buffers', 3)
        _safe_set_property(q, 'max-size-bytes', 0)
        _safe_set_property(q, 'max-size-time', 0)
        _safe_set_property(q, 'leaky', 2)
    _safe_set_property(infer_sink, 'sync', False)
    _safe_set_property(infer_sink, 'async', False)
    _safe_set_property(output_valve, 'drop-mode', 1)  # forward sticky events while frames are dropped
    _safe_set_property(output_valve, 'drop', not bool(_st.get('show_stream', True)))
    with _output_lock:
        _output_valve = output_valve
    _safe_set_property(mjpeg_valve, 'drop-mode', 1)
    with _mjpeg_clients_lock:
        _mjpeg_valve = mjpeg_valve
        mjpeg_has_client = _mjpeg_clients > 0
    _safe_set_property(mjpeg_valve, 'drop', not mjpeg_has_client)

    # ── nvstreammux ──
    mux.set_property('batch-size',           n)
    mux.set_property('width',                mux_w)
    mux.set_property('height',               mux_h)
    mux.set_property('batched-push-timeout', int(1_000_000 / max_src_fps))
    _safe_set_property(mux, 'enable-padding', True)
    _safe_set_property(mux, 'sync-inputs', 0)
    _safe_set_property(mux, 'frame-num-reset-on-eos', True)
    _safe_set_property(mux, 'frame-num-reset-on-stream-reset', True)
    # 항상 True: EOS 프로브(uridecodebin) 또는 nvurisrcbin(source seek) 모두
    # 소스별 EOS가 파이프라인 전체 EOS로 전파되지 않도록 차단.
    _safe_set_property(mux, 'drop-pipeline-eos', True)
    if camera_input or use_nvuri_loop or use_live_restream:
        # 카메라/live 재송출/loop 소스는 live-source/attach-sys-ts 적용
        _safe_set_property(mux, 'live-source', 1)
        _safe_set_property(mux, 'attach-sys-ts', True)
    _safe_set_property(mux, 'frame-duration', 0)
    if _IS_JETSON:
        # DeepStream 파일 입력과 동일하게 Surface Array 메모리를 명시.
        _safe_set_property(mux, 'nvbuf-memory-type', 4)

    # ── nvinfer ──
    # batch_size=n: 스트림 수만큼 한 번에 추론 (GPU 활용 극대화)
    # 엔진이 batch=1로만 빌드된 경우 TRT가 자동으로 순차 처리로 폴백
    _ii = max(0, int(_st.get('infer_interval', 0)))
    _iw = int(_st.get('infer_w', 640))
    _ih = int(_st.get('infer_h', 640))
    if _IS_JETSON:
        print('[DS] Jetson WAR: nvvideoconvert copy-hw=VIC, '
              'nvinfer scaling-compute-hw=VIC', flush=True)
    nvinfer_cfg = _make_nvinfer_cfg(
        FIXED_MODEL_NAME,
        interval=_ii, batch_size=n,
        infer_w=_iw, infer_h=_ih)
    infer.set_property('config-file-path', nvinfer_cfg)
    print(f'[DS] nvinfer: batch={n}, interval={_ii}, '
          f'infer={_iw}x{_ih}, 추론≈{src_fps/(1+_ii):.1f}fps')

    # ── nvtracker (있을 때만) ──
    if use_tracker:
        trk_w, trk_h = _compute_tracker_dims(mux_w, mux_h)
        tracker.set_property('ll-lib-file',    tracker_lib)
        tracker.set_property('ll-config-file', tracker_cfg)
        tracker.set_property('tracker-width',  trk_w)
        tracker.set_property('tracker-height', trk_h)
        tracker.set_property('gpu-id',         0)
        _safe_set_property(tracker, 'tracking-id-reset-mode', 1)
        print(f'[DS] tracker: {tracker_profile["label"]} / infer-space: {trk_w}x{trk_h}')

    # ── nvdsosd ──
    # Jetson(Tegra)에서 process-mode=1(GPU/EGL)은 cuGraphicsEGLRegisterImage 700
    # 오류로 CUDA 상태를 오염시켜 nvinfer/tracker까지 연쇄 충돌함 → CPU 모드(0) 사용
    nvosd.set_property('process-mode', 0)
    nvosd.set_property('display-text', 1)
    nvosd.set_property('display-bbox', 1)

    # ── nvmultistreamtiler ──
    rows, cols = _compute_grid_layout(n)
    tiler.set_property('rows',    rows)
    tiler.set_property('columns', cols)
    tiler.set_property('width',   1280)
    tiler.set_property('height',  720)
    _safe_set_property(tiler, 'buffer-pool-size', 4)

    if webrtc_enabled:
        enc_name = h264enc.get_factory().get_name()
        keyint = max(24, int(round(max_src_fps)) * 3)
        if enc_name == 'nvv4l2h264enc':
            webrtc_caps.set_property('caps', Gst.Caps.from_string(
                'video/x-raw(memory:NVMM),format=NV12'))
            _safe_set_property(h264enc, 'bitrate', 4_000_000)
            _safe_set_property(h264enc, 'iframeinterval', keyint)
            _safe_set_property(h264enc, 'idrinterval', keyint)
            _safe_set_property(h264enc, 'insert-sps-pps', True)
            _safe_set_property(h264enc, 'maxperf-enable', True)
            _safe_set_property(h264enc, 'preset-level', 1)
        elif enc_name == 'x264enc':
            webrtc_caps.set_property('caps', Gst.Caps.from_string(
                'video/x-raw,format=I420'))
            _safe_set_property(h264enc, 'bitrate', 4000)
            _safe_set_property(h264enc, 'speed-preset', 'ultrafast')
            _safe_set_property(h264enc, 'tune', 'zerolatency')
            _safe_set_property(h264enc, 'key-int-max', keyint)
            _safe_set_property(h264enc, 'bframes', 0)
            _safe_set_property(h264enc, 'threads', 2)
        else:
            webrtc_caps.set_property('caps', Gst.Caps.from_string(
                'video/x-raw,format=I420'))
            _safe_set_property(h264enc, 'bitrate', 4_000_000)

        _safe_set_property(h264parse, 'config-interval', -1)
        _safe_set_property(pay_webrtc, 'config-interval', 1)
        _safe_set_property(pay_webrtc, 'pt', 96)
        wb_capsf.set_property('caps', Gst.Caps.from_string(
            'application/x-rtp,media=video,encoding-name=H264,'
            'payload=96,clock-rate=90000'))
        _safe_set_property(webrtcbin, 'latency', 150)
        _safe_set_property(webrtcbin, 'bundle-policy', 3)
        stun_server = os.environ.get('WEBRTC_STUN_SERVER', '').strip()
        if stun_server:
            _safe_set_property(webrtcbin, 'stun-server', stun_server)

        # Two-frame bounded preview queue: one frame may be encoded while the
        # newest frame waits. Older frames are discarded instead of adding
        # latency or back-pressuring inference.
        _safe_set_property(q_webrtc, 'max-size-buffers', 2)
        _safe_set_property(q_webrtc, 'max-size-bytes', 0)
        _safe_set_property(q_webrtc, 'max-size-time', 0)
        _safe_set_property(q_webrtc, 'leaky', 2)

        def _on_negotiation_needed(wb):
            promise = Gst.Promise.new_with_change_func(
                _webrtc_on_offer_created, wb, None)
            wb.emit('create-offer', None, promise)

        def _on_ice_candidate(wb, mlineindex, candidate):
            with _webrtc_lock:
                if wb is not _webrtc_bin:
                    return
                _webrtc_ice_srv.append({
                    'sdpMLineIndex': mlineindex,
                    'candidate': candidate,
                })

        webrtcbin.connect('on-negotiation-needed', _on_negotiation_needed)
        webrtcbin.connect('on-ice-candidate', _on_ice_candidate)
        with _webrtc_lock:
            _webrtc_bin = webrtcbin
            _webrtc_offer_sdp = None
            _webrtc_ice_srv = []
            _webrtc_gen += 1
            _webrtc_session['id'] = None

    # Local MJPEG preview: leaky branch so a slow/closed UI can never
    # back-pressure inference.  It stays on 127.0.0.1 and therefore survives
    # Wi-Fi or ethernet link changes.
    try:
        local_preview_fps = int(os.environ.get('DS_CAMERA_PREVIEW_FPS', '30'))
    except ValueError:
        local_preview_fps = 30
    local_preview_fps = max(1, min(30, local_preview_fps))
    # Match the browser's front/back display buffers. The appsink still exposes
    # only the newest completed sample, so a slow UI cannot grow this queue.
    _safe_set_property(q_mjpeg, 'max-size-buffers', 2)
    _safe_set_property(q_mjpeg, 'max-size-bytes', 0)
    _safe_set_property(q_mjpeg, 'max-size-time', 0)
    _safe_set_property(q_mjpeg, 'leaky', 2)
    try:
        local_preview_width = max(640, min(1920, int(
            os.environ.get('DS_CAMERA_PREVIEW_WIDTH', '1280'))))
        local_preview_height = max(360, min(1080, int(
            os.environ.get('DS_CAMERA_PREVIEW_HEIGHT', '720'))))
        local_preview_quality = max(70, min(100, int(
            os.environ.get('DS_CAMERA_PREVIEW_QUALITY', '90'))))
    except ValueError:
        local_preview_width, local_preview_height, local_preview_quality = 1280, 720, 90
    mjpeg_caps.set_property('caps', Gst.Caps.from_string(
        f'video/x-raw,format=I420,width={local_preview_width},height={local_preview_height}'))
    _safe_set_property(mjpeg_enc, 'quality', local_preview_quality)
    # appsink의 new-sample Python callback은 GStreamer streaming thread에서
    # 실행된다. 파일 loop 경계까지 그 thread에 묶이지 않도록 별도 pull
    # worker가 sample을 읽는다.
    _safe_set_property(mjpeg_sink, 'emit-signals', False)
    _safe_set_property(mjpeg_sink, 'sync', False)
    _safe_set_property(mjpeg_sink, 'async', False)
    _safe_set_property(mjpeg_sink, 'max-buffers', 1)
    _safe_set_property(mjpeg_sink, 'drop', True)
    _safe_set_property(mjpeg_sink, 'wait-on-eos', False)

    print(f'[MJPEG] localhost preview {local_preview_fps} FPS', flush=True)

    # ── nvosd 앞 NV12→RGBA 변환기 ──
    # nvdsosd process-mode=0(CPU)은 RGBA 포맷만 지원.
    # tracker/mux 출력은 NV12 NVMM → nvvideoconvert + capsfilter로 강제 변환.
    osd_conv  = _make('nvvideoconvert', 'osd_conv')
    osd_caps  = _make('capsfilter',     'osd_caps')
    if osd_conv is None or osd_caps is None:
        print('[DS] !! osd_conv / osd_caps 생성 실패'); return None, None
    _apply_nvvideoconvert_jetson_war(osd_conv)
    osd_caps.set_property('caps',
        Gst.Caps.from_string('video/x-raw(memory:NVMM),format=RGBA'))

    # ── infer2(PeopleNet) 설정 ──
    if infer2 is not None:
        infer2.set_property('config-file-path', peoplenet_cfg)

    # 파이프라인에 엘리먼트 추가
    elements = [mux, infer, post_tee, q_infer_sink, infer_sink,
                q_output_gate, output_valve, q_post_track, tiler, q_post_tiler,
                osd_conv, osd_caps, nvosd, q_post_osd, preview_tee,
                mjpeg_valve, q_mjpeg, mjpeg_conv, mjpeg_caps,
                mjpeg_enc, mjpeg_sink,
                ]
    if webrtc_enabled:
        elements.extend([
            q_webrtc, webrtc_conv, webrtc_caps, h264enc, h264parse,
            pay_webrtc, wb_capsf, webrtcbin,
        ])
    if infer2 is not None:
        elements.insert(2, infer2)   # mux → infer → infer2 → ...
    if use_tracker:
        idx = elements.index(infer2) + 1 if infer2 is not None else 2
        elements.insert(idx, tracker)
    for el in elements:
        pipeline.add(el)

    # ── 메인 체인 (mux → ... → tee) ──
    chain = [mux, infer]
    if infer2 is not None:
        chain.append(infer2)
    if use_tracker:
        chain.append(tracker)
    chain += [post_tee]
    for i in range(len(chain) - 1):
        if not chain[i].link(chain[i+1]):
            print(f'[DS] !! link 실패: {chain[i].get_name()} → {chain[i+1].get_name()}')
            return None, None

    def _req_pad(el):
        """tee request pad — request_pad_simple 우선, 구버전은 get_request_pad 폴백."""
        try:
            return el.request_pad_simple('src_%u')
        except AttributeError:
            return el.get_request_pad('src_%u')

    # Inference branch: always consumes frames so detection/counting continues
    # even when the local preview branch is disabled.
    pad_sink = _req_pad(post_tee)
    if pad_sink is None or pad_sink.link(q_infer_sink.get_static_pad('sink')) != Gst.PadLinkReturn.OK:
        print('[DS] !! post_infer_tee → q_infer_sink link 실패'); return None, None
    if not q_infer_sink.link(infer_sink):
        print('[DS] !! q_infer_sink → infer_sink link 실패'); return None, None

    # Output branch: can be toggled at runtime via output_valve.drop.
    pad_out = _req_pad(post_tee)
    if pad_out is None or pad_out.link(q_output_gate.get_static_pad('sink')) != Gst.PadLinkReturn.OK:
        print('[DS] !! post_infer_tee → q_output_gate link 실패'); return None, None
    if not q_output_gate.link(output_valve):
        print('[DS] !! q_output_gate → output_valve link 실패'); return None, None
    if not output_valve.link(q_post_track):
        print('[DS] !! output_valve → q_post_track link 실패'); return None, None

    output_chain = [q_post_track, tiler, q_post_tiler,
                    osd_conv, osd_caps, nvosd, q_post_osd, preview_tee]
    for i in range(len(output_chain) - 1):
        if not output_chain[i].link(output_chain[i+1]):
            print(f'[DS] !! link 실패: {output_chain[i].get_name()} → {output_chain[i+1].get_name()}')
            return None, None

    if webrtc_enabled:
        pad_webrtc = _req_pad(preview_tee)
        if (pad_webrtc is None or
                pad_webrtc.link(q_webrtc.get_static_pad('sink')) != Gst.PadLinkReturn.OK):
            print('[WebRTC] preview_tee → q_webrtc link failed', flush=True)
            return None, None
        webrtc_chain = [
            q_webrtc, webrtc_conv, webrtc_caps, h264enc,
            h264parse, pay_webrtc, wb_capsf,
        ]
        for i in range(len(webrtc_chain) - 1):
            if not webrtc_chain[i].link(webrtc_chain[i + 1]):
                print(f'[WebRTC] link failed: '
                      f'{webrtc_chain[i].get_name()} → '
                      f'{webrtc_chain[i + 1].get_name()}', flush=True)
                return None, None
        try:
            wb_sink = webrtcbin.request_pad_simple('sink_%u')
        except AttributeError:
            wb_sink = webrtcbin.get_request_pad('sink_%u')
        if (wb_sink is None or
                wb_capsf.get_static_pad('src').link(wb_sink) != Gst.PadLinkReturn.OK):
            print('[WebRTC] RTP → webrtcbin link failed', flush=True)
            return None, None

    # Localhost MJPEG branch used only when the on-device WebRTC connection
    # fails. mjpeg_fallback_valve prevents simultaneous JPEG encoding during
    # normal WebRTC playback.
    pad_mjpeg = _req_pad(preview_tee)
    if (pad_mjpeg is None or
            pad_mjpeg.link(mjpeg_valve.get_static_pad('sink')) != Gst.PadLinkReturn.OK):
        print('[DS] !! preview_tee → mjpeg_fallback_valve link 실패'); return None, None
    mjpeg_chain = [mjpeg_valve, q_mjpeg, mjpeg_conv, mjpeg_caps,
                   mjpeg_enc, mjpeg_sink]
    for i in range(len(mjpeg_chain) - 1):
        if not mjpeg_chain[i].link(mjpeg_chain[i+1]):
            print(f'[DS] !! link 실패: {mjpeg_chain[i].get_name()} → {mjpeg_chain[i+1].get_name()}')
            return None, None

    def _pull_mjpeg_samples(sink):
        global _preview_jpeg, _preview_sequence
        while _st.get('running', False):
            with _preview_sink_lock:
                if _preview_sink is not sink:
                    break
            sample = sink.emit('try-pull-sample', 250 * Gst.MSECOND)
            if sample is None:
                continue
            buffer = sample.get_buffer()
            ok, mapped = buffer.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                jpeg = bytes(mapped.data)
            finally:
                buffer.unmap(mapped)
            with _preview_condition:
                _preview_jpeg = jpeg
                _preview_sequence += 1
                _preview_condition.notify_all()

    global _preview_sink, _preview_pull_thread
    with _preview_sink_lock:
        _preview_sink = mjpeg_sink
        _preview_pull_thread = threading.Thread(
            target=_pull_mjpeg_samples,
            args=(mjpeg_sink,),
            name='mjpeg-pull',
            daemon=True,
        )
        _preview_pull_thread.start()

    out_mode = 'local WebRTC + MJPEG fallback' if webrtc_enabled else 'localhost MJPEG'
    print(f'[DS] 체인: {" → ".join(e.get_name() for e in chain)} → [{out_mode}]')

    # ── 소스 추가 ──
    if camera_input:
        source_mode = 'v4l2src(camera)'
    elif use_live_restream:
        source_mode = 'udp-live-restream(gapless-playbin)'
    else:
        source_mode = 'nvurisrcbin(file-loop)' if use_nvuri_loop else 'uridecodebin(rebuild-on-eos)'
    print(f'[DS] source mode: {source_mode}')
    _pad_link_lock = threading.Lock()   # request_pad + link 은 비동기 콜백에서 동시에 호출

    for si, ss in enumerate(_streams):
        src_pre_conv  = _make('nvvideoconvert', f'src_pre_conv_{si}')
        src_pre_caps  = _make('capsfilter',     f'src_pre_caps_{si}')
        src_rate      = _make('videorate',      f'src_rate_{si}')
        src_rate_caps = _make('capsfilter',     f'src_rate_caps_{si}')
        src_conv      = _make('nvvideoconvert', f'src_conv_{si}')
        src_caps      = _make('capsfilter',     f'src_caps_{si}')
        src_sync      = _make('identity',       f'src_sync_{si}')
        if not src_pre_conv or not src_pre_caps or not src_rate or not src_rate_caps or not src_conv or not src_caps or not src_sync:
            return None, None
        _apply_nvvideoconvert_jetson_war(src_pre_conv)
        _apply_nvvideoconvert_jetson_war(src_conv)
        # framerate를 강제하지 않음 → nvurisrcbin 출력 fps를 그대로 수용
        # videorate drop-only는 max-rate 초과 프레임만 버림; caps에 framerate를 명시하면
        # 29.947fps 영상에서 exactly-30/1 요구로 not-negotiated 에러 발생 → format만 지정
        src_pre_caps.set_property('caps', Gst.Caps.from_string('video/x-raw,format=NV12'))
        _safe_set_property(src_rate, 'drop-only', True)
        _safe_set_property(src_rate, 'max-rate', int(_MAX_STREAM_FPS))
        src_rate_caps.set_property('caps', Gst.Caps.from_string('video/x-raw,format=NV12'))
        src_caps.set_property('caps', Gst.Caps.from_string('video/x-raw(memory:NVMM),format=NV12'))
        # live restream도 타임스탬프 기준으로 pace를 맞춰야 burst 전송/빨라졌다가
        # 정상속도로 돌아오는 현상을 줄일 수 있다.
        _safe_set_property(src_sync, 'sync', True)
        is_camera = bool(ss.get('is_camera'))
        # APACK과 동일하게 파일 입력 PTS를 단조 증가 타임라인으로 변환한다.
        _safe_set_property(src_sync, 'single-segment', (not use_live_restream) and (not is_camera))
        if _IS_JETSON:
            _safe_set_property(src_pre_conv, 'nvbuf-memory-type', 4)
            _safe_set_property(src_conv, 'nvbuf-memory-type', 4)

        if is_camera:
            src_bin = _create_camera_source_bin(
                si,
                ss.get('camera_device') or CAMERA_DEVICE,
                ss.get('frame_w') or CAMERA_WIDTH,
                ss.get('frame_h') or CAMERA_HEIGHT,
                int(ss.get('video_fps') or CAMERA_FPS))
            uri_decode_bin = None
        else:
            src_path = ss.get('source_uri') or ss['video_path']
            src_bin, _, uri_decode_bin = _create_source_bin(si, src_path,
                                                            enable_loop=use_nvuri_loop)
        if src_bin is None:
            return None, None
        ss['_uri_decode_bin'] = uri_decode_bin
        pipeline.add(src_bin)
        pipeline.add(src_pre_conv); pipeline.add(src_pre_caps)
        pipeline.add(src_rate); pipeline.add(src_rate_caps)
        pipeline.add(src_conv); pipeline.add(src_caps); pipeline.add(src_sync)

        src_pad = src_bin.get_static_pad('src')
        sink_pad = src_pre_conv.get_static_pad('sink')
        if src_pad is None or sink_pad is None:
            print(f'[DS] !! src bin pad 획득 실패: stream {si}')
            return None, None

        # nvurisrcbin은 자체 file-loop를 사용한다. EOS 프로브는 네이티브
        # file-loop가 없는 uridecodebin fallback에만 적용한다.
        if ((not is_camera) and (not use_nvuri_loop)
                and (not use_live_restream) and uri_decode_bin is not None):
            src_pad.add_probe(
                Gst.PadProbeType.EVENT_DOWNSTREAM,
                _make_loop_probe(uri_decode_bin, si), None)

        if src_pad.link(sink_pad) != Gst.PadLinkReturn.OK:
            print(f'[DS] !! source_bin → src_pre_conv link 실패: stream {si}')
            return None, None

        if not src_pre_conv.link(src_pre_caps):
            print(f'[DS] !! link 실패: src_pre_conv_{si} → src_pre_caps_{si}')
            return None, None
        if not src_pre_caps.link(src_rate):
            print(f'[DS] !! link 실패: src_pre_caps_{si} → src_rate_{si}')
            return None, None
        if not src_rate.link(src_rate_caps):
            print(f'[DS] !! link 실패: src_rate_{si} → src_rate_caps_{si}')
            return None, None
        if not src_rate_caps.link(src_conv):
            print(f'[DS] !! link 실패: src_rate_caps_{si} → src_conv_{si}')
            return None, None
        if not src_conv.link(src_caps):
            print(f'[DS] !! link 실패: src_conv_{si} → src_caps_{si}')
            return None, None
        if not src_caps.link(src_sync):
            print(f'[DS] !! link 실패: src_caps_{si} → src_sync_{si}')
            return None, None

        with _pad_link_lock:
            try:
                mux_sink = mux.request_pad_simple(f'sink_{si}')
            except AttributeError:
                mux_sink = mux.get_request_pad(f'sink_{si}')
            if mux_sink is None:
                print(f'[DS] !! mux sink pad 요청 실패: sink_{si}')
                return None, None
            rc = src_sync.get_static_pad('src').link(mux_sink)
            if rc != Gst.PadLinkReturn.OK:
                print(f'[DS] !! src_sync_{si} → mux link 실패: {rc}')
                return None, None

    # ── pad probes ──
    infer.get_static_pad('src').add_probe(
        Gst.PadProbeType.BUFFER, _infer_tensor_probe, 0)
    # PeopleNet: DeepStream DetectNet parser가 만든 ObjectMeta 중 person만 유지
    if infer2 is not None:
        infer2.get_static_pad('src').add_probe(
            Gst.PadProbeType.BUFFER, _peoplenet_filter_probe, 0)
    # tracker 있으면 track_id 할당 뒤, 없으면 infer 출력에서 카운팅한다.
    if use_tracker:
        tracker.get_static_pad('src').add_probe(
            Gst.PadProbeType.BUFFER, _osd_counting_probe, 0)
    else:
        infer.get_static_pad('src').add_probe(
            Gst.PadProbeType.BUFFER, _osd_counting_probe, 0)

    # ── 체인 로그 ──
    chain_names = ['mux', f'infer({FIXED_MODEL_NAME})']
    if infer2 is not None:
        chain_names.append('infer2(peoplenet)')
    if use_tracker:
        chain_names.append('tracker')
    chain_names += ['post_infer_tee', '[infer_sink + output_valve]',
                    'q_post_track', 'tiler', 'q_post_tiler', 'osd_conv', 'osd_caps',
                    'nvosd', 'q_post_osd', 'preview_tee',
                    '[WebRTC preferred + localhost MJPEG fallback]']
    print(f'[DS] 체인: {" → ".join(chain_names)}', flush=True)

    return pipeline, (nvinfer_cfg, peoplenet_cfg)


# ═══════════════════════════════════════════════════════════════
#  단일 추론 루프 (스레드 하나로 전체 파이프라인 구동)
# ═══════════════════════════════════════════════════════════════
def _drain_bus_errors(bus, label='DS'):
    """버스 에러/경고 메시지를 즉시 출력 (진단용)."""
    msg = bus.pop_filtered(
        Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.INFO)
    while msg is not None:
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f'[{label}] 버스 ERROR: {err} | {dbg}', flush=True)
        elif msg.type == Gst.MessageType.WARNING:
            err, dbg = msg.parse_warning()
            print(f'[{label}] 버스 WARNING: {err} | {dbg}', flush=True)
        elif msg.type == Gst.MessageType.INFO:
            err, dbg = msg.parse_info()
            print(f'[{label}] 버스 INFO: {err}', flush=True)
        msg = bus.pop_filtered(
            Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.INFO)


def _infer_loop_single():
    global _infer_dims_logged, _osd_probe_log_count, _output_valve, _mjpeg_valve
    global _webrtc_bin, _webrtc_offer_sdp, _webrtc_ice_srv

    def _clear_pipeline_global_refs():
        """Drop module-level references into a retired GStreamer graph."""
        global _output_valve, _mjpeg_valve, _preview_sink, _preview_pull_thread
        global _webrtc_bin, _webrtc_offer_sdp, _webrtc_ice_srv
        with _output_lock:
            _output_valve = None
        with _mjpeg_clients_lock:
            _mjpeg_valve = None
        with _webrtc_lock:
            _webrtc_bin = None
            _webrtc_offer_sdp = None
            _webrtc_ice_srv = []
            _webrtc_session['id'] = None
        with _preview_sink_lock:
            _preview_sink = None
            pull_thread = _preview_pull_thread
            _preview_pull_thread = None
        if pull_thread is not None and pull_thread is not threading.current_thread():
            pull_thread.join(timeout=1.0)

    def _reset_runtime(reset_loop_count: bool):
        _st['gpu_stat'] = _acc_empty(maxlen=60)
        _st['cpu_stat'] = _acc_empty(maxlen=60)
        for ss in _streams:
            if reset_loop_count:
                ss['loop_count'] = 1
                ss['latest_loop_result'] = None
                ss['latest_loop_completed_at'] = ''
                ss['_total_elapsed_sec'] = 0.0
                _reset_stream_interval_counters(ss, reset_tracking=False)
            else:
                # 안전망: EOS 프로브가 _finalize_stream_cycle을 호출 못한 경우
                # → 현재 카운트/로그를 스냅샷으로 보존하되, 10분 집계값은 유지
                # frame-count/EOS 경로가 이미 사이클을 확정한 경우에는
                # loop_count와 스냅샷을 다시 증가시키지 않는다.
                if ss.get('counts') and not ss.get('_eos_seen', False):
                    played = _stream_played_seconds(ss)
                    ss['_total_elapsed_sec'] = ss.get('_total_elapsed_sec', 0.0) + played
                    _snapshot_loop_result(ss)
                    ss['loop_count'] = int(ss.get('loop_count', 1)) + 1
            ss['frame_idx'] = 0
            _reset_stream_tracking_state(ss, reset_frame_idx=False)
            ss['last_infer_dets'] = 0
            ss['last_model_added'] = 0
            ss['last_osd_objs'] = 0
            ss['tensor_seen'] = False
            ss['_eos_seen'] = False
            ss['_probe_frame_times'].clear()
            ss['_probe_delivery_times'].clear()
            ss['total_fps'] = 0.0
            ss['infer_fps'] = 0.0
            ss['fps_stat'] = _acc_empty()
            ss['infer_stat'] = _acc_empty()
            ss['_last_frame_num'] = -1
            ss['_last_buf_pts'] = -1
            ss['_first_buf_pts'] = -1
            ss['_tracker_reset_requested'] = False
            ss['_tracker_max_id_seen'] = -1
            ss['_tracker_empty_since'] = 0.0
            ss['_tracker_reset_last_attempt_ts'] = 0.0
            ss['_last_frame_time_sec'] = 0.0
            ss['_last_loop_transition_ts'] = 0.0
            ss['_last_progress_ts'] = time.perf_counter()

    first_run = True
    pipeline_started_once = False

    def _exit_after_rebuild_failure(reason):
        """Let systemd recreate CUDA/NVMM state after an in-process rebuild fails."""
        if not pipeline_started_once or not _st.get('running'):
            return
        print(
            f'[DS] 자동 재빌드 실패 ({reason}) → inferd 프로세스 재기동 요청',
            flush=True,
        )
        # This loop runs in a worker thread.  Raising SystemExit would only stop
        # that thread, leaving /status alive with running=false.  A process exit
        # is intentional here: the systemd unit uses Restart=always and startup
        # restoration preserves the operator's desired_running state.
        os._exit(1)

    # ── 외부 루프: EOS 수신 시 파이프라인 재빌드로 무한반복 ──────
    while _st['running']:
        _st['restart_requested'] = False
        _st['restart_reason'] = ''
        _osd_probe_log_count = 0
        _infer_dims_logged = False
        _reset_runtime(reset_loop_count=first_run)
        first_run = False

        pipeline, _cfgs = _build_single_pipeline()
        nvinfer_cfg, peoplenet_cfg = _cfgs if _cfgs else (None, None)
        if pipeline is None:
            print('[DS] 파이프라인 빌드 실패')
            _clear_pipeline_global_refs()
            _cleanup_cfg(nvinfer_cfg); _cleanup_cfg(peoplenet_cfg)
            _exit_after_rebuild_failure('pipeline build')
            break

        bus = pipeline.get_bus()
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print('[DS] PLAYING 전환 실패')
            _drain_bus_errors(bus)
            pipeline.set_state(Gst.State.NULL)
            _clear_pipeline_global_refs()
            _cleanup_cfg(nvinfer_cfg); _cleanup_cfg(peoplenet_cfg)
            _exit_after_rebuild_failure('PLAYING transition')
            break

        print(f'[DS] set_state(PLAYING) 반환: {ret} — get_state 대기 중…', flush=True)

        total_wait = 0
        MAX_WAIT = 600
        state_ok = False
        while total_wait < MAX_WAIT:
            sc, _, _ = pipeline.get_state(30 * Gst.SECOND)
            total_wait += 30
            _drain_bus_errors(bus)
            if sc == Gst.StateChangeReturn.SUCCESS:
                state_ok = True
                break
            if sc == Gst.StateChangeReturn.FAILURE:
                print(f'[DS] 상태 전환 FAILURE ({total_wait}s)')
                break
            print(f'[DS] ASYNC 대기 중… ({total_wait}s)', flush=True)
            if not _st['running']:
                break

        if not state_ok:
            print('[DS] 파이프라인 기동 실패')
            _drain_bus_errors(bus)
            pipeline.set_state(Gst.State.NULL)
            _clear_pipeline_global_refs()
            _cleanup_cfg(nvinfer_cfg); _cleanup_cfg(peoplenet_cfg)
            _exit_after_rebuild_failure('pipeline startup')
            break

        pipeline_started_once = True
        _infer_accs = [_acc_empty() for _ in _streams]
        _total_accs = [_acc_empty() for _ in _streams]
        stat_tick = time.perf_counter()
        print('[DS] 파이프라인 시작 (local WebRTC, MJPEG fallback)', flush=True)

        eos_received = False
        restart_triggered = False
        fatal_error = False

        while _st['running']:
            msg = bus.timed_pop_filtered(
                100_000_000,
                Gst.MessageType.EOS | Gst.MessageType.ERROR | Gst.MessageType.WARNING)
            if msg:
                if msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    err_str = str(err).lower()
                    print(f'[DS] ERROR: {err} | {dbg}', flush=True)
                    # not-negotiated: 소스 내부 caps 협상 실패 → 파이프라인 재빌드로 복구 시도
                    if 'not-negotiated' in err_str or 'not negotiated' in err_str:
                        print('[DS] not-negotiated 에러 → 재시작 시도', flush=True)
                        restart_triggered = True
                        _st['pipeline_gen'] += 1
                    else:
                        fatal_error = True
                    break
                if msg.type == Gst.MessageType.WARNING:
                    err, dbg = msg.parse_warning()
                    print(f'[DS] WARNING: {err}', flush=True)
                elif msg.type == Gst.MessageType.EOS:
                    if _USE_NVURI_FILE_LOOP:
                        # nvurisrcbin(file-loop=1)의 내부 루프 경계 신호다.
                        # 소스가 스스로 이어서 재생하므로 파이프라인은 건드리지 않는다.
                        print('[DS] EOS 수신 (nvurisrcbin 루프 경계) → 무시', flush=True)
                    else:
                        print('[DS] EOS 수신 → 무한반복 재시작', flush=True)
                        eos_received = True
                        for ss in _streams:
                            with ss['_lock']:
                                _snapshot_loop_result(ss)
                                ss['loop_count'] = ss.get('loop_count', 1) + 1
                                ss['frame_idx'] = 0
                                _reset_stream_tracking_state(ss, reset_frame_idx=False)
                        _st['pipeline_gen'] += 1
                        break

            if _st.get('restart_requested'):
                restart_triggered = True
                print(f'[DS] 재시작 요청 감지 → 파이프라인 재빌드 ({_st.get("restart_reason","unknown")})', flush=True)
                _st['pipeline_gen'] += 1
                break

            now = time.perf_counter()
            # 카메라 ID 임계값에서만 NvSORT 내부 ID 발급기를 초기화한다.
            # 파이프라인/모델은 유지되고 source_id별 tracker 상태만
            # NVIDIA STREAM_RESET 이벤트로 비워진다.
            mux_for_reset = pipeline.get_by_name('mux')
            for si, ss in enumerate(_streams):
                with ss['_lock']:
                    tracker_reset = bool(ss.get('_tracker_reset_requested', False))
                    last_reset_attempt = float(
                        ss.get('_tracker_reset_last_attempt_ts', 0.0) or 0.0)
                if (tracker_reset and mux_for_reset is not None
                        and (now - last_reset_attempt) >= 2.0):
                    with ss['_lock']:
                        ss['_tracker_reset_last_attempt_ts'] = now
                    reset_ok = bool(pyds.gst_element_send_nvevent_new_stream_reset(
                        hash(mux_for_reset), si))
                    if reset_ok:
                        with ss['_lock']:
                            ss['_tracker_reset_requested'] = False
                            ss['_tracker_max_id_seen'] = -1
                            ss['_tracker_empty_since'] = 0.0
                            ss['_tracker_reset_last_attempt_ts'] = 0.0
                            # 누적 통계는 유지하고 이전 ID의 위치/속도/카운팅
                            # 이력만 NvSORT reset과 같은 시점에 제거한다.
                            _reset_stream_tracking_state(ss, reset_frame_idx=False)
                    print(
                        f'[DS] {ss.get("video_name","stream")} NvSORT ID 초기화 '
                        f'{"완료" if reset_ok else "실패"}',
                        flush=True,
                    )
            for ss in _streams:
                if ss.get('_eos_seen', False):
                    continue
                if ss.get('_uri_decode_bin') is None:
                    continue
                last_progress_ts = float(ss.get('_last_progress_ts', now) or now)
                last_transition_ts = float(ss.get('_last_loop_transition_ts', 0.0) or 0.0)
                # 네이티브 file-loop가 실제로 멈춘 예외 상황만 APACK과 같은
                # watchdog 복구 경로로 넘긴다. 정상 루프에서는 실행되지 않는다.
                had_progress = int(ss.get('frame_idx', 0)) > 0 or ss.get('latest_loop_result') is not None
                if had_progress and (now - last_progress_ts) >= 8.0 and (now - last_transition_ts) >= 3.0:
                    if not _st.get('restart_requested'):
                        _st['restart_requested'] = True
                        _st['restart_reason'] = f'watchdog_stall:{ss.get("video_name","stream")}'
                        print(
                            f'[DS] {ss.get("video_name","stream")} 정지 감지 '
                            '→ 파이프라인 복구 예약', flush=True)

            if now - stat_tick >= 1.0:
                stat_tick = now
                for i, ss in enumerate(_streams):
                    pts = list(ss['_probe_frame_times'])
                    if len(pts) > 1:
                        dt = pts[-1] - pts[0]
                        iv = (len(pts)-1)/dt if dt > 0 else 0.0
                        iv = min(_MAX_STREAM_FPS, iv)
                        ss['infer_fps'] = iv
                        if iv > 0:
                            _acc_update(_infer_accs[i], iv)
                        ss['infer_stat'] = _acc_stat(_infer_accs[i])
                    dpts = list(ss['_probe_delivery_times'])
                    if len(dpts) > 1:
                        ddt = dpts[-1] - dpts[0]
                        tv = (len(dpts)-1)/ddt if ddt > 0 else 0.0
                        ss['total_fps'] = tv
                        if tv > 0:
                            _acc_update(_total_accs[i], tv)
                        ss['fps_stat'] = _acc_stat(_total_accs[i])
                gpu = _read_gpu_pct()
                cpu = _read_cpu_pct()
                _st['gpu_pct'] = gpu
                _st['cpu_pct'] = cpu
                if gpu >= 0:
                    _acc_update(_st['gpu_stat'], gpu)
                if cpu >= 0:
                    _acc_update(_st['cpu_stat'], cpu)

        # ── 파이프라인 종료: PLAYING → PAUSED → NULL 순서로 단계적 전환 ──
        # 즉시 NULL로 전환하면 nvtracker CUDA 스레드가 아직 살아있는 상태에서
        # CUDA 컨텍스트가 파괴되어 "GPUassert: driver shutting down" 에러 발생.
        # PAUSED 경유로 데이터 흐름을 멈추고 엘리먼트들이 플러시를 끝낼 시간을 줌.
        print('[DS] 파이프라인 종료 중 (PAUSED → NULL)…', flush=True)
        pipeline.set_state(Gst.State.PAUSED)
        pipeline.get_state(3 * Gst.SECOND)
        time.sleep(0.4)          # nvtracker CUDA 스레드 플러시 대기
        pipeline.set_state(Gst.State.NULL)
        pipeline.get_state(8 * Gst.SECOND)
        _clear_pipeline_global_refs()
        _cleanup_cfg(nvinfer_cfg); _cleanup_cfg(peoplenet_cfg)
        for ss in _streams:
            ss['_uri_decode_bin'] = None
        # GStreamer / CUDA 객체 즉시 해제: Python GC가 비결정론적으로 동작하므로
        # 명시적으로 참조 제거 후 gc.collect()로 파이썬 힙 정리
        del pipeline, nvinfer_cfg, peoplenet_cfg, bus
        gc.collect()

        if fatal_error and _st.get('running'):
            _exit_after_rebuild_failure('fatal pipeline error')
        if fatal_error or not (eos_received or restart_triggered) or not _st['running']:
            break
        # EOS fallback: EOS 프로브 실패로 파이프라인 EOS 발생 → 재빌드
        rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
        print(f'[DS] 재빌드 중… (RSS {rss_mb:.0f} MB, reason={_st.get("restart_reason") or "eos"})', flush=True)
        time.sleep(1.0)          # 이전 CUDA 컨텍스트 완전 정리 대기

    _st['running'] = False
    print('[DS] 파이프라인 종료', flush=True)


# ═══════════════════════════════════════════════════════════════
#  Flask 라우트
