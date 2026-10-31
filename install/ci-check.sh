#!/usr/bin/env bash
# CI check of mm3-install.sh, run inside a clean Debian 12 or 13 container.
#
# It installs MM3 from the local wheels in /src/dist twice and checks what the
# script promises: users and groups, files and modes, the venv symlink and the
# kept previous venv, the unit files, no overwrite of the configuration and the
# env file, no token, and an engine that starts from the installed venv.
#
# Implements CORE-293 (Core spec 0.20, §10.2 Packaging and deployment): the
# Release 1.0 CI job runs the script on clean Debian 12 and 13 images.
#
# Usage (from the repository root, after building the wheels into dist/):
#   docker run --rm -e DEBIAN_VERSION=13 -v "$PWD:/src:ro" debian:13 bash /src/install/ci-check.sh
set -Eeuo pipefail

: "${DEBIAN_VERSION:?set DEBIAN_VERSION to 12 or 13}"
INSTALL=/src/install/mm3-install.sh
DIST=/src/dist

fail() {
  echo "ci-check: FAIL: $*" >&2
  exit 1
}
check() {
  local description=$1
  shift
  if "$@" >/dev/null 2>&1; then echo "ci-check: ok: $description"; else fail "$description"; fi
}
in_group() { id -nG "$1" | tr ' ' '\n' | grep -qx "$2"; }
not_in_group() { ! in_group "$1" "$2"; }
mode_is() { [[ $(stat -c '%a %U:%G' "$1") == "$2" ]]; }
absent() { [[ ! -e $1 ]]; }

echo "ci-check: first run on Debian $DEBIAN_VERSION"
bash "$INSTALL" --find-links "$DIST"

check "mm3 is a member of mm3-kiosk (CORE-268)" in_group mm3 mm3-kiosk
check "mm3-kiosk is not a member of mm3" not_in_group mm3-kiosk mm3
check "/etc/mm3 is 0750 mm3:mm3" mode_is /etc/mm3 "750 mm3:mm3"
check "config.yaml is 0640 mm3:mm3" mode_is /etc/mm3/config.yaml "640 mm3:mm3"
check "mm3.env is 0640 mm3:mm3 (CORE-200)" mode_is /etc/mm3/mm3.env "640 mm3:mm3"
check "/opt/mm3/venv is a symlink into venvs/" test -L /opt/mm3/venv
check "mm3 runs from the venv" /opt/mm3/venv/bin/mm3 --version
check "mm3.service is installed with Type=notify" grep -q '^Type=notify' /etc/systemd/system/mm3.service
if [[ $DEBIAN_VERSION == 13 ]]; then
  check "mm3-kiosk.service is installed on Debian 13" test -f /etc/systemd/system/mm3-kiosk.service
else
  check "no kiosk on Debian 12" absent /etc/systemd/system/mm3-kiosk.service
fi
check "no token and no state was created" absent /var/lib/mm3
check "the default configuration is valid for the service user" \
  runuser -u mm3 -- /opt/mm3/venv/bin/mm3 check-config \
  --config /etc/mm3/config.yaml --env-file /etc/mm3/mm3.env

# The engine starts from the installed venv, serves the mirror and stops on SIGTERM.
install -d -o mm3 -g mm3 /tmp/mm3-state /tmp/mm3-run
runuser -u mm3 -- env STATE_DIRECTORY=/tmp/mm3-state RUNTIME_DIRECTORY=/tmp/mm3-run \
  /opt/mm3/venv/bin/mm3 run >/tmp/mm3-run.log 2>&1 &
engine=$!
served=no
for _ in $(seq 1 40); do
  if /opt/mm3/venv/bin/python -c \
    'import urllib.request as u; u.urlopen("http://127.0.0.1:8080/", timeout=2)' 2>/dev/null; then
    served=yes
    break
  fi
  sleep 0.5
done
kill -TERM "$engine" 2>/dev/null || true
status=0
wait "$engine" || status=$?
if [[ $served != yes ]]; then
  cat /tmp/mm3-run.log >&2
  fail "the engine did not serve the mirror on 127.0.0.1:8080"
fi
# runuser reports a child ended by SIGTERM as 143; the engine's own log says whether it stopped cleanly.
if [[ $status != 0 && $status != 143 ]] || ! grep -q '"event": "engine_stopped"' /tmp/mm3-run.log; then
  cat /tmp/mm3-run.log >&2
  fail "the engine did not stop cleanly (status $status)"
fi
echo "ci-check: ok: the engine serves the mirror and stops on SIGTERM"

# Second run: a new venv, the configuration and the env file untouched.
echo "# kept by the installer" >>/etc/mm3/config.yaml
echo "KEPT=1" >>/etc/mm3/mm3.env
before=$(sha256sum /etc/mm3/config.yaml /etc/mm3/mm3.env)
first=$(readlink -f /opt/mm3/venv)
sleep 1
echo "ci-check: second run"
bash "$INSTALL" --find-links "$DIST"
after=$(sha256sum /etc/mm3/config.yaml /etc/mm3/mm3.env)
[[ $before == "$after" ]] || fail "the second run changed the configuration or the env file"
echo "ci-check: ok: the second run keeps the configuration and the env file"
second=$(readlink -f /opt/mm3/venv)
[[ $first != "$second" ]] || fail "the second run did not build a new venv"
check "the previous venv is kept" test -d "$first"
check "the new venv works" "$second/bin/mm3" --version
[[ $(find /opt/mm3/venvs -mindepth 1 -maxdepth 1 -type d | wc -l) == 2 ]] || fail "expected two venvs"

# An invalid configuration stops the update before anything is switched.
echo "not_a_key: true" >>/etc/mm3/config.yaml
sleep 1
status=0
bash "$INSTALL" --find-links "$DIST" >/tmp/mm3-bad.log 2>&1 || status=$?
[[ $status == 2 ]] || fail "an invalid configuration should exit 2, got $status"
[[ $(readlink -f /opt/mm3/venv) == "$second" ]] || fail "the venv was switched despite the invalid configuration"
[[ $(find /opt/mm3/venvs -mindepth 1 -maxdepth 1 -type d | wc -l) == 2 ]] || fail "the staged venv was not removed"
echo "ci-check: ok: an invalid configuration stops the update (exit 2), nothing switched"

echo "ci-check: all checks passed on Debian $DEBIAN_VERSION"
