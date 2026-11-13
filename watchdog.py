#!/usr/bin/env python3
"""Runtime watchdog for the ds_camera inference daemon.

It monitors the daemon's /status endpoint and restarts only the inference
pipeline when frame progress stalls.  If the daemon itself is unreachable, an
optional restart command can be used by script/systemd deployments.
"""

import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DAEMON_URL = os.environ.get('DS_CAMERA_DAEMON_URL', 'http://127.0.0.1:5122').rstrip('/')
INTERVAL_SEC = float(os.environ.get('DS_CAMERA_WATCHDOG_INTERVAL_SEC', '5'))
STALL_SEC = float(os.environ.get('DS_CAMERA_WATCHDOG_STALL_SEC', '30'))
STARTUP_GRACE_SEC = float(os.environ.get('DS_CAMERA_WATCHDOG_STARTUP_GRACE_SEC', '600'))
STOP_TIMEOUT_SEC = float(os.environ.get('DS_CAMERA_WATCHDOG_STOP_TIMEOUT_SEC', '25'))
COOLDOWN_SEC = float(os.environ.get('DS_CAMERA_WATCHDOG_COOLDOWN_SEC', '90'))
FAIL_LIMIT = int(os.environ.get('DS_CAMERA_WATCHDOG_FAIL_LIMIT', '4'))
RESTART_CMD = os.environ.get('DS_CAMERA_WATCHDOG_RESTART_CMD', '').strip()
TCP_CHECKS = [
    item.strip()
    for item in os.environ.get('DS_CAMERA_WATCHDOG_TCP_CHECKS', '').split(',')
    if item.strip()
]


def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'{ts} [watchdog] {msg}', flush=True)


def request_json(path, method='GET', payload=None, timeout=6):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(
        DAEMON_URL + path,
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    if not body:
        return {}
    return json.loads(body.decode('utf-8'))


def progress_signature(status):
    streams = status.get('streams') or []
    sig = []
    for stream in streams:
        sig.append((
            int(stream.get('idx', len(sig)) or 0),
            int(stream.get('loop_count', 0) or 0),
            int(stream.get('frame_idx', 0) or 0),
            float(stream.get('total_elapsed_sec', 0.0) or 0.0),
            int(stream.get('total_count', 0) or 0),
        ))
    return tuple(sig)


def has_any_frame(status):
    for stream in status.get('streams') or []:
        if int(stream.get('frame_idx', 0) or 0) > 0:
            return True
        if float(stream.get('total_elapsed_sec', 0.0) or 0.0) > 0:
            return True
    return False


def build_start_payload(status):
    streams = status.get('streams') or []
    conf_list = [
        float(stream.get('conf', 0.30) if stream.get('conf') is not None else 0.30)
        for stream in streams
    ]
    payload = {
        'show_stream': bool(status.get('show_stream', False)),
        'use_peoplenet': bool(status.get('use_peoplenet', True)),
        'tracker': str(status.get('tracker_key', 'nvsort') or 'nvsort'),
        'infer_interval': int(status.get('infer_interval', 0) or 0),
        'infer_w': int(status.get('infer_w', 640) or 640),
        'infer_h': int(status.get('infer_h', 640) or 640),
        'mux_w': int(status.get('mux_w_override', 0) or 0),
        'mux_h': int(status.get('mux_h_override', 0) or 0),
    }
    if conf_list:
        payload['conf'] = conf_list[0]
        payload['conf_list'] = conf_list
    return payload


def wait_until_stopped():
    deadline = time.time() + STOP_TIMEOUT_SEC
    last = {}
    while time.time() < deadline:
        try:
            last = request_json('/status', timeout=4)
            if not last.get('running') and not last.get('stopping'):
                return last
        except Exception:
            return last
        time.sleep(0.7)
    return last


def restart_pipeline(status, reason):
    log(f'pipeline restart requested: {reason}')
    payload = build_start_payload(status)
    try:
        request_json('/stop', method='POST', timeout=8)
    except Exception as exc:
        log(f'/stop failed: {type(exc).__name__}: {exc}')
    stopped_status = wait_until_stopped()
    if stopped_status.get('streams'):
        payload = build_start_payload({**status, **stopped_status})
    try:
        result = request_json('/start', method='POST', payload=payload, timeout=20)
    except Exception as exc:
        log(f'/start failed: {type(exc).__name__}: {exc}')
        return False
    if result.get('error'):
        log(f'/start returned error: {result.get("error")}')
        return False
    log('pipeline restart done')
    return True


def restart_daemon(reason):
    if not RESTART_CMD:
        log(f'daemon restart skipped; no DS_CAMERA_WATCHDOG_RESTART_CMD ({reason})')
        return False
    log(f'daemon restart command: {RESTART_CMD} ({reason})')
    try:
        subprocess.run(shlex.split(RESTART_CMD), cwd=BASE_DIR, check=False, timeout=60)
        return True
    except Exception as exc:
        log(f'daemon restart command failed: {type(exc).__name__}: {exc}')
        return False


def check_tcp_targets():
    for target in TCP_CHECKS:
        if ':' not in target:
            log(f'tcp check invalid target: {target}')
            continue
        host, port_text = target.rsplit(':', 1)
        try:
            with socket.create_connection((host, int(port_text)), timeout=2):
                pass
        except Exception as exc:
            log(f'tcp check failed {target}: {type(exc).__name__}: {exc}')


def main():
    log(f'started daemon={DAEMON_URL} interval={INTERVAL_SEC}s stall={STALL_SEC}s')
    if TCP_CHECKS:
        log(f'tcp checks enabled: {", ".join(TCP_CHECKS)}')
    last_sig = None
    last_progress_at = time.time()
    running_since = None
    consecutive_failures = 0
    last_action_at = 0.0
    last_tcp_check_at = 0.0
    unexpected_stop_since = None

    while True:
        now = time.time()
        if TCP_CHECKS and now - last_tcp_check_at >= max(30.0, INTERVAL_SEC):
            check_tcp_targets()
            last_tcp_check_at = now

        try:
            status = request_json('/status', timeout=6)
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            log(f'status failure {consecutive_failures}/{FAIL_LIMIT}: {type(exc).__name__}: {exc}')
            if consecutive_failures >= FAIL_LIMIT and now - last_action_at >= COOLDOWN_SEC:
                if restart_daemon('status unreachable'):
                    last_action_at = now
                consecutive_failures = 0
            time.sleep(INTERVAL_SEC)
            continue

        if not status.get('running'):
            running_since = None
            last_sig = None
            last_progress_at = now
            if status.get('desired_running') and not status.get('stopping'):
                if unexpected_stop_since is None:
                    unexpected_stop_since = now
                    log('inference stopped unexpectedly; waiting for recovery')
                elif (now - unexpected_stop_since >= INTERVAL_SEC
                      and now - last_action_at >= COOLDOWN_SEC):
                    if restart_daemon('desired inference is not running'):
                        last_action_at = now
                    unexpected_stop_since = now
            else:
                unexpected_stop_since = None
            time.sleep(INTERVAL_SEC)
            continue

        unexpected_stop_since = None
        if running_since is None:
            running_since = now
            last_progress_at = now
            last_sig = progress_signature(status)
            log('inference running; watching progress')
            time.sleep(INTERVAL_SEC)
            continue

        sig = progress_signature(status)
        if sig and sig != last_sig:
            last_sig = sig
            last_progress_at = now
            time.sleep(INTERVAL_SEC)
            continue

        startup_window = not has_any_frame(status) and (now - running_since) < STARTUP_GRACE_SEC
        stalled_for = now - last_progress_at
        if not startup_window and stalled_for >= STALL_SEC and now - last_action_at >= COOLDOWN_SEC:
            reason = f'no frame progress for {int(stalled_for)}s'
            ok = restart_pipeline(status, reason)
            last_action_at = now
            running_since = None
            last_sig = None
            last_progress_at = now
            if not ok:
                restart_daemon('pipeline restart failed')

        time.sleep(INTERVAL_SEC)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log('stopped by user')
        sys.exit(0)
