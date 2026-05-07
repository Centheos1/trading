"""Allow `python -m wave` to run the Wave CLI.

Separating this from __init__.py avoids the runpy conflict that arises because
Python's standard library also has a top-level 'wave' module (for WAV audio).
"""
import sys
from wave.wave_cli import run_cli

sys.exit(run_cli())
