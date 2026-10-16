#!/usr/bin/env python
"""Launcher for the midikit CLI so it works from any directory.

    python midi.py info song.mid
    python midi.py apply song.mid --plan edits.json -o out.mid
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from midikit.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
