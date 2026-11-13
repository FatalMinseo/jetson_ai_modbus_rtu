# Auto-split from runtime.py: lines 1124-1321
def _line_side(p1, p2, pt):
    v = (p2[0]-p1[0])*(pt[1]-p1[1]) - (p2[1]-p1[1])*(pt[0]-p1[0])
    return 1 if v > 0 else (-1 if v < 0 else 0)

def _segs_cross(a1, a2, b1, b2):
    def _c(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    d1,d2,d3,d4 = _c(b1,b2,a1),_c(b1,b2,a2),_c(a1,a2,b1),_c(a1,a2,b2)
    return ((d1>0 and d2<0) or (d1<0 and d2>0)) and \
           ((d3>0 and d4<0) or (d3<0 and d4>0))

def _bbox_hits_line(x1, y1, x2, y2, p1, p2):
    def _in(p): return x1 <= p[0] <= x2 and y1 <= p[1] <= y2
    if _in(p1) or _in(p2): return True
    c = [(x1,y1),(x2,y1),(x2,y2),(x1,y2)]
    return any(_segs_cross(p1, p2, c[i], c[(i+1)%4]) for i in range(4))

_BEV_W = 400.0
_BEV_H = 600.0
_TRACK_STATE_TTL_SEC = max(
    5.0, float(os.environ.get('DS_TRACK_STATE_TTL_SEC', '30')))
_TRACK_STATE_MAX = max(
    256, int(os.environ.get('DS_TRACK_STATE_MAX', '4096')))


def _prune_tracking_state(state, active_ids, frame_idx, fps):
    """Bound per-track Python state without modifying nvtracker object IDs."""
    track_state = state.get('track_state', {})
    if not track_state:
        return
    # Once per second is sufficient and avoids scanning the mapping per frame.
    if frame_idx % max(1, int(round(fps))) != 0 and len(track_state) <= _TRACK_STATE_MAX:
        return
    cutoff = frame_idx - max(1, int(round(fps * _TRACK_STATE_TTL_SEC)))
    stale_ids = [
        tid for tid, item in track_state.items()
        if tid not in active_ids and int(item.get('last_seen_frame', frame_idx)) < cutoff
    ]
    remaining = len(track_state) - len(stale_ids)
    if remaining > _TRACK_STATE_MAX:
        candidates = sorted(
            (
                (int(item.get('last_seen_frame', -1)), tid)
                for tid, item in track_state.items()
                if tid not in active_ids and tid not in stale_ids
            ),
        )
        stale_ids.extend(
            tid for _, tid in candidates[:remaining - _TRACK_STATE_MAX])
    for tid in stale_ids:
        track_state.pop(tid, None)
        state.get('pos_history', {}).pop(tid, None)
        state.get('live_speed', {}).pop(tid, None)


def _line_crosses_by_side(prev, curr, lp1, lp2):
    # 무한 직선이 아닌 선분(lp1-lp2) 범위 내에서만 교차 감지.
    # 선분의 x 범위 밖에 있으면 교차로 보지 않는다.
    if not (_segs_cross(prev, curr, lp1, lp2)):
        return False
    return _line_side(lp1, lp2, prev) * _line_side(lp1, lp2, curr) < 0


def _reset_passage(ts, clear_result=False):
    """Discard one passage's timing/samples, not the object's tracking ID."""
    ts['line1_cross'] = None
    ts['line2_cross'] = None
    ts['bev_y_hist'] = []
    ts['speed_samples'] = []
    ts['last_valid_disp'] = None
    if clear_result:
        ts['speed'] = None
        ts['last_speed_kph'] = None
        ts['direction'] = None


def _advance_passage(ts, prev, curr, current_time, line1, line2):
    """Enter through either line; complete only by exiting through the other.

    Returning through the entry line aborts that attempt. A completed exit is
    consumed by the caller, so it cannot become the next passage's entry.
    """
    crossings = []
    for number, line, other in ((1, line1, line2), (2, line2, line1)):
        if not _line_crosses_by_side(prev, curr, *line):
            continue
        a, b = line
        def side_value(point):
            return ((b[0] - a[0]) * (point[1] - a[1])
                    - (b[1] - a[1]) * (point[0] - a[0]))
        before, after = side_value(prev), side_value(curr)
        fraction = before / (before - after)
        midpoint = ((other[0][0] + other[1][0]) / 2.0,
                    (other[0][1] + other[1][1]) / 2.0)
        inside_side = _line_side(*line, midpoint)
        entering = inside_side != 0 and _line_side(*line, curr) == inside_side
        crossings.append((fraction, number, entering))

    completed = False
    for _, number, entering in sorted(crossings):
        key = f'line{number}_cross'
        other_key = f'line{3 - number}_cross'
        if entering:
            _reset_passage(ts, clear_result=True)
            ts[key] = {'time': current_time}
        elif ts.get(other_key) is not None:
            ts[key] = {'time': current_time}
            completed = True
        else:
            # Started inside, or turned back through the same entry line.
            _reset_passage(ts, clear_result=True)
    return completed


def _build_bev_homography(line1, line2):
    """Build the same two-line BEV transform used by speed_test/video_processor.py."""
    l1 = [(float(p[0]), float(p[1])) for p in line1]
    l2 = [(float(p[0]), float(p[1])) for p in line2]
    y1_avg = (l1[0][1] + l1[1][1]) / 2.0
    y2_avg = (l2[0][1] + l2[1][1]) / 2.0
    if y1_avg <= y2_avg:
        top_pts, bot_pts = list(l1), list(l2)
    else:
        top_pts, bot_pts = list(l2), list(l1)
    top_pts.sort(key=lambda p: p[0])
    bot_pts.sort(key=lambda p: p[0])

    src = np.array([top_pts[0], top_pts[1], bot_pts[1], bot_pts[0]], dtype=np.float64)
    dst = np.array([[0.0, 0.0], [_BEV_W, 0.0], [_BEV_W, _BEV_H], [0.0, _BEV_H]],
                   dtype=np.float64)
    a = []
    b = []
    for (x, y), (u, v) in zip(src, dst):
        a.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y])
        b.append(u)
        a.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y])
        b.append(v)
    h = np.linalg.solve(np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64))
    return np.array([
        [h[0], h[1], h[2]],
        [h[3], h[4], h[5]],
        [h[6], h[7], 1.0],
    ], dtype=np.float64)


def _to_bev_y(h, pt):
    x, y = float(pt[0]), float(pt[1])
    den = h[2, 0] * x + h[2, 1] * y + h[2, 2]
    if abs(den) < 1e-9:
        raise ValueError('invalid BEV transform')
    return (h[1, 0] * x + h[1, 1] * y + h[1, 2]) / den


def _do_counting(tracked, tp1, tp2, bp1, bp2, roi_pts,
                 frame_idx=0, video_fps=25.0, loop_count=1, state=None,
                 elapsed_offset=0.0, ref_distance=0.0,
                 frame_time_sec=None, line1=None, line2=None):
    """
    tracked: [(track_id, x1, y1, x2, y2, cls_id), ...]
    roi_pts: [[x,y], ...] (polygon vertices in stream native coords)
    ref_distance: real-world distance between line1 and line2 in meters (0 = disabled)
    """
    if state is None: state = _st
    fps = max(float(video_fps or 0.0), 1.0)
    try:
        frame_time = None if frame_time_sec is None else max(0.0, float(frame_time_sec))
    except Exception:
        frame_time = None

    def _vtime():
        if frame_time is not None:
            return round(elapsed_offset + frame_time, 1)
        return round(elapsed_offset + frame_idx / fps, 1)

    line1 = line1 or (tp1, tp2)
    line2 = line2 or (bp1, bp2)
    line1 = (tuple(line1[0]), tuple(line1[1]))
    line2 = (tuple(line2[0]), tuple(line2[1]))
    y1_avg = (line1[0][1] + line1[1][1]) / 2.0
    y2_avg = (line2[0][1] + line2[1][1]) / 2.0
    lower_line_num = 1 if y1_avg > y2_avg else 2
    bev_scale = float(ref_distance or 0.0) / _BEV_H if ref_distance > 0 else 0.0
    h_bev = None
    if bev_scale > 0:
        try:
            h_bev = _build_bev_homography(line1, line2)
        except Exception as e:
            if not state.get('_bev_warned'):
                print(f'[DS speed] BEV Homography 계산 실패, time fallback 사용: {e}', flush=True)
                state['_bev_warned'] = True

    def _new_track_state(cls_id, cur_pos):
        return {
            'cls': cls_id,
            'last_pos': cur_pos,
            'last_time': frame_time if frame_time is not None else frame_idx / fps,
            'bev_y_hist': [],
            'speed_samples': [],
            'last_valid_disp': None,
            'line1_cross': None,
            'line2_cross': None,
            'speed': None,
            'direction': None,
            'last_speed_kph': None,
            'class_votes': [],
            'last_seen_frame': frame_idx,
        }

    active_track_ids = set()
    for (track_id, x1, y1, x2, y2, cls_id) in tracked:
        active_track_ids.add(track_id)
        cx = (float(x1) + float(x2)) / 2.0
        cur_pos = (cx, float(y2))  # speed_test와 동일한 bbox foot point
        cur_time_s = frame_time if frame_time is not None else frame_idx / fps

        ts = state['track_state'].setdefault(track_id, _new_track_state(cls_id, cur_pos))
        ts['cls'] = cls_id
        ts['last_seen_frame'] = frame_idx
        ts.setdefault('class_votes', []).append(cls_id)
        if len(ts['class_votes']) > 30:
            del ts['class_votes'][:-30]

        prev_pos = ts.get('last_pos', cur_pos)
        passage_completed = _advance_passage(
            ts, prev_pos, cur_pos, cur_time_s, line1, line2)

        if h_bev is not None and bev_scale > 0:
            try:
                bev_y = _to_bev_y(h_bev, cur_pos)
                in_zone = 0.0 <= bev_y <= _BEV_H  # L1~L2 사이에 있을 때만
                if in_zone:
                    hist = ts.setdefault('bev_y_hist', [])
                    # 이상값 필터: 350km/h 초과 점프는 트래커 혼동으로 판단 → 히스토리 리셋
                    if hist:
                        max_bev_jump = _BEV_H * (1.0 / 0.2) * (1.0 / max(video_fps, 1.0))
                        if abs(bev_y - hist[-1][0]) > max_bev_jump:
                            hist.clear()
                    hist.append((bev_y, cur_time_s))
                    if len(hist) > 30:
                        del hist[:-30]
                    if len(hist) >= 5:
                        w_start = hist[max(0, len(hist) - 10)]
                        w_end   = hist[-1]
                        dy_px   = w_end[0] - w_start[0]
                        dt_w    = w_end[1] - w_start[1]
                        if dt_w >= 0.16:
                            v_kph = abs(dy_px) * bev_scale / dt_w * 3.6
                            if 1.0 < v_kph < 300.0:
                                samples = ts.setdefault('speed_samples', [])
                                samples.append(v_kph)
                                if len(samples) > 30:
                                    del samples[:-30]
                                if len(samples) >= 2:
                                    ts['last_valid_disp'] = float(np.median(samples[-10:]))
                else:
                    # ROI 구간 밖으로 나가면 실시간 표시 속도 초기화
                    # → ROI 통과 후 외부에서 다른 차량 속도처럼 보이는 현상 제거
                    ts['last_valid_disp'] = None
            except Exception:
                pass

        if passage_completed:
            t1 = float(ts['line1_cross']['time'])
            t2 = float(ts['line2_cross']['time'])
            fc = cls_id
            if ts.get('class_votes'):
                fc = max(set(ts['class_votes']), key=ts['class_votes'].count)
            class_name = CLASS_NAMES.get(int(fc), str(fc))
            is_person = _norm_class_name(class_name) == 'person'
            # 최종 카운팅 속도: 시간 기반 우선 사용
            # BEV는 트래킹 노이즈를 ~12x 증폭시켜 부정확 → 실시간 표시용으로만 활용
            dt = abs(t2 - t1)
            speed_kph = (float(ref_distance) / dt) * 3.6 if dt >= 0.05 else None
            method = 'time'
            # BEV 샘플이 충분할 때 교차검증: time 기반과 30% 이내로 일치하면 평균 사용
            samples = ts.get('speed_samples', [])
            if speed_kph is not None and len(samples) >= 5:
                bev_spd = float(np.median(samples))
                if abs(bev_spd - speed_kph) / max(speed_kph, 1.0) < 0.30:
                    speed_kph = (speed_kph + bev_spd) / 2.0
                    method = 'BEV+time'

            # 사람은 통행량만 제공하므로 속도 산출 성공 여부로 카운팅을
            # 제한하지 않는다. 차량/오토바이는 기존 속도 유효성 검사를 유지한다.
            speed_valid = speed_kph is not None and 1.0 < speed_kph < 350.0
            if is_person or speed_valid:
                first_line = 1 if t1 <= t2 else 2
                direction = '상행' if first_line == lower_line_num else '하행'
                reported_speed = 0.0 if is_person else speed_kph
                ts['speed'] = reported_speed
                ts['last_speed_kph'] = None if is_person else speed_kph
                ts['direction'] = direction
                state['counts'].setdefault(fc, {'상행': 0, '하행': 0})[direction] += 1
                trend_event_time = elapsed_offset + max(t1, t2)
                _record_rolling_event(
                    state,
                    class_name,
                    direction,
                    reported_speed,
                )
                _record_vehicle_trend_event(
                    state,
                    class_name,
                    direction,
                    reported_speed,
                    trend_event_time,
                    track_id,
                )
                record_roi_event = globals().get('_record_modbus_roi_event')
                if callable(record_roi_event):
                    record_roi_event(class_name, direction, reported_speed)
                # 실시간 탐지로그 (최대 50건, 초과분 즉시 삭제 — 디스크 불필요)
                det_log = state.setdefault('det_log', [])
                det_log.append({
                    'dir': direction,
                    'cls': _display_class_name(CLASS_NAMES.get(int(fc), str(fc))),
                    'tid': int(track_id),
                    'speed_kph': round(float(reported_speed), 1),
                    'cycle': int(loop_count or state.get('loop_count', 1)),
                    'vtime': round(_vtime(), 1),
                    'logged_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                })
                if len(det_log) > 50:
                    del det_log[:-50]  # 최신 50건만 유지, 나머지 즉시 소멸
                if not is_person:
                    if 'speed_stat' in state:
                        _acc_update(state['speed_stat'], speed_kph)
                    spd_cls = state.setdefault('speed_by_cls_dir', {}).setdefault(fc, {})
                    _acc_update(spd_cls.setdefault(direction, _acc_empty()), speed_kph)

            # Consume even an invalid-speed passage. A subsequent traversal
            # with this same ID must use fresh entry/exit times and BEV samples.
            _reset_passage(ts)

        ts['last_pos'] = cur_pos
        ts['last_time'] = cur_time_s

    _prune_tracking_state(state, active_track_ids, frame_idx, fps)

    analysis_time = elapsed_offset + (
        frame_time if frame_time is not None else frame_idx / fps
    )
    _update_vehicle_speed_trends(state, analysis_time)


# ═══════════════════════════════════════════════════════════════
#  공유 상태
