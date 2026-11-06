#!/usr/bin/env bash
# mm3-install.sh: install or update MM3 on Debian 12 or 13 (x86-64).
#
# Run as root; safe to run again. It creates the users and groups mm3,
# mm3-kiosk (mm3 is a member of mm3-kiosk), mm3-install (a member of mm3) and
# mm3-build (no groups), installs the Debian packages the
# engine and the kiosk need, builds a venv in /opt/mm3/venvs/<timestamp>
# behind the symlink /opt/mm3/venv and the installer's own venv
# /opt/mm3/installer (mm3-core[installer], never a module), writes
# /etc/mm3/config.yaml and /etc/mm3/mm3.env when they are absent, installs
# mm3.service, mm3-kiosk.service, mm3-installer.service and its polkit rule,
# and enables and starts them. It never overwrites a
# configuration, env file, state directory or token store, and it creates no
# token. A new run builds a new venv, moves the symlink, restarts the units and
# keeps the previous venv for rollback.
#
# Usage: mm3-install.sh [--find-links DIR] [--no-kiosk]
#
# Exit code: 0 success, 1 unsupported system, 2 any other failure.
#
# Implements CORE-293 (Core spec 0.20, §10.2 Packaging and deployment).
# Implements CORE-268 (Core spec 0.20, §6.7 Display output): mm3 is a member of
# the group mm3-kiosk.
# Implements CORE-143 (Core spec 0.20, §6.7 Display output): the kiosk needs
# Debian 13 or later and cage 0.1.5 or later.
# Implements CORE-348 (Core spec 0.34, §9.2 Presence and power: module boundary):
# on kiosk systems ddcutil and cec-utils, and mm3 in the groups i2c and dialout.
# Implements CORE-353 (Core spec 0.33, §10.6 Module catalog): the user
# mm3-install, a member of mm3, owns /opt/mm3/venvs.
# Implements CORE-357 (Core spec 0.33, §10.7 Installer service): the installer
# unit and its polkit rule.
# Implements CORE-368 (Core spec 0.35, §10.7 Installer service): the installer's
# own venv and the user mm3-build.
set -Eeuo pipefail

readonly EXIT_UNSUPPORTED=1
readonly EXIT_FAILED=2
readonly KIOSK_USER=mm3-kiosk
readonly KIOSK_GROUP=mm3-kiosk
readonly MIN_CAGE=0.1.5
readonly KIOSK_DEVICE_GROUPS=(video input render)
# CORE-348: the engine user reaches the I2C bus (ddcutil) and the CEC adapter
# (cec-client) through these groups, where they exist.
readonly DISPLAY_CONTROL_GROUPS=(i2c dialout)

# Locations and names. The environment overrides them for the tests of this
# script only; a real installation uses the defaults.
OS_RELEASE=${MM3_OS_RELEASE:-/etc/os-release}
ARCH=${MM3_ARCH:-}
OPT=${MM3_OPT:-/opt/mm3}
ETC=${MM3_ETC:-/etc/mm3}
UNIT_DIR=${MM3_UNIT_DIR:-/etc/systemd/system}
SVC_USER=${MM3_USER:-mm3}
SVC_GROUP=${MM3_GROUP:-mm3}
INSTALLER_USER=${MM3_INSTALLER_USER:-mm3-install}
BUILD_USER=${MM3_BUILD_USER:-mm3-build}
POLKIT_DIR=${MM3_POLKIT_DIR:-/etc/polkit-1/rules.d}
TIMEZONE_FILE=${MM3_TIMEZONE_FILE:-/etc/timezone}
SKIP_PACKAGES=${MM3_SKIP_PACKAGES:-no}

FIND_LINKS=""
WANT_KIOSK=yes
DEBIAN_MAJOR=""
STAGED_VENV=""
PREVIOUS_VENV=""
ACTIVATED=no

say() { printf 'mm3-install: %s\n' "$*"; }
warn() { printf 'mm3-install: warning: %s\n' "$*" >&2; }
die() {
  printf 'mm3-install: error: %s\n' "$*" >&2
  cleanup
  exit "$EXIT_FAILED"
}
unsupported() {
  printf 'mm3-install: error: %s\n' "$*" >&2
  exit "$EXIT_UNSUPPORTED"
}

usage() {
  cat <<'USAGE'
Usage: mm3-install.sh [--find-links DIR] [--no-kiosk]

Installs or updates MM3 on Debian 12 or 13 (x86-64). Run as root.

  --find-links DIR  install mm3-core and mm3-modules-default from the wheels in
                    DIR (and PyPI for their dependencies) instead of PyPI alone
  --no-kiosk        do not install or start the kiosk (mm3-kiosk.service)
  -h, --help        show this help

Exit code: 0 success, 1 unsupported system, 2 any other failure.
USAGE
}

# Remove a venv that was built but never activated.
cleanup() {
  if [[ -n $STAGED_VENV && $ACTIVATED != yes && -d $STAGED_VENV ]]; then
    rm -rf "$STAGED_VENV"
  fi
}

on_error() {
  local status=$? line=$1 command=$2
  trap - ERR
  printf 'mm3-install: error: "%s" failed (status %s, line %s)\n' "$command" "$status" "$line" >&2
  cleanup
  exit "$EXIT_FAILED"
}

parse_args() {
  while (($#)); do
    case $1 in
      --find-links)
        (($# >= 2)) || die "--find-links needs a directory"
        FIND_LINKS=$2
        shift 2
        ;;
      --find-links=*)
        FIND_LINKS=${1#*=}
        shift
        ;;
      --no-kiosk)
        WANT_KIOSK=no
        shift
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        usage >&2
        die "unknown option: $1"
        ;;
    esac
  done
  if [[ -n $FIND_LINKS ]]; then
    [[ -d $FIND_LINKS ]] || die "--find-links: no such directory: $FIND_LINKS"
    FIND_LINKS=$(cd "$FIND_LINKS" && pwd)
  fi
}

check_system() {
  local id="" version="" arch=${ARCH:-$(uname -m)}
  if [[ -r $OS_RELEASE ]]; then
    # shellcheck source=/dev/null
    id=$(. "$OS_RELEASE" && printf '%s' "${ID:-}")
    # shellcheck source=/dev/null
    version=$(. "$OS_RELEASE" && printf '%s' "${VERSION_ID:-}")
  fi
  [[ $arch == x86_64 ]] || unsupported "unsupported architecture '$arch' (x86-64 only)"
  [[ $id == debian ]] || unsupported "unsupported system '${id:-unknown}' (Debian 12 or 13 only)"
  case $version in
    12 | 13) DEBIAN_MAJOR=$version ;;
    *) unsupported "unsupported Debian release '${version:-unknown}' (12 or 13 only)" ;;
  esac
}

require_root() {
  ((EUID == 0)) || die "run as root, for example: sudo bash mm3-install.sh"
}

in_group() {
  id -nG "$1" | tr ' ' '\n' | grep -qx "$2"
}

# CORE-293, CORE-268: the users and groups, and mm3 in the group mm3-kiosk.
ensure_accounts() {
  if ! getent group "$SVC_GROUP" >/dev/null; then groupadd --system "$SVC_GROUP"; fi
  if ! getent group "$KIOSK_GROUP" >/dev/null; then groupadd --system "$KIOSK_GROUP"; fi
  if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --gid "$SVC_GROUP" --home-dir /var/lib/mm3 \
      --shell /usr/sbin/nologin "$SVC_USER"
    say "created the user $SVC_USER"
  fi
  if ! id -u "$KIOSK_USER" >/dev/null 2>&1; then
    useradd --system --gid "$KIOSK_GROUP" \
      --home-dir /var/lib/mm3-kiosk --shell /usr/sbin/nologin "$KIOSK_USER"
    say "created the user $KIOSK_USER"
  fi
  # The engine gives /run/mm3/kiosk.key to this group (CORE-268); the kiosk
  # user is not a member of the group mm3.
  if ! in_group "$SVC_USER" "$KIOSK_GROUP"; then
    usermod --append --groups "$KIOSK_GROUP" "$SVC_USER"
    say "added $SVC_USER to the group $KIOSK_GROUP"
  fi
  ensure_installer_accounts
}

# CORE-353, CORE-368: the installer user, a member of mm3 (installer and admin
# sockets), and the build user, with no supplementary groups, which package
# code runs as without capabilities (CORE-367).
ensure_installer_accounts() {
  if ! id -u "$INSTALLER_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/mm3-install \
      --shell /usr/sbin/nologin "$INSTALLER_USER"
    say "created the user $INSTALLER_USER"
  fi
  if ! in_group "$INSTALLER_USER" "$SVC_GROUP"; then
    usermod --append --groups "$SVC_GROUP" "$INSTALLER_USER"
    say "added $INSTALLER_USER to the group $SVC_GROUP"
  fi
  if ! id -u "$BUILD_USER" >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin "$BUILD_USER"
    say "created the user $BUILD_USER"
  fi
}

# CORE-143: the kiosk user reaches the display and the input devices through
# these groups. They come with systemd, so this runs after the packages.
ensure_kiosk_groups() {
  local group
  local -a missing=()
  for group in "${KIOSK_DEVICE_GROUPS[@]}"; do
    if ! getent group "$group" >/dev/null; then
      missing+=("$group")
    elif ! in_group "$KIOSK_USER" "$group"; then
      usermod --append --groups "$group" "$KIOSK_USER"
      say "added $KIOSK_USER to the group $group"
    fi
  done
  if [[ $WANT_KIOSK == yes ]] && ((${#missing[@]})); then
    warn "the groups ${missing[*]} do not exist; the kiosk may not reach the display or the input devices"
  fi
}

# CORE-348: the engine runs the display drivers of mm3-presence, so mm3 joins the
# groups of the I2C bus and the serial CEC adapter that exist. A failed change
# is logged, not fatal; the groups apply at the next start of mm3.service.
ensure_display_control_groups() {
  local group
  if [[ $WANT_KIOSK != yes ]]; then
    return 0
  fi
  for group in "${DISPLAY_CONTROL_GROUPS[@]}"; do
    if ! getent group "$group" >/dev/null; then
      continue
    fi
    if in_group "$SVC_USER" "$group"; then
      continue
    fi
    if usermod --append --groups "$group" "$SVC_USER"; then
      say "added $SVC_USER to the group $group"
    else
      warn "could not add $SVC_USER to the group $group; the display drivers may not reach the monitor"
    fi
  done
}

display_manager_enabled() {
  [[ -d /run/systemd/system ]] && systemctl is-enabled --quiet display-manager.service 2>/dev/null
}

# CORE-143: the kiosk needs Debian 13 or later, and tty1 and the seat to itself.
decide_kiosk() {
  if [[ $WANT_KIOSK == no ]]; then
    return 0
  fi
  if ((DEBIAN_MAJOR < 13)); then
    say "Debian $DEBIAN_MAJOR: the kiosk needs Debian 13 or later; installing the engine only"
    WANT_KIOSK=no
  elif display_manager_enabled; then
    warn "a display manager is enabled and owns tty1 and the seat; installing the engine only."
    warn "To use the kiosk, disable it (systemctl disable --now gdm3, sddm or lightdm) and run this script again."
    WANT_KIOSK=no
  fi
}

install_packages() {
  local packages=(python3 python3-venv ca-certificates polkitd)
  if [[ $WANT_KIOSK == yes ]]; then
    packages+=(cage chromium wlr-randr ddcutil cec-utils)
  fi
  if [[ $SKIP_PACKAGES == yes ]]; then
    say "skipping the Debian packages (${packages[*]})"
    return 0
  fi
  say "installing the Debian packages: ${packages[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends "${packages[@]}"
}

# True when `cage -v` prints a leading MAJOR.MINOR.PATCH of 0.1.5 or later (CORE-143).
cage_version_ok() {
  local output version
  output=$(cage -v 2>/dev/null) || return 1
  version=$(printf '%s\n' "$output" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)
  [[ -n $version ]] || return 1
  [[ $(printf '%s\n%s\n' "$MIN_CAGE" "$version" | sort -V | head -n1) == "$MIN_CAGE" ]]
}

verify_cage() {
  if [[ $WANT_KIOSK == yes && $SKIP_PACKAGES != yes ]] && ! cage_version_ok; then
    warn "cage $MIN_CAGE or later is required (CORE-143); installing the engine only"
    WANT_KIOSK=no
  fi
}

# Build a venv in /opt/mm3/venvs/<timestamp> and install the core, then the
# default modules (CORE-293).
build_venv() {
  local stamp core_major
  local -a pip modules=()
  stamp=$(date +%Y%m%d-%H%M%S)
  install -d -m 0755 "$OPT" "$OPT/venvs"
  STAGED_VENV=$OPT/venvs/$stamp
  say "building the venv $STAGED_VENV"
  python3 -m venv "$STAGED_VENV"
  pip=("$STAGED_VENV/bin/pip" install --disable-pip-version-check --no-input)
  if [[ -n $FIND_LINKS ]]; then
    pip+=(--find-links "$FIND_LINKS")
  fi
  "${pip[@]}" mm3-core
  core_major=$("$STAGED_VENV/bin/python" -c \
    'from importlib.metadata import version; print(version("mm3-core").split(".")[0])')
  if [[ $core_major == 0 ]]; then
    # Until mm3-core 1.0.0 the modules ask for mm3-core>=1.0,<2 (MOD-03): install
    # the bundle and the modules it lists without their dependencies.
    say "mm3-core is 0.x: installing the default modules with --no-deps"
    "${pip[@]}" --no-deps mm3-modules-default
    mapfile -t modules < <("$STAGED_VENV/bin/python" -c '
from importlib.metadata import requires
for requirement in requires("mm3-modules-default") or []:
    print(requirement)')
    ((${#modules[@]})) || die "mm3-modules-default lists no modules"
    "${pip[@]}" --no-deps "${modules[@]}"
  else
    "${pip[@]}" mm3-modules-default
  fi
  "$STAGED_VENV/bin/mm3" --version >/dev/null
  # CORE-353: the installer service is the only writer of the venvs from now on.
  chown "$INSTALLER_USER:" "$OPT/venvs"
}

# CORE-368: the installer runs from its own venv, with the same mm3-core and its
# extra [installer], and never a module. It is rebuilt with every run.
build_installer_venv() {
  local version target=$OPT/installer new=$OPT/installer.new old=$OPT/installer.old
  local -a pip
  version=$("$STAGED_VENV/bin/python" -c \
    'from importlib.metadata import version; print(version("mm3-core"))')
  rm -rf "$new" "$old"
  say "building the installer venv $target (mm3-core $version)"
  python3 -m venv "$new"
  pip=("$new/bin/pip" install --disable-pip-version-check --no-input)
  if [[ -n $FIND_LINKS ]]; then
    pip+=(--find-links "$FIND_LINKS")
  fi
  "${pip[@]}" "mm3-core[installer]==$version"
  "$new/bin/mm3" --version >/dev/null
  if [[ -e $target ]]; then mv -T "$target" "$old"; fi
  mv -T "$new" "$target"
  rm -rf "$old"
}

detect_timezone() {
  local tz=""
  if [[ -r $TIMEZONE_FILE ]]; then
    tz=$(head -n1 "$TIMEZONE_FILE")
  fi
  if [[ -z $tz && -L /etc/localtime ]]; then
    tz=$(readlink /etc/localtime)
    tz=${tz#*/zoneinfo/}
  fi
  tz=${tz//[[:space:]]/}
  if [[ -n $tz && -e /usr/share/zoneinfo/$tz ]]; then
    printf '%s' "$tz"
  else
    printf 'UTC'
  fi
}

default_config() {
  cat <<CONFIG
# MM3 configuration, written once by mm3-install.sh; the installer never
# overwrites it. Saving this file reloads the engine; 'mm3 check-config'
# validates it first. Documentation: https://github.com/mm3-project/mm3-core
engine:
  locale: en
  timezone: $(detect_timezone)
  # The admin UI listens on 127.0.0.1:8081 only. To reach it from your LAN:
  # admin:
  #   host: 0.0.0.0
  #   allow: ["192.168.1.0/24"]
modules:
  - module: clock
    position: top_left
CONFIG
}

# CORE-293, CORE-200: the configuration and the env file, only when absent.
write_config() {
  local tmp
  if [[ ! -d $ETC ]]; then
    install -d -o "$SVC_USER" -g "$SVC_GROUP" -m 0750 "$ETC"
  fi
  if [[ -e $ETC/mm3.env ]]; then
    say "keeping $ETC/mm3.env"
  else
    install -o "$SVC_USER" -g "$SVC_GROUP" -m 0640 /dev/null "$ETC/mm3.env"
    say "created $ETC/mm3.env"
  fi
  if [[ -e $ETC/config.yaml ]]; then
    say "keeping $ETC/config.yaml"
  else
    tmp=$(mktemp)
    default_config >"$tmp"
    install -o "$SVC_USER" -g "$SVC_GROUP" -m 0640 "$tmp" "$ETC/config.yaml"
    rm -f "$tmp"
    say "created $ETC/config.yaml"
  fi
}

run_as_service_user() {
  if ((EUID == 0)) && command -v runuser >/dev/null; then
    runuser -u "$SVC_USER" -- "$@"
  else
    "$@"
  fi
}

# The configuration is checked with the new venv before anything is switched.
validate_config() {
  say "checking the configuration with the new venv"
  if ! run_as_service_user "$STAGED_VENV/bin/mm3" check-config \
    --config "$ETC/config.yaml" --env-file "$ETC/mm3.env"; then
    die "the configuration in $ETC is not valid for the new venv; nothing was switched, fix it and run this script again"
  fi
}

# Point /opt/mm3/venv at the new venv and keep the previous one (CORE-293).
activate_venv() {
  local link=$OPT/venv
  if [[ -L $link ]]; then
    PREVIOUS_VENV=$(readlink -f "$link")
  elif [[ -e $link ]]; then
    PREVIOUS_VENV=$OPT/venvs/previous-$(date +%Y%m%d-%H%M%S)
    mv "$link" "$PREVIOUS_VENV"
    warn "$link was a directory; it is kept as $PREVIOUS_VENV, but a moved venv cannot be used for rollback"
  fi
  ln -sfn "$STAGED_VENV" "$OPT/venv.new"
  mv -T "$OPT/venv.new" "$link"
  ACTIVATED=yes
  say "$link now points to $STAGED_VENV"
}

# CORE-292, CORE-143: the unit files come from the installed package.
install_unit() {
  local name=$1 command=$2 tmp
  install -d -m 0755 "$UNIT_DIR"
  tmp=$(mktemp)
  "$OPT/venv/bin/mm3" "$command" unit >"$tmp"
  install -m 0644 "$tmp" "$UNIT_DIR/$name"
  rm -f "$tmp"
  say "installed $UNIT_DIR/$name"
}

install_units() {
  install_unit mm3.service service
  if [[ $WANT_KIOSK == yes ]]; then
    install_unit mm3-kiosk.service kiosk
  fi
  install_installer_unit
}

# CORE-357: the installer unit and the polkit rule that lets it stop, start and
# restart mm3.service and nothing else (CORE-359).
install_installer_unit() {
  local tmp
  install -d -m 0755 "$UNIT_DIR" "$POLKIT_DIR"
  tmp=$(mktemp)
  "$OPT/installer/bin/mm3" installer unit >"$tmp"
  install -m 0644 "$tmp" "$UNIT_DIR/mm3-installer.service"
  "$OPT/installer/bin/mm3" installer polkit >"$tmp"
  install -m 0644 "$tmp" "$POLKIT_DIR/50-mm3-installer.rules"
  rm -f "$tmp"
  say "installed $UNIT_DIR/mm3-installer.service and $POLKIT_DIR/50-mm3-installer.rules"
}

start_units() {
  local -a units=(mm3.service mm3-installer.service)
  local unit action
  if [[ $WANT_KIOSK == yes ]]; then
    units+=(mm3-kiosk.service)
  fi
  if [[ ! -d /run/systemd/system ]]; then
    warn "systemd is not running: the units are installed but not enabled or started"
    return 0
  fi
  systemctl daemon-reload
  if [[ $WANT_KIOSK == yes && $(systemctl get-default) == multi-user.target ]]; then
    systemctl set-default graphical.target
    say "default target set to graphical.target (mm3-kiosk.service is wanted by it)"
  fi
  systemctl enable "${units[@]}"
  for unit in "${units[@]}"; do
    if systemctl is-active --quiet "$unit"; then action=restart; else action=start; fi
    systemctl "$action" "$unit" || die "$unit failed to $action; see: journalctl -u ${unit%.service}"
  done
}

summary() {
  say "done."
  say "Create an admin token (it is shown once; this script creates none):"
  say "  sudo -u $SVC_USER env STATE_DIRECTORY=/var/lib/mm3 $OPT/venv/bin/mm3 token new admin"
  say "The admin UI listens on 127.0.0.1:8081; edit $ETC/config.yaml to open it to your LAN."
  say "Set the install passphrase for module installs (once, as root):"
  say "  sudo $OPT/installer/bin/mm3 installer passphrase"
  if [[ -n $PREVIOUS_VENV ]]; then
    say "To roll back: ln -sfn $PREVIOUS_VENV $OPT/venv && systemctl restart mm3 mm3-kiosk"
  fi
}

main() {
  trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR
  parse_args "$@"
  check_system
  require_root
  ensure_accounts
  decide_kiosk
  install_packages
  ensure_kiosk_groups
  ensure_display_control_groups
  verify_cage
  build_venv
  build_installer_venv
  write_config
  validate_config
  activate_venv
  install_units
  start_units
  summary
}

# Run main unless the file is sourced (the tests source it).
if [[ ${BASH_SOURCE[0]:-$0} == "$0" ]]; then
  main "$@"
fi
