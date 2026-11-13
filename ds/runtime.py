#!/usr/bin/env python3
"""Runtime loader for the refactored DeepStream pipeline.

The pipeline still runs in one shared module namespace because GStreamer probes,
Flask route decorators, and process-wide state are tightly coupled. The code is
split into ordered parts so feature edits are localized without changing runtime
behavior.
"""

import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PARTS_DIR = os.path.join(PACKAGE_DIR, 'parts')
PART_FILES = [
    '00_bootstrap.py',
    '10_common_state.py',
    '20_media_yolo.py',
    '40_speed_counting.py',
    '50_state_config.py',
    '55_comm_watchdog.py',
    '60_probes_sources.py',
    '70_pipeline.py',
    '80_routes.py',
    '90_lifecycle.py',
]


def _load_runtime_parts():
    for part_name in PART_FILES:
        part_path = os.path.join(PARTS_DIR, part_name)
        with open(part_path, 'r', encoding='utf-8') as f:
            code = f.read()
        exec(compile(code, part_path, 'exec'), globals(), globals())


_load_runtime_parts()
