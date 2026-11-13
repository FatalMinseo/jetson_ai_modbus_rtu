#!/usr/bin/env python3
"""DeepStream inference daemon entry point.

This process owns the camera, DeepStream pipeline, ROI/counting state, local
MJPEG preview, and Modbus background services.  The public Flask UI can
restart independently and proxy requests back to this daemon.
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Internal-only defaults.  Public access should go through web_ui.py on PORT 5022.
os.environ.setdefault('HOST', '127.0.0.1')
os.environ.setdefault('PORT', '5122')
os.environ.setdefault('PYTHONUNBUFFERED', '1')

from ds import run


if __name__ == '__main__':
    run()
