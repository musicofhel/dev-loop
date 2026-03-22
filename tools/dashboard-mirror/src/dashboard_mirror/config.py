"""Shared configuration for dashboard-mirror."""

from __future__ import annotations

import os
from pathlib import Path


OO_URL = os.environ.get("OPENOBSERVE_URL", "http://localhost:5080")
OO_USER = os.environ.get("OPENOBSERVE_USER", "admin@dev-loop.local")
OO_PASS = os.environ.get("OPENOBSERVE_PASS", "devloop123")
OO_ORG = os.environ.get("OPENOBSERVE_ORG", "default")
OUTPUT_DIR = Path(os.environ.get("DM_OUTPUT", "./output"))
CONFIG_DIR = Path(os.environ.get("DM_CONFIG_DIR", os.path.expanduser("~/dev-loop/config/dashboards")))

# Time ranges to capture (label, OO relativeTimePeriod value)
TIME_RANGES = [
    ("30d", "30d"),
    ("7d", "7d"),
    ("1h", "1h"),
]
