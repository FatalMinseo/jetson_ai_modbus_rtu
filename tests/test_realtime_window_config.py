import ast
import io
import json
import os
import runpy
import tempfile
import threading
import time
import unittest
from collections import deque
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from flask import Flask

import ds_modbus_rtu_comm as protocol
import modbus_daemon
from modbus_daemon import RealtimeWindowConfig, realtime_window_seconds


BASE = Path(__file__).resolve().parents[1]


class RealtimeWindowConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'settings.json'
        self.write(500)

    def write(self, milliseconds):
        self.path.write_text(json.dumps({
            'analytics': {'window_seconds': 600, 'realtime_window_ms': milliseconds},
        }), encoding='utf-8')

    def infer_runtime(self):
        # Compile production helpers only: no DeepStream/GPU or runtime files.
        namespace = {
            'time': time, 'deque': deque, '_modbus_comm': protocol,
            '_norm_class_name': lambda value: str(value).lower(),
            '_realtime_window_config': RealtimeWindowConfig(self.path, poll_interval=0),
            '_REALTIME_WINDOW_SECONDS': .5, '_REALTIME_MEMORY_ROWS': 100,
            '_MODBUS_CUMULATIVE_COUNT_MAX': 65535,
            '_next_realtime_id': mock.Mock(return_value=1),
            '_realtime_wall_time': mock.Mock(return_value='test'),
            '_append_realtime_ndjson': mock.Mock(),
            '_variance_from_speeds': mock.Mock(return_value=0),
        }
        selected = {
            '10_common_state.py': {'_refresh_realtime_window', '_prune_rolling_window',
                                   '_rolling_window_events', '_update_vehicle_speed_trends'},
            '55_comm_watchdog.py': {'_modbus_slot_key', '_empty_modbus_slot_stats',
                                    '_build_modbus_slot_stats'},
        }
        for filename, functions in selected.items():
            tree = ast.parse((BASE / 'ds' / 'parts' / filename).read_text())
            nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in functions]
            exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec'), namespace)
        return namespace

    def daemon(self):
        with mock.patch.object(modbus_daemon, 'SETTINGS_FILE', str(self.path)), \
             mock.patch.object(modbus_daemon, 'STATE_FILE', str(self.path.parent / 'state.json')):
            daemon = modbus_daemon.ModbusDaemon(start_serial=False)
        daemon.realtime_window_config.poll_interval = 0
        return daemon

    def test_shared_default_for_older_settings(self):
        self.assertEqual(realtime_window_seconds({}), .5)

    def test_only_realtime_setting_changes_not_600_second_aggregation(self):
        self.assertEqual(realtime_window_seconds({'analytics': {
            'window_seconds': 600, 'realtime_window_ms': 750}}), .75)

    def test_reject_invalid_values(self):
        for value in (None, True, False, 0, -1, 49, 'bad', float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                realtime_window_seconds({'analytics': {'realtime_window_ms': value}})
        self.assertEqual(realtime_window_seconds({'analytics': {'realtime_window_ms': 50}}), .05)

    def test_file_edit_updates_existing_reader_without_restart(self):
        reader = RealtimeWindowConfig(self.path, poll_interval=0)
        self.assertEqual(reader.seconds(), .5)
        self.write(1000)
        self.assertEqual(reader.seconds(), 1)
        self.write(200)
        self.assertEqual(reader.seconds(), .2)

    def test_partial_write_and_invalid_setting_preserve_last_valid_value(self):
        reader = RealtimeWindowConfig(self.path, poll_interval=0)
        self.assertEqual(reader.seconds(), .5)
        self.path.write_text('{', encoding='utf-8')
        with redirect_stdout(io.StringIO()) as messages:
            self.assertEqual(reader.seconds(), .5)
            self.assertEqual(reader.seconds(), .5)
        self.assertEqual(messages.getvalue().count('[WARN]'), 1)
        self.write(-1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(reader.seconds(), .5)
        self.write(750)
        self.assertEqual(reader.seconds(), .75)

    def test_polling_does_not_read_every_video_frame(self):
        reader = RealtimeWindowConfig(self.path)
        with mock.patch('modbus_daemon.time.monotonic', return_value=10):
            self.assertEqual(reader.seconds(), .5)
        self.write(200)
        with mock.patch('modbus_daemon.time.monotonic', return_value=10.2):
            self.assertEqual(reader.seconds(), .5)
        with mock.patch('modbus_daemon.time.monotonic', return_value=10.6):
            self.assertEqual(reader.seconds(), .2)

    def test_old_environment_cannot_make_daemons_use_different_values(self):
        with mock.patch.dict(os.environ, {'DS_CAMERA_REALTIME_WINDOW_SECONDS': '0.2'}):
            self.assertEqual(RealtimeWindowConfig(self.path).seconds(), .5)
            self.assertEqual(self.daemon().realtime_window_sec, .5)

    def test_both_modbus_and_infer_filter_follow_one_live_setting(self):
        namespace = self.infer_runtime()
        stream = {'_lock': threading.RLock()}
        namespace['_streams'] = [stream]
        daemon = self.daemon()
        for milliseconds, expected in ((500, 1), (200, 0), (1000, 1)):
            self.write(milliseconds)
            # Fresh events because widening cannot recover already-pruned ones.
            stream['rolling_window'] = deque([{
                'event_time': 99.7, 'class_name': 'car', 'direction': '상행', 'speed_kph': 60}])
            daemon.realtime = deque([{'recorded_at': 99.7, 'key': (1, 1), 'speed_kph': 60}])
            with mock.patch.object(time, 'monotonic', return_value=100), \
                 mock.patch.object(time, 'time', return_value=100):
                infer_stats = namespace['_build_modbus_slot_stats'](realtime=True)
                modbus_stats = daemon._realtime_stats()
            self.assertEqual(infer_stats[(1, 1)]['count'], expected)
            self.assertEqual(modbus_stats[(1, 1)]['count'], expected)
            self.assertEqual(daemon.health()['window_duration_ms'], milliseconds)

    def test_modbus_cumulative_counts_do_not_change_on_window_edit(self):
        daemon = self.daemon()
        daemon.cumulative[(1, 1)]['count'] = 8
        self.write(200)
        daemon._realtime_stats()
        self.assertEqual(daemon.cumulative[(1, 1)]['count'], 8)

    def test_analysis_rows_follow_setting_and_reenter_warmup(self):
        namespace = self.infer_runtime()
        stream = {}
        namespace['_update_vehicle_speed_trends'](stream, 10)
        rows = namespace['_update_vehicle_speed_trends'](stream, 11)
        self.assertEqual(rows[1]['window_duration_ms'], 500)
        self.assertNotEqual(rows[1]['status'], '워밍업')
        self.write(1000)
        rows = namespace['_update_vehicle_speed_trends'](stream, 11.1)
        self.assertEqual(rows[1]['window_duration_ms'], 1000)
        self.assertEqual(rows[1]['status'], '워밍업')
        rows = namespace['_update_vehicle_speed_trends'](stream, 12.2)
        self.assertNotEqual(rows[1]['status'], '워밍업')

    def test_ui_initial_label_uses_server_setting(self):
        app = Flask('window-config-test', template_folder=str(BASE / 'templates'))
        with app.app_context():
            rendered = app.jinja_env.get_template('bench.html').render(realtime_window_ms=750)
        self.assertIn('window_duration_ms: 750', rendered)
        self.assertNotIn('window_duration_ms || 200', rendered)

    def test_importing_shared_config_does_not_start_serial_threads_or_http(self):
        with mock.patch('serial.Serial') as serial_open, \
             mock.patch('threading.Thread.start') as thread_start, \
             mock.patch('http.server.ThreadingHTTPServer') as http_server:
            namespace = runpy.run_path(str(BASE / 'modbus_daemon.py'),
                                      run_name='config_import_test')
        self.assertIn('RealtimeWindowConfig', namespace)
        serial_open.assert_not_called()
        thread_start.assert_not_called()
        http_server.assert_not_called()


if __name__ == '__main__':
    unittest.main()
