"""BenchCam: local-first bench-side capture and marker logging.

BenchCam helps you record a bench session, log time-stamped markers while you
work, and keep everything in plain local files (JSON, CSV, Markdown).

Design notes:
- Local files only. No cloud sync.
- BenchCam logs markers and (optionally) drives a recorder. It never controls
  moving hardware or actuators.
"""

__version__ = "0.0.1"
