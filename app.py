#!/usr/bin/env python3
"""Entry point for the DeepStream camera pipeline."""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Keep the app self-contained when the whole ds_camera folder is moved to a
# fresh SD card.
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from ds import run


if __name__ == '__main__':
    run()
