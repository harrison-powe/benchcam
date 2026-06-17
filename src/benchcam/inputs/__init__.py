"""Input backends for BenchCam.

v0 ships a single line-based keyboard backend (terminal prompt). Future inputs
(for example GPIO buttons on a Raspberry Pi) would live here too, but BenchCam
only logs the events they produce; it never drives moving hardware.
"""

from __future__ import annotations

from .keyboard_input import run_interactive_loop

__all__ = ["run_interactive_loop"]
