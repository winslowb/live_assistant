#!/usr/bin/env python3
import os, sys, runpy
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Prefer a local main if present in this project root
local_main = HERE / 'live_assistant_main.py'
if local_main.is_file():
    runpy.run_path(str(local_main), run_name='__main__')
    sys.exit(0)

# Fallback: use the parent directory’s main (current repository layout)
parent_main = (HERE.parent / 'live_assistant_main.py')
if parent_main.is_file():
    runpy.run_path(str(parent_main), run_name='__main__')
    sys.exit(0)

sys.stderr.write(f"Live Assistant entry script not found at {local_main} or {parent_main}\n")
sys.exit(1)

