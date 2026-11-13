import os
import ast
import json
import io
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import ds_modbus_rtu_comm as protocol
from detection_fusion import cross_model_suppressed_ids
import modbus_daemon
import watchdog


class PersonDetectionFusionTest(unittest.TestCase):
    @staticmethod
    def candidate(identifier, component_id, confidence, box):
        return {
            "id": identifier,
            "component_id": component_id,
            "confidence": confidence,
            "box": box,
        }

    def test_overlapping_cross_model_person_keeps_higher_confidence(self):
        candidates = [
            self.candidate("traffic", 1, 0.82, (10, 10, 110, 210)),
            self.candidate("peoplenet", 2, 0.71, (14, 12, 108, 208)),
        ]
        self.assertEqual(cross_model_suppressed_ids(candidates), {"peoplenet"})

    def test_independent_people_from_both_models_are_retained(self):
        candidates = [
            self.candidate("traffic", 1, 0.75, (10, 10, 80, 180)),
            self.candidate("peoplenet", 2, 0.90, (200, 20, 270, 190)),
        ]
        self.assertEqual(cross_model_suppressed_ids(candidates), set())

    def test_same_model_candidates_are_not_cross_suppressed(self):
        candidates = [
            self.candidate("a", 1, 0.80, (10, 10, 100, 200)),
            self.candidate("b", 1, 0.70, (12, 12, 98, 198)),
        ]
        self.assertEqual(cross_model_suppressed_ids(candidates), set())

    def test_contained_cross_model_box_is_treated_as_duplicate(self):
        candidates = [
            self.candidate("traffic", 1, 0.65, (10, 10, 110, 210)),
            self.candidate("peoplenet", 2, 0.85, (25, 35, 95, 190)),
        ]
        self.assertEqual(cross_model_suppressed_ids(candidates), {"traffic"})


class ModbusDaemonStateTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_state_file = modbus_daemon.STATE_FILE
        self.old_settings_file = modbus_daemon.SETTINGS_FILE
        modbus_daemon.STATE_FILE = os.path.join(self.tempdir.name, "state.json")
        modbus_daemon.SETTINGS_FILE = os.path.join(self.tempdir.name, "operation_settings.json")
        with open(modbus_daemon.SETTINGS_FILE, "w", encoding="utf-8") as handle:
            json.dump({
                "version": 1,
                "modbus_rtu": {"slave_id": 101},
                "roi_defaults": {"ref_distance_m": 10.0, "confidence_threshold": 0.30},
                "startup": {"desired_running": True, "camera_device": "/dev/video0"},
            }, handle)
        self.daemon = modbus_daemon.ModbusDaemon(start_serial=False)
        self.daemon._post_infer_clear = lambda _start: None

    def tearDown(self):
        modbus_daemon.STATE_FILE = self.old_state_file
        modbus_daemon.SETTINGS_FILE = self.old_settings_file
        self.tempdir.cleanup()

    def test_event_is_idempotent_persistent_and_clearable(self):
        event = {
            "event_id": "test-boot:1",
            "class_name": "car",
            "direction": "상행",
            "speed_kph": 72.0,
            "recorded_at": time.time(),
        }
        self.assertEqual(self.daemon.record_event(event), (True, "recorded"))
        self.assertEqual(self.daemon.record_event(event), (True, "duplicate"))
        registers = self.daemon.snapshot()["registers"]
        self.assertEqual(registers[0x1203], 1)
        self.assertEqual(registers[0x1204], 720)

        reloaded = modbus_daemon.ModbusDaemon(start_serial=False)
        reloaded._post_infer_clear = lambda _start: None
        self.assertEqual(reloaded.snapshot()["registers"][0x1203], 1)
        self.assertEqual(reloaded.handle_write(0x1203, [0, 0]), {})
        self.assertEqual(reloaded.snapshot()["registers"][0x1203], 0)

    def test_command_requires_new_sequence_and_is_persisted(self):
        result = self.daemon.handle_write(protocol.DEVICE_COMMAND_ADDRESS, [3, 41])
        self.assertTrue(callable(result["post_response_action"]))
        self.assertFalse(self.daemon.handle_write(protocol.DEVICE_COMMAND_ADDRESS, [3, 41]))
        self.assertFalse(self.daemon.handle_write(protocol.DEVICE_COMMAND_ADDRESS, [3]))
        reloaded = modbus_daemon.ModbusDaemon(start_serial=False)
        self.assertEqual(reloaded.command_sequence, 41)

    def test_inference_restart_forces_saved_runtime_to_resume(self):
        with open(modbus_daemon.SETTINGS_FILE, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
        settings["startup"]["desired_running"] = False
        with open(modbus_daemon.SETTINGS_FILE, "w", encoding="utf-8") as handle:
            json.dump(settings, handle)

        with mock.patch("modbus_daemon.subprocess.run") as run:
            self.daemon._run_control(
                ["systemctl", "restart", "inferd.service"],
                "inference restart",
                resume_inference=True,
            )

        with open(modbus_daemon.SETTINGS_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertTrue(saved["startup"]["desired_running"])
        run.assert_called_once_with(
            ["systemctl", "restart", "inferd.service"],
            check=True,
            timeout=20,
        )

    def test_ui_clear_can_reset_all_authoritative_slots(self):
        now = time.time()
        self.daemon.record_event({
            "event_id": "test-boot:person",
            "class_name": "person",
            "direction": "상행",
            "speed_kph": 0,
            "recorded_at": now,
        })
        self.daemon.record_event({
            "event_id": "test-boot:vehicle",
            "class_name": "truck",
            "direction": "하행",
            "speed_kph": 55,
            "recorded_at": now,
        })
        self.daemon.clear_slots(None)
        registers = self.daemon.snapshot()["registers"]
        for base in protocol.CUMULATIVE_SLOT_BASES:
            self.assertEqual(registers[base + 3], 0)

    def test_cumulative_http_snapshot_matches_menu_10_registers(self):
        self.daemon.record_event({
            "event_id": "test-boot:menu10",
            "class_name": "vehicle",
            "direction": "하행",
            "speed_kph": 63.5,
            "recorded_at": time.time(),
        })
        payload = self.daemon.cumulative_snapshot()
        vehicle_down = payload["cumulative_slots"][3]
        registers = self.daemon.snapshot()["registers"]
        base = protocol.CUMULATIVE_SLOT_BASES[3]
        self.assertEqual((vehicle_down["object_no"], vehicle_down["direction"]), (1, 2))
        self.assertEqual(vehicle_down["count"], registers[base + 3])
        self.assertEqual(round(vehicle_down["average_speed_kph"] * 10), registers[base + 4])

    def test_master_clear_is_mirrored_to_inferd(self):
        called = []
        completed = threading.Event()

        def fake_sync(start):
            called.append(start)
            completed.set()

        self.daemon._post_infer_clear = fake_sync
        self.assertEqual(self.daemon.handle_write(0x1203, [0, 0]), {})
        self.assertTrue(completed.wait(1.0))
        self.assertEqual(called, [0x1203])

    def test_config_update_preserves_latest_startup_settings(self):
        with open(modbus_daemon.SETTINGS_FILE, "r", encoding="utf-8") as handle:
            latest = json.load(handle)
        latest["startup"]["show_stream"] = False
        with open(modbus_daemon.SETTINGS_FILE, "w", encoding="utf-8") as handle:
            json.dump(latest, handle)

        self.assertTrue(self.daemon.apply_config(
            protocol.ROI_REFERENCE_DISTANCE_ADDRESS,
            [1550],
        ))
        with open(modbus_daemon.SETTINGS_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertTrue(saved["startup"]["desired_running"])
        self.assertFalse(saved["startup"]["show_stream"])
        self.assertEqual(saved["roi_defaults"]["ref_distance_m"], 15.5)

    def test_master_confidence_updates_modbusd_and_notifies_inferd(self):
        calls = []
        self.daemon._post_infer_config = lambda start, values: calls.append((start, values))
        self.assertEqual(self.daemon.handle_write(
            protocol.BBOX_CONFIDENCE_ADDRESS,
            [35],
        ), {})
        self.assertEqual(self.daemon.confidence, 0.35)
        self.assertEqual(calls, [(protocol.BBOX_CONFIDENCE_ADDRESS, [35])])


class ProtocolAcknowledgementTest(unittest.TestCase):
    def test_serial_disconnect_retries_are_log_rate_limited(self):
        runtime = protocol.ModbusRTUSlaveRuntime(slave_id=101)
        output = io.StringIO()
        error = OSError("device absent")
        with redirect_stdout(output):
            runtime._log_serial_disconnect(error)
            runtime._log_serial_disconnect(error)
        self.assertEqual(output.getvalue().count("serial disconnected"), 1)
        self.assertEqual(runtime._suppressed_disconnect_logs, 1)

    def test_broadcast_address_is_silent_and_cannot_write(self):
        writes = []
        runtime = protocol.ModbusRTUSlaveRuntime(
            slave_id=101,
            write_handler=lambda start, values: writes.append((start, values)) or {},
        )
        request = protocol.append_crc(
            bytes((0, protocol.FC_WRITE_MULTIPLE_REGISTERS))
            + protocol.DEVICE_COMMAND_ADDRESS.to_bytes(2, "big")
            + (2).to_bytes(2, "big")
            + bytes((4,))
            + (4).to_bytes(2, "big")
            + (77).to_bytes(2, "big")
        )
        self.assertEqual(runtime.handle_frame(request), b"")
        self.assertEqual(writes, [])
        self.assertIsNone(runtime._post_response_action)

    def test_control_action_is_deferred_until_after_fc16_ack(self):
        called = []
        runtime = protocol.ModbusRTUSlaveRuntime(
            slave_id=101,
            write_handler=lambda _start, _values: {
                "post_response_action": lambda: called.append(True),
            },
        )
        request = protocol.append_crc(
            bytes((101, protocol.FC_WRITE_MULTIPLE_REGISTERS))
            + protocol.DEVICE_COMMAND_ADDRESS.to_bytes(2, "big")
            + (2).to_bytes(2, "big")
            + bytes((4,))
            + (3).to_bytes(2, "big")
            + (99).to_bytes(2, "big")
        )
        response = runtime.handle_frame(request)
        self.assertTrue(response)
        self.assertEqual(called, [])
        self.assertTrue(callable(runtime._post_response_action))


class WatchdogRestartTest(unittest.TestCase):
    def test_restart_payload_defaults_to_nvsort(self):
        payload = watchdog.build_start_payload({})
        self.assertEqual(payload["tracker"], "nvsort")
        self.assertTrue(payload["use_peoplenet"])

    def test_restart_payload_preserves_runtime_options(self):
        payload = watchdog.build_start_payload({
            "show_stream": True,
            "use_peoplenet": True,
            "tracker_key": "nvdcf_counting",
            "infer_interval": 2,
            "infer_w": 1280,
            "infer_h": 1280,
            "mux_w_override": 1920,
            "mux_h_override": 1080,
            "streams": [{"conf": 0.35}],
        })
        self.assertTrue(payload["use_peoplenet"])
        self.assertEqual(payload["tracker"], "nvdcf_counting")
        self.assertEqual(payload["conf_list"], [0.35])
        self.assertEqual((payload["infer_w"], payload["infer_h"]), (1280, 1280))
        self.assertEqual((payload["mux_w"], payload["mux_h"]), (1920, 1080))


class OfflineOperationTest(unittest.TestCase):
    def test_video_selection_thumbnail_does_not_allocate_nvmm(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(base_dir, "ds", "parts", "20_media_yolo.py"),
            encoding="utf-8",
        ) as handle:
            media = handle.read()
        first_frame = media.split("def _get_first_frame_jpeg", 1)[1].split(
            "def _make_small_thumb", 1
        )[0]
        self.assertIn("subprocess.run", first_frame)
        self.assertIn("'-c:v', 'mjpeg'", first_frame)
        self.assertIn("_FIRST_FRAME_CACHE", first_frame)
        self.assertNotIn("Gst.parse_launch", first_frame)
        self.assertNotIn("nvvideoconvert", first_frame)

    def test_services_do_not_wait_for_network_online(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base_dir, "install_systemd.sh"), encoding="utf-8") as handle:
            unit_installer = handle.read()
        self.assertNotIn("network-online.target", unit_installer)
        self.assertIn("RestartSec=8", unit_installer)

    def test_browser_preview_recovery_does_not_restart_inference(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base_dir, "templates", "bench.html"), encoding="utf-8") as handle:
            page = handle.read()
        self.assertNotIn("영상 프레임 12초 정체", page)
        self.assertIn("_resetStreamForRestart()", page)

    def test_on_device_preview_defaults_to_canvas_mjpeg_for_exhibition(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base_dir, "templates", "bench.html"), encoding="utf-8") as handle:
            page = handle.read()
        with open(os.path.join(base_dir, "ds", "parts", "80_routes.py"), encoding="utf-8") as handle:
            routes = handle.read()
        with open(os.path.join(base_dir, "ds", "parts", "70_pipeline.py"), encoding="utf-8") as handle:
            pipeline = handle.read()
        with open(os.path.join(base_dir, "ds", "parts", "00_bootstrap.py"), encoding="utf-8") as handle:
            bootstrap = handle.read()
        with open(os.path.join(base_dir, "install_systemd.sh"), encoding="utf-8") as handle:
            installer = handle.read()
        self.assertIn("streamImg.src = `/stream.mjpg?session=", page)
        self.assertIn("html{background:#111}", page)
        self.assertIn("display:none;position:fixed;inset:0;background:transparent", page)
        self.assertIn("async function openVideoPicker()", page)
        self.assertIn("streamImg.removeAttribute('src')", page)
        self.assertIn("await _autoCameraDisconnect()", page)
        self.assertIn("function _scheduleRoiRedraw(previousDirtyRect = null)", page)
        self.assertIn("_roiRedrawRaf = requestAnimationFrame", page)
        self.assertIn("const ROI_REDRAW_INTERVAL_MS = 1000 / 30", page)
        self.assertIn("performance.now() - _roiLastRedrawAt", page)
        self.assertIn("function _roiInteractiveDirtyRect()", page)
        self.assertIn("redrawROI(dirtyRect)", page)
        self.assertIn("ctx.rect(x, y, w, h)", page)
        self.assertIn("transition:none!important;animation:none!important", page)
        self.assertIn(".top-bar,.card,.modal-box,#stream-wrap{contain:paint}", page)
        self.assertIn("if (sizeChanged)", page)
        self.assertIn("overlay.replaceChildren()", page)
        self.assertNotIn("if (d.running && showStream && points.length && closed) redrawROI();", page)
        self.assertIn("function _captureCurrentPreviewFrame()", page)
        self.assertIn("startupHoldFrame = _captureCurrentPreviewFrame()", page)
        self.assertIn('id="stream-hold"', page)
        self.assertIn('id="stream-frame-canvas"', page)
        self.assertNotIn('id="stream-frame-canvas-next"', page)
        self.assertIn("function _preserveCurrentPreviewFrame()", page)
        self.assertIn("function _paintDisplayFrame(source, width, height)", page)
        self.assertIn("const nextCanvas = streamFrameCanvasNext", page)
        self.assertIn("function _startVideoFramePump(vid, token, onFirstFrame)", page)
        self.assertIn("_paintDisplayFrame(vid, vid.videoWidth, vid.videoHeight)", page)
        self.assertIn("async function _runCanvasMjpeg", page)
        self.assertIn("createImageBitmap(new Blob([jpeg]", page)
        self.assertIn("_paintDisplayFrame(bitmap, bitmap.width, bitmap.height)", page)
        self.assertIn("fetch('/last_frame.jpg?t='", page)
        self.assertIn("stopStreamOutput(false)", page)
        self.assertIn("body.stream-fullscreen-monitor #roi-canvas", page)
        self.assertIn("body.stream-fullscreen-monitor #stream-wrap{contain:paint;isolation:isolate}", page)
        self.assertNotIn("body.stream-fullscreen-monitor #stream-wrap{contain:none;isolation:auto}", page)
        self.assertIn("body:not(.inference-running) .btn:hover:not(:disabled)", page)
        self.assertIn("document.body.classList.toggle('inference-running', running)", page)
        self.assertIn("function _setTextIfChanged(elementOrId, value)", page)
        self.assertIn("function _setStyleIfChanged(elementOrId, property, value)", page)
        self.assertIn("expanded && running", page)
        self.assertNotIn("transform:translateZ(0);backface-visibility:hidden", page)
        self.assertNotIn('onmousemove="moveCctvTabTip(event)"', page)
        self.assertNotIn('onmousemove="moveResultEntryTooltip(event)"', page)
        self.assertIn("DeepStream output already contains ROI/BBox/labels", page)
        self.assertIn("_preserveCurrentPreviewFrame();\n  streamImg.removeAttribute('src')", page)
        self.assertIn("let _mjpegRetryTimer = null", page)
        self.assertIn("streamImg.onerror = () =>", page)
        self.assertIn("streamImg.style.opacity = '0'", page)
        self.assertIn("streamImg.style.opacity = '1'", page)
        self.assertIn("if (!res.ok) throw new Error(`status HTTP ${res.status}`)", page)
        self.assertIn("statusRecovered && _streamMode !== 'none'", page)
        self.assertIn("_statusUnavailable = true", page)
        self.assertIn('id="btn-reset-counts"', page)
        self.assertIn("fetch('/reset_counts'", page)
        self.assertNotIn("confirm('모든 채널의 누적 통행량", page)
        self.assertIn('id="reset-counts-modal"', page)
        self.assertIn("function confirmResetCumulativeData()", page)
        self.assertIn("_updateLiveCountsTable({}, 0)", page)
        self.assertIn("@app.route('/stream.mjpg')", routes)
        self.assertIn("b'Content-Length: '", routes)
        self.assertIn("@app.route('/last_frame.jpg')", routes)
        self.assertIn("@app.route('/reset_counts'", routes)
        self.assertIn("new RTCPeerConnection(rtcConfig)", page)
        self.assertIn("async function _startWebRtcPreview(token)", page)
        self.assertIn("async function _startMjpegFallback(token)", page)
        self.assertIn("const PREVIEW_TRANSPORT =", page)
        self.assertIn("if (PREVIEW_TRANSPORT !== 'webrtc')", page)
        self.assertIn("await _startMjpegFallback(token);", page)
        self.assertIn("iceServers: WEBRTC_STUN_SERVER", page)
        self.assertIn("const WEBRTC_STALL_TIMEOUT_MS = 2500", page)
        self.assertIn("video frame stalled → localhost MJPEG fallback", page)
        self.assertIn("void _startMjpegFallback(_streamSessionToken)", page)
        self.assertIn("@app.route('/webrtc/prepare'", routes)
        self.assertIn("@app.route('/webrtc/offer')", routes)
        self.assertIn("@app.route('/webrtc/answer'", routes)
        self.assertIn("@app.route('/webrtc/ice'", routes)
        self.assertIn("never rebuild the AI pipeline", routes)
        self.assertIn("webrtcbin", pipeline)
        self.assertIn("rtph264pay", pipeline)
        self.assertIn("max-size-buffers', 3", pipeline)
        self.assertIn("_safe_set_property(q_webrtc, 'max-size-buffers', 2)", pipeline)
        self.assertIn("_safe_set_property(q_mjpeg, 'max-size-buffers', 2)", pipeline)
        self.assertIn("mjpeg_fallback_valve", pipeline)
        self.assertIn("_safe_set_property(mjpeg_valve, 'drop', not mjpeg_has_client)", pipeline)
        self.assertIn("_set_mjpeg_fallback_enabled(True)", routes)
        self.assertIn("_set_mjpeg_fallback_enabled(False)", routes)
        self.assertIn("'DS_CAMERA_UI_PREVIEW_TRANSPORT', 'mjpeg'", bootstrap)
        self.assertIn("Environment=DS_CAMERA_UI_PREVIEW_TRANSPORT=mjpeg", installer)
        with open(os.path.join(base_dir, "ds", "parts", "70_pipeline.py"), encoding="utf-8") as handle:
            pipeline = handle.read()
        self.assertIn("DS_CAMERA_PREVIEW_FPS', '30'", pipeline)
        with open(os.path.join(base_dir, "ds", "parts", "60_probes_sources.py"), encoding="utf-8") as handle:
            sources = handle.read()
        self.assertIn("def _attach_roi_display_meta(", sources)
        self.assertIn("pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)", sources)
        self.assertIn("_attach_roi_display_meta(\n                        batch_meta, frame_meta, roi_pts, l1, l2)", sources)
        self.assertIn("CAMERA_IDLE_PREVIEW_FPS", sources)
        self.assertIn("frame_interval = 1.0 / preview_fps", sources)
        with open(os.path.join(base_dir, "ds", "parts", "10_common_state.py"), encoding="utf-8") as handle:
            common_state = handle.read()
        with open(os.path.join(base_dir, "ds", "parts", "40_speed_counting.py"), encoding="utf-8") as handle:
            speed_counting = handle.read()
        self.assertIn("DS_OSD_FONT_SIZE", common_state)
        self.assertIn("font_params.font_name = 'DejaVu Sans'", common_state)
        self.assertIn("DS_OSD_FONT_SIZE', '10'", common_state)
        self.assertIn("class_name in ('car', 'bus', 'truck')", common_state)
        self.assertIn("return 'car'", common_state)
        self.assertIn("'cls': _display_class_name", speed_counting)
        self.assertIn("'motorcycle': '#dc50ff'", page)
        self.assertIn("function displayClassName(cls)", page)
        self.assertIn("DS_CAMERA_PREVIEW_WIDTH", pipeline)
        self.assertIn("DS_CAMERA_PREVIEW_HEIGHT", pipeline)
        self.assertIn("DS_CAMERA_PREVIEW_QUALITY", pipeline)

    def test_failed_automatic_rebuild_restarts_inferd_process(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base_dir, "ds", "parts", "70_pipeline.py"), encoding="utf-8") as handle:
            pipeline = handle.read()
        with open(os.path.join(base_dir, "ds", "parts", "00_bootstrap.py"), encoding="utf-8") as handle:
            bootstrap = handle.read()
        with open(os.path.join(base_dir, "ds", "parts", "80_routes.py"), encoding="utf-8") as handle:
            routes = handle.read()
        with open(os.path.join(base_dir, "watchdog.py"), encoding="utf-8") as handle:
            watchdog_source = handle.read()
        with open(os.path.join(base_dir, "ds", "parts", "40_speed_counting.py"), encoding="utf-8") as handle:
            counting = handle.read()
        with open(os.path.join(base_dir, "ds", "parts", "60_probes_sources.py"), encoding="utf-8") as handle:
            probes = handle.read()

        self.assertIn("pipeline_started_once = False", pipeline)
        self.assertIn("def _exit_after_rebuild_failure(reason):", pipeline)
        self.assertIn("os._exit(1)", pipeline)
        self.assertIn("_exit_after_rebuild_failure('pipeline startup')", pipeline)
        self.assertIn("_safe_set_property(q, 'max-size-buffers', 3)", pipeline)
        self.assertIn("_safe_set_property(tiler, 'buffer-pool-size', 4)", pipeline)
        self.assertIn("_safe_set_property(el, 'output-buffers', 4)", bootstrap)
        self.assertIn("_output_valve = None", pipeline)
        self.assertNotIn("source.seek_simple(", pipeline)
        self.assertNotIn("pipeline.seek_simple(", pipeline)
        self.assertNotIn("pipeline.set_start_time(0)", pipeline)
        self.assertNotIn("source seek(0)", pipeline)
        self.assertIn("watchdog_stall:", pipeline)
        self.assertNotIn("모든 스트림 실제 EOS 완료 → seek(0) 요청", probes)
        self.assertIn("if _USE_NVURI_FILE_LOOP:", probes)
        self.assertIn("재시작 없이 계속", probes)
        self.assertIn("_safe_set_property(uri_decode_bin, 'file-loop', 1)", probes)
        self.assertIn("(not is_camera) and (not use_nvuri_loop)", pipeline)
        self.assertIn("'already_running': True", routes)
        self.assertIn("gst_element_send_nvevent_new_stream_reset", pipeline)
        self.assertIn("_tracker_reset_requested", probes)
        self.assertIn("_CAMERA_TRACKER_ID_RESET_THRESHOLD", probes)
        self.assertIn("_CAMERA_TRACKER_EMPTY_RESET_SEC", probes)
        self.assertIn("DS_CAMERA_TRACKER_ID_RESET_THRESHOLD', '60000'", bootstrap)
        self.assertIn("DS_CAMERA_TRACKER_EMPTY_RESET_SEC', '2.0'", bootstrap)
        self.assertIn("_tracker_max_id_seen", pipeline)
        self.assertIn("_reset_stream_tracking_state(ss, reset_frame_idx=False)", pipeline)
        self.assertIn("uridecodebin(rebuild-on-eos)", pipeline)
        self.assertIn("_st['restart_reason'] = 'all-sources-eos'", probes)
        self.assertIn("def _prune_tracking_state(", counting)
        self.assertIn("DS_TRACK_STATE_MAX", counting)
        self.assertIn("del ts['class_votes'][:-30]", counting)
        self.assertIn("del hist[:-30]", counting)
        self.assertIn("del samples[:-30]", counting)
        self.assertIn("'desired_running': desired_running", routes)
        self.assertIn("restart_daemon('desired inference is not running')", watchdog_source)

    def test_inference_stop_and_restore_sequence_is_acknowledged(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base_dir, "templates", "bench.html"), encoding="utf-8") as handle:
            bench = handle.read()
        with open(os.path.join(base_dir, "ds", "parts", "80_routes.py"), encoding="utf-8") as handle:
            routes = handle.read()
        with open(os.path.join(base_dir, "ds", "parts", "90_lifecycle.py"), encoding="utf-8") as handle:
            lifecycle = handle.read()

        self.assertIn("if (!stopRes.ok || stopBody.ok !== true)", bench)
        self.assertIn("d.desired_running === false", bench)
        self.assertNotIn("setTimeout(connectCamera, 250)", bench)
        self.assertIn("_restore_saved_state", routes)
        self.assertIn("'restore_cancelled': True", routes)
        self.assertIn("'_restore_saved_state': True", lifecycle)
        self.assertIn("result.get('restore_cancelled')", lifecycle)

    def test_gpio_overlay_installer_is_explicit_and_optional(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base_dir, "gpio_install.sh"), encoding="utf-8") as handle:
            installer = handle.read()
        with open(os.path.join(base_dir, "deploy.sh"), encoding="utf-8") as handle:
            deploy = handle.read()

        self.assertIn("tegra234-p3768-board29-gpio-output.dts", installer)
        self.assertIn("config-by-hardware.py", installer)
        self.assertIn('OVERLAY_NAME="Jetson BOARD 29 GPIO output"', installer)
        self.assertIn("Reboot is required", installer)
        self.assertNotIn("gpio_install.sh", deploy)

    def test_pyqt_webview_binds_reload_shortcuts(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base_dir, "webview.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('QShortcut(QKeySequence("Ctrl+R"), window)', source)
        self.assertIn('QShortcut(QKeySequence("F5"), window)', source)
        self.assertIn("activated.connect(reload_ui)", source)
        self.assertIn("view.reload()", source)
        self.assertIn("QTimer.singleShot(0, load_ui)", source)
        self.assertIn("page.loadProgress.connect(_on_load_progress)", source)
        self.assertIn("load_watchdog.timeout.connect(_on_load_stalled)", source)
        self.assertLess(source.index("window.setCentralWidget(view)"),
                        source.index("QTimer.singleShot(0, load_ui)"))
        self.assertIn("signal.SIGUSR1", source)
        self.assertIn("window.raise_()", source)
        self.assertIn("window.activateWindow()", source)
        self.assertIn("QWebEngineProfile.defaultProfile()", source)
        self.assertIn("QWebEngineProfile.NoPersistentCookies", source)
        self.assertIn("page.setBackgroundColor(dark_color)", source)
        self.assertIn('window.setStyleSheet("background:#111111")', source)
        self.assertIn('"--disable-gpu --disable-gpu-compositing"', source)
        self.assertIn('os.environ.setdefault("QT_QUICK_BACKEND", "software")', source)
        self.assertLess(source.index('os.environ.setdefault("QT_QUICK_BACKEND"'),
                        source.index("from PyQt5"))
        self.assertIn("page.renderProcessTerminated.connect", source)
        self.assertNotIn("from PyQt5.QtNetwork import", source)
        self.assertNotIn("network_manager =", source)

    def test_webview_sets_software_backends_before_qt_initializes(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base_dir, "webview.py"), encoding="utf-8") as handle:
            bootstrap = handle.read().split("from PyQt5", 1)[0]
        # Exercise the real bootstrap without loading Qt or needing a display.
        with mock.patch.dict(os.environ, {}, clear=True):
            exec(compile(bootstrap, "webview-bootstrap", "exec"), {})
            self.assertEqual(os.environ["QT_QUICK_BACKEND"], "software")
            self.assertEqual(os.environ["QTWEBENGINE_CHROMIUM_FLAGS"],
                             "--disable-gpu --disable-gpu-compositing")
        with mock.patch.dict(os.environ, {
            "QT_QUICK_BACKEND": "opengl",
            "QTWEBENGINE_CHROMIUM_FLAGS": "--custom-test-flag",
        }, clear=True):
            exec(compile(bootstrap, "webview-bootstrap", "exec"), {})
            self.assertEqual(os.environ["QT_QUICK_BACKEND"], "opengl")
            self.assertEqual(os.environ["QTWEBENGINE_CHROMIUM_FLAGS"],
                             "--custom-test-flag")

    def test_stalled_page_cancel_does_not_schedule_two_retries(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base_dir, "webview.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"_on_load_finished", "_on_load_stalled"}]
        timer = mock.Mock()
        active = [False]
        timer.isActive.side_effect = lambda: active[0]
        timer.start.side_effect = lambda _ms: active.__setitem__(0, True)
        timer.stop.side_effect = lambda: active.__setitem__(0, False)
        namespace = dict(retry_timer=timer, load_watchdog=mock.Mock(),
                         window=mock.Mock(), view=mock.Mock(), _load_progress=0,
                         _stalled_load_retries=0, _automatic_retry_enabled=True)
        exec(compile(ast.Module(body=functions, type_ignores=[]), "webview-load", "exec"),
             namespace)
        namespace["view"].stop.side_effect = lambda: namespace["_on_load_finished"](False)
        with redirect_stdout(io.StringIO()):
            namespace["_on_load_stalled"]()
            timer.start.assert_called_once_with(500)
            for _ in range(3):
                namespace["_on_load_stalled"]()
        self.assertFalse(namespace["_automatic_retry_enabled"])
        self.assertFalse(active[0])
        self.assertEqual(timer.start.call_count, 3)
        namespace["window"].statusBar().showMessage.assert_called_once()

    def test_desktop_launcher_starts_only_local_ui_components(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base_dir, "jetsonctl.sh"), encoding="utf-8") as handle:
            launcher = handle.read()
        with open(os.path.join(base_dir, "jetson-ai-modbus-rtu.desktop.in"), encoding="utf-8") as handle:
            desktop_entry = handle.read()
        with open(os.path.join(base_dir, "deploy.sh"), encoding="utf-8") as handle:
            deploy = handle.read()

        self.assertIn("http://127.0.0.1:", launcher)
        self.assertIn("export DISPLAY=:0", launcher)
        self.assertIn("gdm/Xauthority", launcher)
        self.assertIn('"$APP_DIR/web_ui.py"', launcher)
        self.assertIn('"$APP_DIR/webview.py"', launcher)
        self.assertIn('"$APP_DIR/web_ui.py" 9>&-', launcher)
        self.assertIn('"$APP_DIR/webview.py" 9>&-', launcher)
        self.assertIn("kill -USR1", launcher)
        self.assertIn("기존 화면을 앞으로 가져왔습니다.", launcher)
        self.assertIn("open_local_ui()", launcher)
        self.assertIn("ui)", launcher)
        self.assertIn("open_local_ui", launcher)
        self.assertIn("Exec=@APP_DIR@/jetsonctl.sh ui", desktop_entry)
        self.assertEqual(deploy.count('"$APP_DIR/install_systemd.sh"'), 1)
        self.assertNotIn("install_desktop_launcher.sh", deploy)
        with open(os.path.join(base_dir, "install_systemd.sh"), encoding="utf-8") as handle:
            installer = handle.read()
        self.assertIn("install_desktop_launcher()", installer)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS", installer)
        self.assertIn("metadata::trusted true", installer)



if __name__ == "__main__":
    unittest.main()
