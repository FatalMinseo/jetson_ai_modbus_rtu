# Auto-split from runtime.py: lines 1724-2328
def _infer_tensor_probe(pad, info, u_data):
    global _infer_dims_logged
    buf = info.get_buffer()
    if buf is None:
        return Gst.PadProbeReturn.OK
    try:
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buf))
        if batch_meta is None:
            return Gst.PadProbeReturn.OK

        mux_w           = _st.get('mux_w', 640)
        mux_h           = _st.get('mux_h', 480)
        conf_thresh_def = _st['conf']   # 전체 기본값
        # _st에서 추론 해상도 읽기 (모델별 커스텀 크기 지원)
        infer_w = int(_st.get('infer_w', 640))
        infer_h = int(_st.get('infer_h', 640))
        # letterbox 역변환: nvinfer가 mux 프레임을 infer_w×infer_h로 letterbox할 때
        # 생기는 패딩 오프셋을 빼야 올바른 mux 좌표가 나옴
        _lb       = min(infer_w / max(1, mux_w), infer_h / max(1, mux_h))
        _pad_left = (infer_w - mux_w * _lb) / 2.0
        _pad_top  = (infer_h - mux_h * _lb) / 2.0

        l_frame = batch_meta.frame_meta_list
        while l_frame is not None:
            try:
                frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            except StopIteration:
                break

            si = int(frame_meta.source_id)
            ss = _streams[si] if 0 <= si < len(_streams) else None
            if ss is not None:
                ss['tensor_seen'] = True

            l_user = frame_meta.frame_user_meta_list
            while l_user is not None:
                try:
                    user_meta = pyds.NvDsUserMeta.cast(l_user.data)
                except StopIteration:
                    break

                if user_meta.base_meta.meta_type == \
                        pyds.NvDsMetaType.NVDSINFER_TENSOR_OUTPUT_META:
                    tensor_meta = pyds.NvDsInferTensorMeta.cast(
                        user_meta.user_meta_data)
                    for li in range(tensor_meta.num_output_layers):
                        layer = pyds.get_nvds_LayerInfo(tensor_meta, li)
                        if layer.layerName != 'output0':
                            continue
                        infer_dims = layer.inferDims
                        size = int(getattr(infer_dims, 'numElements', 0))
                        if size <= 0:
                            dims = layer.dims
                            for di in range(dims.numDims):
                                size = max(1, size) * int(dims.d[di])
                        if not _infer_dims_logged:
                            try:
                                dd = [int(infer_dims.d[i]) for i in range(int(infer_dims.numDims))]
                            except Exception:
                                dd = []
                            print(f'[DS infer] output0 inferDims={dd} numElements={size}', flush=True)
                            _infer_dims_logged = True
                        # host buffer를 명시적으로 복사한다. DeepStream이 프레임별 오프셋된
                        # out_buf_ptrs_host를 layer.buffer로 연결해 준다.
                        ptr_addr = pyds.get_ptr(layer.buffer)
                        arr = np.ctypeslib.as_array(
                            (ctypes.c_float * size).from_address(ptr_addr)
                        ).copy()

                        if ss is not None:
                            ss['_probe_frame_times'].append(time.perf_counter())

                        # YOLOv8 후처리: infer 공간(letterboxed) 좌표로 유지
                        # → 아래 letterbox 역변환으로 mux 좌표로 전환
                        # 채널별 conf 사용 (없으면 전체 기본값)
                        stream_conf = ss.get('conf', conf_thresh_def) if ss is not None else conf_thresh_def
                        dets = yolo_postprocess(
                            arr, NUM_CLASSES, infer_w, infer_h, stream_conf,
                            infer_dims=infer_dims)
                        if ss is not None:
                            ss['last_infer_dets'] = len(dets)

                        added_count = 0
                        for (x1, y1, x2, y2, conf, cls_id) in dets:
                            # traffic과 PeopleNet의 person 후보를 모두 유지한다.
                            # infer2 뒤의 fusion probe가 겹치는 교차 모델 박스만
                            # confidence 기준으로 하나로 줄인다.
                            # 640 → mux 좌표 역변환 (패딩 오프셋 제거 후 스케일)
                            x1m = max(0.0, (x1 - _pad_left) / _lb)
                            y1m = max(0.0, (y1 - _pad_top)  / _lb)
                            x2m = min(float(mux_w), (x2 - _pad_left) / _lb)
                            y2m = min(float(mux_h), (y2 - _pad_top)  / _lb)
                            if (x2m - x1m) < 1 or (y2m - y1m) < 1:
                                continue

                            obj_meta = pyds.nvds_acquire_obj_meta_from_pool(batch_meta)
                            obj_meta.unique_component_id = 1
                            obj_meta.class_id  = int(cls_id)
                            obj_meta.confidence = float(conf)
                            obj_meta.object_id  = _UNTRACKED_OBJ_ID

                            rect = obj_meta.rect_params
                            _set_rect_bbox(rect, x1m, y1m, x2m, y2m, cls_id)

                            # tracker가 downstream에서 detector bbox를 참조할 수 있게
                            # 원본 detector 좌표를 별도 필드에도 채운다.
                            _store_detector_bbox(obj_meta, x1m, y1m, x2m, y2m)

                            # 초기 레이블 (probe 2에서 track_id로 업데이트)
                            cls_name = CLASS_NAMES.get(int(cls_id), str(cls_id))
                            obj_meta.obj_label = cls_name
                            _set_osd_label(obj_meta.text_params, cls_name, x1m, y1m, cls_id)

                            pyds.nvds_add_obj_meta_to_frame(frame_meta, obj_meta, None)
                            added_count += 1
                        if ss is not None:
                            ss['last_model_added'] = added_count
                        # 단일 detector 모드에서는 Python에서 추가한 ObjectMeta를
                        # downstream tracker가 detector 결과로 인식하도록 표시해야 한다.
                        # PeopleNet이 실제 활성화된 경우에는 두 번째 primary nvinfer가
                        # 같은 프레임을 처리해야 하므로 여기서 완료 표시를 하지 않는다.
                        if not _st.get('peoplenet_active', False):
                            frame_meta.bInferDone = True
                        break

                try: l_user = l_user.next
                except StopIteration: break

            try: l_frame = l_frame.next
            except StopIteration: break

    except Exception as e:
        print(f'[DS infer probe] 오류: {e}', flush=True)
    return Gst.PadProbeReturn.OK


# ═══════════════════════════════════════════════════════════════
#  Probe 2 — nvtracker src pad: ROI 카운팅 + NvDsDisplayMeta
#  nvtracker가 object_id(track_id)를 할당한 후 이 probe가 실행됨
# ═══════════════════════════════════════════════════════════════
_osd_probe_log_count = 0   # 최초 N 배치만 source_id 로그 출력
_infer_dims_logged = False


def _attach_roi_display_meta(batch_meta, frame_meta, roi_pts, line1, line2):
    """Burn the configured ROI and counting lines into the output frame."""
    polygon = list(roi_pts or [])
    segments = []
    if len(polygon) >= 3:
        segments.extend(
            (polygon[index], polygon[(index + 1) % len(polygon)],
             (0.24, 0.55, 1.0, 1.0), 2)
            for index in range(len(polygon))
        )
    counting_lines = []
    if line1 and len(line1) == 2:
        counting_lines.append(('L1', line1, (0.94, 0.65, 0.0, 1.0)))
    if line2 and len(line2) == 2:
        counting_lines.append(('L2', line2, (0.94, 0.65, 0.0, 1.0)))
    segments.extend(
        (line[0], line[1], color, 4)
        for _label, line, color in counting_lines
    )
    if not segments:
        return

    # NvDsDisplayMeta provides 16 line/text slots. A normal ROI uses four
    # polygon edges plus L1/L2, so it fits in one metadata object.
    display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
    visible_segments = segments[:16]
    display_meta.num_lines = len(visible_segments)
    for index, (start, end, color, width) in enumerate(visible_segments):
        params = display_meta.line_params[index]
        params.x1, params.y1 = int(start[0]), int(start[1])
        params.x2, params.y2 = int(end[0]), int(end[1])
        params.line_width = int(width)
        params.line_color.set(*color)

    visible_labels = counting_lines[:16]
    display_meta.num_labels = len(visible_labels)
    for index, (label, line, color) in enumerate(visible_labels):
        text = display_meta.text_params[index]
        text.display_text = label
        text.x_offset = max(0, int((line[0][0] + line[1][0]) / 2) + 5)
        text.y_offset = max(0, int((line[0][1] + line[1][1]) / 2) - 18)
        text.font_params.font_name = 'Sans'
        text.font_params.font_size = 15
        text.font_params.font_color.set(*color)
        text.set_bg_clr = 1
        text.text_bg_clr.set(0.0, 0.0, 0.0, 0.65)
    pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)


def _osd_counting_probe(pad, info, u_data):
    global _osd_probe_log_count
    buf = info.get_buffer()
    if buf is None:
        return Gst.PadProbeReturn.OK
    try:
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buf))
        if batch_meta is None:
            return Gst.PadProbeReturn.OK

        do_log = _osd_probe_log_count < 5

        l_frame = batch_meta.frame_meta_list
        batch_sources = []
        mux_w = max(1, int(_st.get('mux_w', 640)))
        mux_h = max(1, int(_st.get('mux_h', 480)))
        while l_frame is not None:
            try:
                frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            except StopIteration:
                break

            si = int(frame_meta.source_id)
            if do_log:
                batch_sources.append(si)
            ss = _streams[si] if 0 <= si < len(_streams) else None
            MAX_D = 16
            _frame_time_sec = None

            if ss is not None:
                try:
                    cur_frame_num = int(getattr(frame_meta, 'frame_num'))
                except Exception:
                    cur_frame_num = -1
                try:
                    cur_buf_pts = int(getattr(frame_meta, 'buf_pts'))
                except Exception:
                    cur_buf_pts = -1
                with ss['_lock']:
                    last_frame_num = int(ss.get('_last_frame_num', -1))
                    last_buf_pts = int(ss.get('_last_buf_pts', -1))
                    looped = False
                    loop_reason = ''
                    if cur_frame_num >= 0 and last_frame_num >= 0 and cur_frame_num < last_frame_num:
                        looped = True
                        loop_reason = 'frame_num rewind'
                    if cur_buf_pts >= 0 and last_buf_pts >= 0 and cur_buf_pts + 1_000_000_000 < last_buf_pts:
                        looped = True
                        loop_reason = 'pts rewind'
                    if looped:
                        _advance_stream_loop(ss, loop_reason)
                    if cur_frame_num >= 0:
                        ss['_last_frame_num'] = cur_frame_num
                    if cur_buf_pts >= 0:
                        first_buf_pts = int(ss.get('_first_buf_pts', -1))
                        if first_buf_pts < 0:
                            first_buf_pts = cur_buf_pts
                            ss['_first_buf_pts'] = first_buf_pts
                        _frame_time_sec = max(0.0, (cur_buf_pts - first_buf_pts) / 1_000_000_000.0)
                        ss['_last_frame_time_sec'] = _frame_time_sec
                        ss['_last_buf_pts'] = cur_buf_pts

            # ── NvDsObjectMeta 수집 → BEV 속도 계산 후 레이블 렌더링 ─────
            tracked = []
            object_entries = []
            comp_counts = {}
            l_obj = frame_meta.obj_meta_list
            while l_obj is not None:
                try:
                    obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                except StopIteration:
                    break

                tid    = int(obj_meta.object_id)
                cls_id = int(obj_meta.class_id)
                comp_id = int(getattr(obj_meta, 'unique_component_id', -1))
                comp_counts[comp_id] = comp_counts.get(comp_id, 0) + 1
                rect   = obj_meta.rect_params

                det_rect = obj_meta.detector_bbox_info.org_bbox_coords
                if det_rect.width > 1 and det_rect.height > 1:
                    rect.left = det_rect.left
                    rect.top = det_rect.top
                    rect.width = det_rect.width
                    rect.height = det_rect.height

                x1 = int(rect.left);  y1 = int(rect.top)
                x2 = int(rect.left + rect.width)
                y2 = int(rect.top  + rect.height)
                tracked.append((tid, x1, y1, x2, y2, cls_id))
                object_entries.append((obj_meta, rect, tid, cls_id, x1, y1, x2, y2))

                try: l_obj = l_obj.next
                except StopIteration: break

            if do_log:
                print(f'[DS osd probe] source={si} object_components={comp_counts}', flush=True)

            if ss is not None:
                valid_track_ids = [
                    tid for tid, *_ in tracked
                    if tid >= 0 and tid != int(_UNTRACKED_OBJ_ID)
                ]
                now_tracker = time.perf_counter()
                with ss['_lock']:
                    ss['last_osd_objs'] = len(tracked)
                    if valid_track_ids:
                        ss['_tracker_max_id_seen'] = max(
                            int(ss.get('_tracker_max_id_seen', -1)),
                            max(valid_track_ids),
                        )

                    # 카메라는 루프 경계가 없으므로 ID 임계값 도달 후,
                    # 유효 추적 객체가 2초간 없는 안전한 구간에서 reset을 예약한다.
                    if (ss.get('is_camera', False)
                            and int(ss.get('_tracker_max_id_seen', -1))
                            >= _CAMERA_TRACKER_ID_RESET_THRESHOLD):
                        # ID 미할당 검출까지 포함해 화면에 객체가 하나라도
                        # 보이면 reset하지 않는다.
                        if tracked:
                            ss['_tracker_empty_since'] = 0.0
                        else:
                            empty_since = float(ss.get('_tracker_empty_since', 0.0) or 0.0)
                            if empty_since <= 0.0:
                                ss['_tracker_empty_since'] = now_tracker
                            elif (now_tracker - empty_since) >= _CAMERA_TRACKER_EMPTY_RESET_SEC:
                                if not ss.get('_tracker_reset_requested', False):
                                    print(
                                        f'[DS] {ss.get("video_name","camera")} NvSORT ID '
                                        f'{ss.get("_tracker_max_id_seen")} 도달, 빈 화면 확인 '
                                        f'→ tracker reset 예약',
                                        flush=True,
                                    )
                                ss['_tracker_reset_requested'] = True

            # ── ROI 카운팅 ─────────────────────────────────────────────
            if ss is not None and ss.get('roi_ready'):
                roi_pts, l1, l2 = _roi_to_mux_coords(ss, mux_w, mux_h)
                avg_y1 = (l1[0][1]+l1[1][1])/2
                avg_y2 = (l2[0][1]+l2[1][1])/2
                if avg_y1 <= avg_y2:
                    tp1,tp2,bp1,bp2 = tuple(l1[0]),tuple(l1[1]),\
                                      tuple(l2[0]),tuple(l2[1])
                else:
                    tp1,tp2,bp1,bp2 = tuple(l2[0]),tuple(l2[1]),\
                                      tuple(l1[0]),tuple(l1[1])
                # _lock: loop probe(EOS 리셋)와의 race condition 방지
                with ss['_lock']:
                    _do_counting(tracked, tp1, tp2, bp1, bp2, roi_pts,
                                 frame_idx=ss.get('frame_idx', 0),
                                 video_fps=ss.get('video_fps', 25.0),
                                 loop_count=ss.get('loop_count', 1),
                                 state=ss,
                                 elapsed_offset=ss.get('_total_elapsed_sec', 0.0),
                                 ref_distance=float(ss.get('ref_distance', 0.0) or 0.0),
                                 frame_time_sec=_frame_time_sec,
                                 line1=l1,
                                 line2=l2)
                if _st.get('show_stream', True):
                    _attach_roi_display_meta(
                        batch_meta, frame_meta, roi_pts, l1, l2)

            # ── OSD 레이블: 출력이 켜져 있을 때만 렌더링 메타를 채운다.
            if _st.get('show_stream', True):
                for obj_meta, rect, tid, cls_id, x1, y1, x2, y2 in object_entries:
                    ts = ss['track_state'].get(tid, {}) if ss is not None else {}
                    _disp_spd = ts.get('last_valid_disp')
                    if _disp_spd is None:
                        _disp_spd = ts.get('last_speed_kph')
                    cls_name = CLASS_NAMES.get(cls_id, str(cls_id))
                    _label = f'{cls_name} #{tid}'
                    is_person = str(cls_name).strip().lower() == 'person'
                    if not is_person and _disp_spd is not None:
                        try:
                            _label += f' {float(_disp_spd):.1f}km/h'
                        except Exception:
                            _label += f' {_disp_spd}km/h'
                    _set_osd_label(obj_meta.text_params, _label, x1, y1, cls_id)
                    _set_rect_bbox(rect, x1, y1, x2, y2, cls_id)

            # 재생시간 OSD 제거 — 헤더 UI에서 표시

            if ss is not None:
                with ss['_lock']:
                    ss['frame_idx'] = ss.get('frame_idx', 0) + 1
                    ss['_last_progress_ts'] = time.perf_counter()
                    # ── frame count 기반 루프 감지 ──────────────────────────
                    # ghost pad EOS 프로브가 uridecodebin 버전에서 불안정하므로
                    # OSD 프로브에서 직접 frame_idx와 expected_loop_frames를 비교.
                    exp = ss.get('expected_loop_frames', 0)
                    if (exp > 0 and ss['frame_idx'] >= exp
                            and not ss.get('_eos_seen', False)):
                        _finalize_stream_cycle(ss, 'frame_count')
                        ss['_eos_seen'] = True
                        all_done = all(s.get('_eos_seen', False) for s in _streams)
                        if all_done and not _st.get('restart_requested'):
                            if _USE_NVURI_FILE_LOOP:
                                # 네이티브 루프는 계속 재생하고 통계/트래킹
                                # 경계 상태만 다음 사이클로 넘긴다.
                                for stream in _streams:
                                    stream['_eos_seen'] = False
                                print('[DS] 모든 스트림 루프 완료 (frame_count) '
                                      '→ 재시작 없이 계속', flush=True)
                            else:
                                _st['restart_requested'] = True
                                _st['restart_reason'] = 'frame_count_loop'
                                print('[DS] 모든 스트림 루프 완료 (frame_count) '
                                      '→ 재시작 예약', flush=True)
                ss['_probe_delivery_times'].append(time.perf_counter())

            try: l_frame = l_frame.next
            except StopIteration: break

        if do_log and batch_sources:
            print(f'[DS osd probe] batch#{_osd_probe_log_count} source_ids={batch_sources} '
                  f'(총 {len(batch_sources)}프레임)', flush=True)
            _osd_probe_log_count += 1

    except Exception as e:
        print(f'[DS osd probe] 오류: {e}', flush=True)
    return Gst.PadProbeReturn.OK


# ═══════════════════════════════════════════════════════════════
#  PeopleNet ObjectMeta 필터 probe
#  nvinfer(network-type=0, gie-unique-id=2)가 자동 생성한 ObjectMeta 중
#  person(cls_id=0)만 유지하고 bag(1)/face(2) 제거
# ═══════════════════════════════════════════════════════════════
def _peoplenet_filter_probe(pad, info, u_data):
    buf = info.get_buffer()
    if buf is None:
        return Gst.PadProbeReturn.OK
    try:
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buf))
        if batch_meta is None:
            return Gst.PadProbeReturn.OK
        l_frame = batch_meta.frame_meta_list
        while l_frame is not None:
            try:
                frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            except StopIteration:
                break
            to_remove = []
            person_candidates = []
            l_obj = frame_meta.obj_meta_list
            while l_obj is not None:
                try:
                    obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                except StopIteration:
                    break
                uid = int(getattr(obj_meta, 'unique_component_id', -1))
                cls_id = int(obj_meta.class_id)
                # PeopleNet(gie=2)에서 person(0)만 유지, 나머지 제거
                if uid == 2 and cls_id != 0:
                    to_remove.append(obj_meta)
                # class_id 음수 (파서 이상) 제거
                elif cls_id < 0:
                    to_remove.append(obj_meta)
                elif cls_id == 0 and uid in (1, 2):
                    rect = obj_meta.detector_bbox_info.org_bbox_coords
                    if rect.width <= 1 or rect.height <= 1:
                        rect = obj_meta.rect_params
                    person_candidates.append({
                        'id': len(person_candidates),
                        'component_id': uid,
                        'confidence': float(obj_meta.confidence),
                        'box': (
                            float(rect.left),
                            float(rect.top),
                            float(rect.left + rect.width),
                            float(rect.top + rect.height),
                        ),
                        'meta': obj_meta,
                    })
                try: l_obj = l_obj.next
                except StopIteration: break
            suppressed_ids = cross_model_suppressed_ids(person_candidates)
            to_remove.extend(
                item['meta'] for item in person_candidates
                if item['id'] in suppressed_ids
            )
            for om in to_remove:
                pyds.nvds_remove_obj_meta_from_frame(frame_meta, om)
            try: l_frame = l_frame.next
            except StopIteration: break
    except Exception as e:
        print(f'[DS peoplenet filter] 오류: {e}', flush=True)
    return Gst.PadProbeReturn.OK


# ═══════════════════════════════════════════════════════════════
#  EOS 프로브 (decodebin src pad)
#  파일 끝 → 모든 소스 종료 후 파이프라인 재생성
# ═══════════════════════════════════════════════════════════════
def _make_loop_probe(uri_decode_bin, si):
    """Fallback uridecodebin EOS를 차단하고 파이프라인 재시작을 예약한다."""
    def _probe(pad, info, u_data):
        event = info.get_event()
        if event and event.type == Gst.EventType.EOS:
            if _st.get('running') and 0 <= si < len(_streams):
                ss = _streams[si]
                name = ss.get('video_name', f'stream-{si}')
                all_done = False
                with ss['_lock']:
                    if not ss.get('_eos_seen', False):
                        _finalize_stream_cycle(ss, f'source-eos:{si}')
                        ss['_eos_seen'] = True
                        print(f'[DS] {name} EOS 감지 → 마지막 프레임 대기', flush=True)
                if not _st.get('restart_requested'):
                    all_done = all(s.get('_eos_seen', False) for s in _streams)
                if all_done:
                    _st['restart_requested'] = True
                    _st['restart_reason'] = 'all-sources-eos'
                    print('[DS] 모든 스트림 EOS 완료 → 동시 재시작 예약', flush=True)
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK
    return _probe


def _cb_src_newpad(_decodebin, decoder_src_pad, source_bin):
    caps = decoder_src_pad.get_current_caps() or decoder_src_pad.query_caps(None)
    if not caps:
        return
    gststruct = caps.get_structure(0)
    if gststruct is None:
        return
    gstname = gststruct.get_name()
    if not gstname.startswith('video'):
        return

    features = caps.get_features(0)
    is_nvmm = features and features.contains('memory:NVMM')
    if not is_nvmm:
        print(f'[DS] 경고: 디코더가 NVMM 출력이 아님 ({gststruct.get_name()}) — ghost pad 연결은 계속 시도')
    ghost_pad = source_bin.get_static_pad('src')
    if ghost_pad and ghost_pad.get_target() is None:
        if not ghost_pad.set_target(decoder_src_pad):
            print('[DS] source bin ghost pad 연결 실패')


def _decodebin_child_added(child_proxy, obj, name, user_data):
    if name.find('decodebin') != -1:
        obj.connect('child-added', _decodebin_child_added, user_data)

    if name.find('nvv4l2decoder') != -1:
        if not _IS_JETSON:
            _safe_set_property(obj, 'cudadec-memtype', 2)

    if 'source' in name:
        source_element = child_proxy.get_by_name('source')
        if (source_element is not None
                and source_element.find_property('drop-on-latency') is not None):
            _safe_set_property(obj, 'drop-on-latency', True)


def _create_source_bin(index: int, path: str, enable_loop: bool = True):
    """DeepStream 권장 소스 bin.
    file-loop가 필요하면 nvurisrcbin을 우선 사용하고,
    없으면 uridecodebin으로 폴백한다.
    """
    bin_name = f'source_bin_{index:02d}'
    nbin = Gst.Bin.new(bin_name)
    if nbin is None:
        print(f'[DS] !! source bin 생성 실패: {bin_name}')
        return None, False, None

    use_nvuri = enable_loop and Gst.ElementFactory.find('nvurisrcbin') is not None
    src_name = 'nvurisrcbin' if use_nvuri else 'uridecodebin'
    uri_decode_bin = Gst.ElementFactory.make(src_name, f'uri_decode_bin_{index}')
    if uri_decode_bin is None:
        print(f'[DS] !! 소스 엘리먼트 생성 실패: {src_name}')
        return None, False, None

    if use_nvuri:
        # APACK production pipeline과 동일한 네이티브 무중단 루프.
        # 파이프라인을 PAUSED로 바꾸거나 seek(0)하지 않는다.
        _safe_set_property(uri_decode_bin, 'file-loop', 1)
        # 0: NVBUF_MEM_DEFAULT. Jetson file-loop 권장값.
        _safe_set_property(uri_decode_bin, 'cudadec-memtype', 0)

    source_uri = path if '://' in str(path) else Gst.filename_to_uri(os.path.abspath(path))
    uri_decode_bin.set_property('uri', source_uri)
    uri_decode_bin.connect('pad-added', _cb_src_newpad, nbin)
    uri_decode_bin.connect('child-added', _decodebin_child_added, nbin)

    Gst.Bin.add(nbin, uri_decode_bin)
    ghost = Gst.GhostPad.new_no_target('src', Gst.PadDirection.SRC)
    if ghost is None or not nbin.add_pad(ghost):
        print(f'[DS] !! ghost pad 생성 실패: {bin_name}')
        return None, False, None
    return nbin, use_nvuri, uri_decode_bin


def _camera_caps_string(media_type='image/jpeg', width=None, height=None, fps=None):
    width = int(width or CAMERA_WIDTH)
    height = int(height or CAMERA_HEIGHT)
    fps = int(fps or CAMERA_FPS)
    return f'{media_type},width={width},height={height},framerate={fps}/1'


def _find_camera_device(preferred=None):
    preferred = preferred or CAMERA_DEVICE
    if preferred and os.path.exists(preferred):
        return preferred
    for dev in sorted(_glob.glob('/dev/video*')):
        if os.path.exists(dev):
            return dev
    return ''


def _pull_jpeg_once(device, width=None, height=None, fps=None, timeout_sec=3):
    """UVC 카메라에서 JPEG 1프레임을 가져온다. MJPEG 우선, raw fallback."""
    device = _find_camera_device(device)
    if not device:
        return b''
    caps_mjpeg = _camera_caps_string('image/jpeg', width, height, fps)
    caps_raw = _camera_caps_string('video/x-raw', width, height, fps)
    descs = [
        f'v4l2src device={device} num-buffers=1 do-timestamp=true ! '
        f'{caps_mjpeg} ! appsink name=sink emit-signals=false sync=false max-buffers=1 drop=true',
        f'v4l2src device={device} num-buffers=1 do-timestamp=true ! '
        f'{caps_raw} ! videoconvert ! jpegenc quality=85 ! '
        'appsink name=sink emit-signals=false sync=false max-buffers=1 drop=true',
    ]
    for desc in descs:
        pipeline = None
        try:
            pipeline = Gst.parse_launch(desc)
            sink = pipeline.get_by_name('sink')
            pipeline.set_state(Gst.State.PLAYING)
            sample = sink.emit('try-pull-sample', int(timeout_sec * Gst.SECOND))
            if sample is None:
                continue
            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                return bytes(mapinfo.data)
            finally:
                buf.unmap(mapinfo)
        except Exception as e:
            print(f'[CAM] snapshot 실패: {e}', flush=True)
        finally:
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
    return b''


def _camera_mjpeg_frames(device, width=None, height=None, fps=None):
    """브라우저 대기화면 프리뷰용 multipart MJPEG generator.

    추론 파이프라인 FPS는 유지하고, 설정 중의 MJPEG만 낮은 FPS로
    전달한다. QtWebEngine 소프트웨어 합성의 CPU 부하를 낮춘다.
    """
    device = _find_camera_device(device)
    if not device:
        return
    try:
        preview_fps = max(1, min(
            int(fps or CAMERA_FPS),
            int(os.environ.get('CAMERA_IDLE_PREVIEW_FPS', '10'))))
    except (TypeError, ValueError):
        preview_fps = 10
    frame_interval = 1.0 / preview_fps
    next_frame_at = time.monotonic()
    caps_mjpeg = _camera_caps_string('image/jpeg', width, height, fps)
    desc = (
        f'v4l2src device={device} do-timestamp=true ! {caps_mjpeg} ! '
        'appsink name=sink emit-signals=false sync=false max-buffers=1 drop=true'
    )
    pipeline = None
    try:
        pipeline = Gst.parse_launch(desc)
        sink = pipeline.get_by_name('sink')
        pipeline.set_state(Gst.State.PLAYING)
        # _streams가 비워지면(disconnect) 즉시 루프 종료 → device 해제
        while not _st.get('running', False) and _streams:
            delay = next_frame_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            sample = sink.emit('try-pull-sample', Gst.SECOND)
            if sample is None:
                continue
            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                frame = bytes(mapinfo.data)
            finally:
                buf.unmap(mapinfo)
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n'
                   b'Cache-Control: no-cache\r\n\r\n' + frame + b'\r\n')
            next_frame_at = max(next_frame_at + frame_interval,
                                time.monotonic())
    except GeneratorExit:
        pass
    except Exception as e:
        print(f'[CAM] preview 실패: {e}', flush=True)
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)


def _create_camera_source_bin(index: int, device: str, width=None, height=None, fps=None):
    """DeepStream 입력용 UVC/MJPEG 카메라 source bin."""
    bin_name = f'camera_source_bin_{index:02d}'
    nbin = Gst.Bin.new(bin_name)
    if nbin is None:
        print(f'[CAM] !! source bin 생성 실패: {bin_name}')
        return None

    src = Gst.ElementFactory.make('v4l2src', f'camera_src_{index}')
    capsf = Gst.ElementFactory.make('capsfilter', f'camera_caps_{index}')
    dec = Gst.ElementFactory.make('jpegdec', f'camera_jpegdec_{index}')
    conv = Gst.ElementFactory.make('videoconvert', f'camera_videoconvert_{index}')
    out_caps = Gst.ElementFactory.make('capsfilter', f'camera_out_caps_{index}')
    if any(el is None for el in (src, capsf, dec, conv, out_caps)):
        print('[CAM] !! 카메라 입력 엘리먼트 생성 실패')
        return None

    src.set_property('device', device)
    _safe_set_property(src, 'do-timestamp', True)
    capsf.set_property('caps', Gst.Caps.from_string(
        _camera_caps_string('image/jpeg', width, height, fps)))
    out_caps.set_property('caps', Gst.Caps.from_string('video/x-raw,format=NV12'))

    for el in (src, capsf, dec, conv, out_caps):
        Gst.Bin.add(nbin, el)
    if not src.link(capsf) or not capsf.link(dec) or not dec.link(conv) or not conv.link(out_caps):
        print('[CAM] !! 카메라 source bin link 실패')
        return None

    ghost = Gst.GhostPad.new('src', out_caps.get_static_pad('src'))
    if ghost is None or not nbin.add_pad(ghost):
        print(f'[CAM] !! ghost pad 생성 실패: {bin_name}')
        return None
    return nbin


# ═══════════════════════════════════════════════════════════════
#  단일 파이프라인 빌더
#  모든 스트림을 하나의 파이프라인에서 처리
