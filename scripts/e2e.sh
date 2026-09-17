#!/bin/sh
# The browser regression suite (tests/e2e), on a stack of its own: a mock NVR and two agents
# in a separate compose project with nothing published. It cannot touch your running wall,
# your NVR or your settings, and it is removed again afterwards, pass or fail.
#
#   sh scripts/e2e.sh                     everything
#   sh scripts/e2e.sh -k onboarding -x    pytest arguments pass straight through
#
# Needs only Docker: the browser, Python and the tests all run in containers.
set -eu
cd "$(dirname "$0")/.."

project=viewport-e2e
compose() { docker compose -p "$project" --profile test "$@"; }

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "--- logs of the e2e stack ---"
    compose logs --no-color --tail=80 e2e-agent e2e-agent-fresh e2e-go2rtc e2e-nvr 2>/dev/null || true
  fi
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT INT TERM

mkdir -p e2e-output
rm -f e2e-output/failed-*.png          # screenshots of an earlier run's failures would mislead
# Every image the suite runs, from the current source: a stale one would test old code.
compose build e2e e2e-agent e2e-agent-fresh e2e-nvr
if [ "$#" -gt 0 ]; then
  compose run --rm e2e -q "$@"
else
  compose run --rm e2e
fi
