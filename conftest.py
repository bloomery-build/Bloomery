"""Make the top-level bloomery module importable when running pytest.

Bloomery is a single-module project that is not necessarily installed
into the environment under test, so put the repository root on sys.path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
