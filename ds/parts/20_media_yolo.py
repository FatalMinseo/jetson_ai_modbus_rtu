# Auto-split from runtime.py: lines 498-889
def _get_video_info(path):
    """Return (fps, width, height, duration_sec) without decoding frames.

    An external metadata process is preferred because loading NVIDIA decoder
    plugins in this long-running Python process may require an EGL display even
    though only stream metadata is needed.
    """
    abs_path = os.path.abspath(path)

    ffprobe = shutil.which('ffprobe')
    if ffprobe:
        try:
            proc = subprocess.run([
                ffprobe, '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height,r_frame_rate:format=duration',
                '-of', 'json', abs_path,
            ], capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                metadata = json.loads(proc.stdout or '{}')
                stream = (metadata.get('streams') or [{}])[0]
                rate = str(stream.get('r_frame_rate') or '0/1').split('/', 1)
                fps = float(rate[0]) / max(1.0, float(rate[1] if len(rate) > 1 else 1))
                width = int(stream.get('width') or 0)
                height = int(stream.get('height') or 0)
                duration = float((metadata.get('format') or {}).get('duration') or 0)
                if width > 0 and height > 0:
                    return round(fps or 25.0, 3), width, height, round(max(0.0, duration), 3)
        except Exception as exc:
            print(f'[ffprobe] metadata fallback: {exc}', flush=True)

    discoverer = shutil.which('gst-discoverer-1.0')
    if discoverer:
        try:
            proc = subprocess.run(
                [discoverer, abs_path], capture_output=True, text=True, timeout=20)
            output = (proc.stdout or '') + '\n' + (proc.stderr or '')
            width_match = re.search(r'^\s*Width:\s*(\d+)\s*$', output, re.MULTILINE)
            height_match = re.search(r'^\s*Height:\s*(\d+)\s*$', output, re.MULTILINE)
            rate_match = re.search(r'^\s*Frame rate:\s*(\d+)\s*/\s*(\d+)\s*$', output, re.MULTILINE)
            duration_match = re.search(
                r'^\s*Duration:\s*(\d+):(\d+):([0-9.]+)\s*$', output, re.MULTILINE)
            if width_match and height_match:
                width = int(width_match.group(1))
                height = int(height_match.group(1))
                fps = (
                    int(rate_match.group(1)) / max(1, int(rate_match.group(2)))
                    if rate_match else 25.0
                )
                duration = 0.0
                if duration_match:
                    duration = (
                        int(duration_match.group(1)) * 3600
                        + int(duration_match.group(2)) * 60
                        + float(duration_match.group(3))
                    )
                return round(fps, 3), width, height, round(duration, 3)
        except Exception as exc:
            print(f'[gst-discoverer] metadata fallback: {exc}', flush=True)

    try:
        gi.require_version('GstPbutils', '1.0')
        from gi.repository import GstPbutils
        disc = GstPbutils.Discoverer.new(10 * Gst.SECOND)
        uri  = Gst.filename_to_uri(abs_path)
        info = disc.discover_uri(uri)
        dur_ns = int(info.get_duration() or 0)
        duration_sec = max(0.0, dur_ns / float(Gst.SECOND))
        for stream in info.get_video_streams():
            fps_n = stream.get_framerate_num()
            fps_d = max(1, stream.get_framerate_denom())
            fps   = fps_n / fps_d if fps_n > 0 else 25.0
            w     = stream.get_width()
            h     = stream.get_height()
            if w > 0 and h > 0:
                return round(fps, 3), w, h, round(duration_sec, 3)
    except Exception as e:
        print(f'[Discoverer] 오류: {e}')
    return None


def _should_normalize_video(path: str) -> bool:
    ext = os.path.splitext(str(path))[1].lower()
    return ext in {'.avi', '.mkv', '.mov', '.ts', '.h264', '.h265', '.hevc'}


def _normalize_video_to_mp4(path: str, display_name: str = ''):
    """DeepStream 입력 안정화를 위해 H.264 MP4로 정규화.

    반환:
      (normalized_path, error_message, normalized_bool)
    """
    src_path = os.path.abspath(path)
    if not _should_normalize_video(src_path):
        return src_path, '', False
    if not _FFMPEG:
        msg = 'ffmpeg가 없어 MP4(H.264) 변환을 수행할 수 없습니다'
        print(f'[NORMALIZE] {msg}: {os.path.basename(src_path)}', flush=True)
        return '', msg, False

    work_dir = globals().get('VIDEO_DIR') or tempfile.gettempdir()
    os.makedirs(work_dir, exist_ok=True)
    fd, out_path = tempfile.mkstemp(suffix='.mp4', prefix='norm_', dir=work_dir)
    os.close(fd)
    label = display_name or os.path.basename(src_path)
    cmd = [
        _FFMPEG, '-y',
        '-hide_banner', '-loglevel', 'error',
        '-i', src_path,
        '-map', '0:v:0',
        '-an',
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
        out_path,
    ]
    print(f'[NORMALIZE] MP4 변환 시작: {label} -> {os.path.basename(out_path)}', flush=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception as e:
        try:
            os.unlink(out_path)
        except Exception:
            pass
        msg = f'ffmpeg 실행 실패: {e}'
        print(f'[NORMALIZE] {msg}', flush=True)
        return '', msg, False

    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        try:
            os.unlink(out_path)
        except Exception:
            pass
        err = (proc.stderr or proc.stdout or '').strip()
        msg = f'MP4 변환 실패: {err[:300]}' if err else 'MP4 변환 실패'
        print(f'[NORMALIZE] {label}: {msg}', flush=True)
        return '', msg, False

    print(f'[NORMALIZE] MP4 변환 완료: {label}', flush=True)
    return out_path, '', True


_FIRST_FRAME_CACHE = {}


def _get_first_frame_jpeg(path, max_dim=None):
    """Extract a lightweight preview without allocating NVIDIA/NVMM buffers.

    Video selection must only prepare the ROI screen.  Starting a temporary
    NVIDIA decoder here competed with the real DeepStream pipeline and could
    block `/add_local` for several seconds when NVMM was under pressure.
    """
    if not _PIL_OK or not _FFMPEG:
        return None
    abs_path = os.path.abspath(path)
    try:
        stat = os.stat(abs_path)
        preview_dim = max(80, min(1280, int(max_dim or 1280)))
        cache_key = (abs_path, stat.st_mtime_ns, stat.st_size, preview_dim)
        cached = _FIRST_FRAME_CACHE.get(cache_key)
        if cached is not None:
            return cached

        proc = subprocess.run([
            _FFMPEG,
            '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-threads', '2',
            '-i', abs_path,
            '-map', '0:v:0',
            '-an', '-sn', '-dn',
            '-frames:v', '1',
            '-vf', f"scale=w='min({preview_dim},iw)':h=-2",
            '-q:v', '4',
            '-f', 'image2pipe', '-c:v', 'mjpeg', 'pipe:1',
        ], capture_output=True, timeout=8)
        jpeg = bytes(proc.stdout or b'')
        if proc.returncode != 0 or not jpeg.startswith(b'\xff\xd8'):
            error = (proc.stderr or b'').decode(errors='replace').strip()
            print(
                f'[FirstFrame] ffmpeg 실패: {os.path.basename(abs_path)} '
                f'{error[:200]}',
                flush=True,
            )
            return None

        if len(_FIRST_FRAME_CACHE) >= 16:
            _FIRST_FRAME_CACHE.pop(next(iter(_FIRST_FRAME_CACHE)))
        _FIRST_FRAME_CACHE[cache_key] = jpeg
        return jpeg
    except Exception as exc:
        print(f'[FirstFrame] 오류: {os.path.basename(abs_path)} {exc!r}', flush=True)
        return None


def _make_small_thumb(jpeg_bytes, max_dim=80):
    """JPEG 바이트를 max_dim 크기로 리사이즈."""
    if not _PIL_OK or not jpeg_bytes:
        return None
    try:
        img = Image.open(_io.BytesIO(jpeg_bytes))
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format='JPEG', quality=80)
        return buf.getvalue()
    except Exception:
        return None


def _nms_numpy(boxes_xywh, scores, iou_thresh=0.45):
    """순수 numpy NMS — cv2.dnn.NMSBoxes 대체.
    boxes_xywh: [[x1,y1,w,h],...], scores: [...]
    """
    if not len(boxes_xywh):
        return []
    boxes  = np.array(boxes_xywh, dtype=np.float32)
    scores = np.array(scores,     dtype=np.float32)
    order  = np.argsort(-scores)
    keep   = []
    while len(order):
        i = order[0]; keep.append(i)
        if len(order) == 1:
            break
        rest = order[1:]
        b  = boxes[i]; rs = boxes[rest]
        ix1 = np.maximum(b[0],       rs[:, 0])
        iy1 = np.maximum(b[1],       rs[:, 1])
        ix2 = np.minimum(b[0]+b[2],  rs[:, 0]+rs[:, 2])
        iy2 = np.minimum(b[1]+b[3],  rs[:, 1]+rs[:, 3])
        inter = np.maximum(0, ix2-ix1) * np.maximum(0, iy2-iy1)
        ab = b[2]*b[3]; ar = rs[:, 2]*rs[:, 3]
        iou = inter / (ab + ar - inter + 1e-6)
        order = rest[iou < iou_thresh]
    return keep


def _point_in_polygon(pt, poly):
    """Ray casting 알고리즘 — cv2.pointPolygonTest 대체.
    poly: [[x,y], ...] (최소 3점)
    """
    x, y = float(pt[0]), float(pt[1])
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(poly[i][0]), float(poly[i][1])
        xj, yj = float(poly[j][0]), float(poly[j][1])
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi + 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def _compute_grid_layout(n):
    if n == 1: return 1, 1
    if n == 2: return 1, 2   # 1 row, 2 cols (side-by-side)
    if n <= 4: return 2, 2
    if n <= 6: return 2, 3
    return (n + 2) // 3, 3


def _align32(v: int) -> int:
    return max(32, int(round(v / 32.0) * 32))


def _compute_tracker_dims(mux_w: int, mux_h: int):
    """tracker 입력 해상도는 원본 비율을 최대한 유지해 왜곡을 줄인다."""
    base_w = 960 if mux_w >= 960 else 640
    base_h = max(32, int(round(base_w * mux_h / max(1, mux_w))))
    return _align32(base_w), _align32(base_h)


def _make_static_composite_pil(frames_jpeg_and_names):
    """PIL로 첫 프레임 그리드 합성 JPEG 생성.
    각 셀에서 가능한 한 크게 표시 (업스케일 포함), 중앙 배치.
    셀 구분선 표시.
    """
    if not _PIL_OK or not frames_jpeg_and_names:
        return None
    n = len(frames_jpeg_and_names)
    rows, cols = _compute_grid_layout(n)
    out_w, out_h = 1280, 720
    cell_w = out_w // cols; cell_h = out_h // rows
    composite = Image.new('RGB', (out_w, out_h), (18, 18, 18))

    for i, (jpeg_bytes, name) in enumerate(frames_jpeg_and_names):
        if i >= rows * cols:
            break
        r, c = i // cols, i % cols
        x0, y0 = c * cell_w, r * cell_h
        if jpeg_bytes:
            try:
                img = Image.open(_io.BytesIO(jpeg_bytes))
                # 셀에 꽉 차도록 스케일 계산 (업스케일 포함)
                scale  = min(cell_w / img.width, cell_h / img.height)
                new_w  = max(1, round(img.width  * scale))
                new_h  = max(1, round(img.height * scale))
                img    = img.resize((new_w, new_h), Image.LANCZOS)
                ox = x0 + (cell_w - new_w) // 2
                oy = y0 + (cell_h - new_h) // 2
                composite.paste(img, (ox, oy))
            except Exception:
                pass

    # 셀 구분선
    if rows > 1 or cols > 1:
        draw = ImageDraw.Draw(composite)
        line_color = (60, 60, 60)
        for c in range(1, cols):
            x = c * cell_w
            draw.line([(x, 0), (x, out_h - 1)], fill=line_color, width=2)
        for r in range(1, rows):
            y = r * cell_h
            draw.line([(0, y), (out_w - 1, y)], fill=line_color, width=2)

    buf = _io.BytesIO()
    composite.save(buf, format='JPEG', quality=90)
    return buf.getvalue()


def _stream_to_mux_point(x, y, ss, mux_w, mux_h):
    """원본 스트림 좌표를 nvstreammux 출력 좌표로 변환.
    enable-padding=1 기준 중앙 패딩을 반영한다.
    """
    sw = max(1, int(ss.get('frame_w', mux_w)))
    sh = max(1, int(ss.get('frame_h', mux_h)))
    scale = min(mux_w / sw, mux_h / sh)
    pad_x = (mux_w - sw * scale) / 2.0
    pad_y = (mux_h - sh * scale) / 2.0
    return (
        float(np.clip(pad_x + x * scale, 0.0, mux_w)),
        float(np.clip(pad_y + y * scale, 0.0, mux_h)),
    )


def _roi_to_mux_coords(ss, mux_w, mux_h):
    roi_pts = [
        list(_stream_to_mux_point(float(p[0]), float(p[1]), ss, mux_w, mux_h))
        for p in ss.get('roi_pts', [])
        if len(p) == 2
    ]
    line1 = [
        list(_stream_to_mux_point(float(p[0]), float(p[1]), ss, mux_w, mux_h))
        for p in ss.get('line1', [])
        if len(p) == 2
    ]
    line2 = [
        list(_stream_to_mux_point(float(p[0]), float(p[1]), ss, mux_w, mux_h))
        for p in ss.get('line2', [])
        if len(p) == 2
    ]
    return roi_pts, line1, line2


# ═══════════════════════════════════════════════════════════════
#  YOLO 후처리 (output0 텐서 → 검출 목록)
# ═══════════════════════════════════════════════════════════════
def _reshape_yolo_output(tensor_1d, infer_dims, rows):
    """output0 버퍼를 [rows, num_boxes] 형태로 정규화."""
    arr = np.asarray(tensor_1d, dtype=np.float32).reshape(-1)

    dims = []
    if infer_dims is not None:
        try:
            for i in range(int(infer_dims.numDims)):
                v = int(infer_dims.d[i])
                if v > 0:
                    dims.append(v)
        except Exception:
            dims = []

    # 배치 차원 1은 제거
    eff = [d for d in dims if d > 1]
    if len(eff) >= 2:
        # 마지막 두 축 중 하나가 rows(=4+num_classes)인 경우를 우선 처리
        a, b = eff[-2], eff[-1]
        if a == rows:
            return arr.reshape(a, b)
        if b == rows:
            return arr.reshape(a, b).T

    cols = arr.size // rows
    return arr[:rows * cols].reshape(rows, cols)


def yolo_postprocess(tensor_1d, num_classes, infer_w=640, infer_h=640,
                     conf_thresh=0.30, nms_iou=0.45, infer_dims=None):
    """
    output0 텐서: shape (4+num_classes, N) flattened
      - N = 8400 (640 모델) / 33600 (1280 모델) 등 모델 의존
      - cx/cy/w/h 는 이미 infer_w×infer_h 픽셀 좌표
    Returns: [[x1,y1,x2,y2, conf, cls_id], ...]  (infer_w×infer_h 좌표)
    호출자(probe)가 letterbox 역변환으로 mux 좌표로 변환한다.
    """
    rows = 4 + num_classes
    if not isinstance(tensor_1d, np.ndarray):
        tensor_1d = np.array(tensor_1d, dtype=np.float32)
    if tensor_1d.size < rows:
        return []

    data = _reshape_yolo_output(tensor_1d, infer_dims, rows)
    if data.size == 0 or data.shape[0] != rows:
        return []
    cx_    = data[0]; cy_ = data[1]; w_ = data[2]; h_ = data[3]
    scores = data[4:]
    conf   = scores.max(axis=0)
    cls_id = scores.argmax(axis=0)

    mask = conf > conf_thresh
    if not mask.any():
        return []

    cx_, cy_, w_, h_ = cx_[mask], cy_[mask], w_[mask], h_[mask]
    conf, cls_id = conf[mask], cls_id[mask]

    # 텐서 좌표는 이미 infer_w×infer_h 공간 — 추가 스케일 불필요
    # (INFER_W 전역 상수 제거: 모델 입력 크기와 무관하게 동작)
    x1 = cx_ - w_ / 2; y1 = cy_ - h_ / 2
    x2 = cx_ + w_ / 2; y2 = cy_ + h_ / 2

    # 경계 클리핑
    x1 = np.clip(x1, 0, infer_w); x2 = np.clip(x2, 0, infer_w)
    y1 = np.clip(y1, 0, infer_h); y2 = np.clip(y2, 0, infer_h)

    results = []
    for cid in np.unique(cls_id):
        m      = cls_id == cid
        boxes  = np.stack([x1[m], y1[m], (x2-x1)[m], (y2-y1)[m]], axis=1)
        sc     = conf[m]
        ids    = _nms_numpy(boxes.tolist(), sc.tolist(), nms_iou)
        for i in ids:
            results.append([float(x1[m][i]), float(y1[m][i]),
                             float(x2[m][i]), float(y2[m][i]),
                             float(sc[i]),    int(cid)])
    return results


_ORT_SESSIONS = {}

_IMAGE_LABEL_FONT_PATHS = (
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
)
