#!/usr/bin/env bash
# Headless build: drop the Rewind-viewer client and the per-tick debug/timing output that the
# repository's CMakeLists.txt enables by default.
set -e
sed -i -e '/REWIND_VIEWER/d' -e '/DEBUG_ENABLED/d' -e '/TIMING_ENABLED/d' CMakeLists.txt
grep -n 'add_definitions' CMakeLists.txt || true
