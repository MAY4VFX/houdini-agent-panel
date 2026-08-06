#!/bin/sh
set -eu

version="${1:-20.5}"
installed_probe=0
installed_ui_probe=0
case "$version" in
  20.5)
    houdini_bin="/Applications/Houdini/Houdini20.5.445/Houdini FX 20.5.445.app/Contents/MacOS/houdini"
    ;;
  20.5-installed)
    houdini_bin="/Applications/Houdini/Houdini20.5.445/Houdini FX 20.5.445.app/Contents/MacOS/houdini"
    installed_ui_probe=1
    ;;
  21)
    houdini_bin="/Applications/Houdini/Houdini21.0.792/Houdini FX 21.0.792.app/Contents/MacOS/houdini"
    ;;
  21-installed)
    houdini_bin="/Applications/Houdini/Houdini21.0.792/Houdini FX 21.0.792.app/Contents/MacOS/houdini"
    installed_probe=1
    ;;
  22)
    houdini_bin="/Applications/Houdini/Houdini22.0.368/Houdini FX 22.0.368.app/Contents/MacOS/houdini"
    ;;
  *)
    echo "usage: $0 20.5|20.5-installed|21|21-installed|22" >&2
    exit 2
    ;;
esac

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
probe_dir=$(mktemp -d /tmp/hap-ui-probe.XXXXXX)
export HAP_DATA_DIR="$probe_dir"
export HOUDINI_PATH="$repo_dir/tests/houdini_ui_probe;&"
if [ "$installed_probe" -eq 1 ] || [ "$installed_ui_probe" -eq 1 ]; then
  # Read the real package JSON, but never the owner's panel settings. This
  # proves a fresh Houdini version sees both plugin trees without risking
  # autostarting their configured agent.
  unset HOUDINI_USER_PREF_DIR
  unset PYTHONPATH
  export HAP_USE_INSTALLED_PANEL=1
  if [ "$installed_probe" -eq 1 ]; then
    export HAP_EXPECT_FX=1
  fi
else
  export HOUDINI_USER_PREF_DIR="$probe_dir/houdini__HVER__"
  export PYTHONPATH="$repo_dir/python"
fi

probe_pid=""
cleanup() {
  if [ -n "$probe_pid" ] && kill -0 "$probe_pid" 2>/dev/null; then
    kill -TERM "$probe_pid" 2>/dev/null || true
    # A failed startup script can leave Houdini's renderer threads alive.
    # This PID belongs exclusively to the throwaway probe launched above.
    kill -KILL "$probe_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

"$houdini_bin" -foreground -geometry 900x700+0+0 &
probe_pid=$!

if [ "$installed_probe" -eq 1 ]; then
  result_file="$probe_dir/result.json"
  attempts=0
  while [ ! -f "$result_file" ] && [ "$attempts" -lt 400 ]; do
    sleep 0.1
    attempts=$((attempts + 1))
  done
  if [ ! -f "$result_file" ]; then
    echo "installed probe did not produce result.json" >&2
    exit 1
  fi
  port=$(sed -n '/"port":/s/[^0-9]//gp' "$result_file" | head -1)
  if [ -z "$port" ]; then
    echo "fxhoudinimcp did not publish a port" >&2
    exit 1
  fi
  health=$(curl -fsS --max-time 3 --data-urlencode 'json=["mcp.health", [], {}]' \
    "http://127.0.0.1:$port/api")
  case "$health" in
    *'"status": "ok"'*|*'"status":"ok"'*)
      echo "H21_FX_LIVENESS_OK port=$port $health"
      ;;
    *)
      echo "unexpected fx health response: $health" >&2
      exit 1
      ;;
  esac
fi
wait "$probe_pid"
