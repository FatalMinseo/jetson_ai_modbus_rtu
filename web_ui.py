#!/usr/bin/env python3
"""Public Flask UI process for ds_camera.

The heavy DeepStream runtime lives in a separate infer_daemon.py process.  This
app intentionally does not import GStreamer or pyds; it only proxies browser
requests to the daemon so the web UI can restart without stopping inference.
"""

import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, Response, jsonify, request


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DAEMON_URL = os.environ.get('DS_CAMERA_DAEMON_URL', 'http://127.0.0.1:5122').rstrip('/')
HOST = os.environ.get('HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', '5022'))

app = Flask(__name__, static_folder=None)

_HOP_BY_HOP_HEADERS = {
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailer',
    'transfer-encoding',
    'upgrade',
}

_BODYLESS_METHODS = {'GET', 'HEAD', 'OPTIONS'}


def _target_url(path):
    qs = request.query_string.decode('utf-8', errors='ignore')
    url = f'{DAEMON_URL}/{path.lstrip("/")}'
    if qs:
        url += '?' + qs
    return url


def _proxy_headers():
    headers = {}
    for key, value in request.headers.items():
        lk = key.lower()
        if lk in _HOP_BY_HOP_HEADERS or lk in {'host', 'content-length'}:
            continue
        headers[key] = value
    headers['Accept-Encoding'] = 'identity'
    headers['X-Forwarded-Host'] = request.host
    headers['X-Forwarded-Proto'] = request.scheme
    return headers


def _response_headers(resp):
    headers = []
    for key, value in resp.headers.items():
        lk = key.lower()
        if lk in _HOP_BY_HOP_HEADERS:
            continue
        headers.append((key, value))
    return headers


def _daemon_unavailable(path, exc):
    msg = f'추론 데몬에 연결할 수 없습니다: {DAEMON_URL}'
    if path in {'', '/'}:
        html = f"""<!doctype html>
<html lang="ko">
<meta charset="utf-8">
<title>ds_camera daemon offline</title>
<body style="margin:0;background:#111;color:#eee;font-family:Segoe UI,sans-serif">
  <div style="max-width:720px;margin:12vh auto;padding:28px;background:#1e1e1e;border:1px solid #333;border-radius:10px">
    <h1 style="font-size:20px;margin:0 0 12px">추론 데몬이 실행 중이 아닙니다</h1>
    <p style="color:#aaa;line-height:1.6">{msg}</p>
    <pre style="white-space:pre-wrap;background:#151515;border:1px solid #292929;border-radius:8px;padding:12px;color:#d0d0d0">./jetsonctl.sh start
./jetsonctl.sh status</pre>
    <p style="color:#666;font-size:12px">원인: {type(exc).__name__}: {exc}</p>
  </div>
</body>
</html>"""
        return Response(html, status=503, mimetype='text/html')
    return jsonify({'ok': False, 'error': msg, 'detail': f'{type(exc).__name__}: {exc}'}), 503


@app.route('/__ui/health')
def ui_health():
    # This endpoint is the proxy process readiness check.  It must not wait
    # for inferd: the UI is also how an operator diagnoses or starts inferd.
    return jsonify({
        'ok': True,
        'role': 'web_ui_proxy',
        'daemon_url': DAEMON_URL,
    })


@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
def proxy(path):
    url = _target_url(path)
    body = None if request.method in _BODYLESS_METHODS else request.get_data()
    req = urllib.request.Request(
        url,
        data=body,
        method=request.method,
        headers=_proxy_headers(),
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as exc:
        return Response(exc.read(), status=exc.code, headers=_response_headers(exc))
    except Exception as exc:
        return _daemon_unavailable(path, exc)

    def generate():
        try:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            resp.close()

    return Response(generate(), status=resp.status, headers=_response_headers(resp), direct_passthrough=True)


if __name__ == '__main__':
    print(f'[WEB] ds_camera UI proxy listening on {HOST}:{PORT}', flush=True)
    print(f'[WEB] proxy target: {DAEMON_URL}', flush=True)
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
