#!/usr/bin/env bash
# updev installer.
#
# Installs what is missing and nothing else — every apt package is checked
# first, so re-running this is cheap and quiet. Nothing here is required to
# *use* updev from a checkout (bin/updev works as-is); this is for getting the
# optional backends and the `updev` command onto the system.
#
#   ./install.sh                  core + hardware backends + the wheel
#   ./install.sh --core           just the two required python packages
#   ./install.sh --tools          just the external CLI tools
#   ./install.sh --panel-service  register the ST7735S boot screen with systemd
#   ./install.sh --dry-run        print what it would do
set -euo pipefail

cd "$(dirname "$0")"

DRY_RUN=0
DO_CORE=1
DO_HARDWARE=1
DO_TOOLS=1
DO_WHEEL=1
DO_PANEL=0

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --core)     DO_HARDWARE=0; DO_TOOLS=0; DO_WHEEL=0 ;;
        --tools)    DO_CORE=0; DO_HARDWARE=0; DO_WHEEL=0 ;;
        --no-wheel) DO_WHEEL=0 ;;
        --panel-service) DO_CORE=0; DO_HARDWARE=0; DO_TOOLS=0; DO_WHEEL=0; DO_PANEL=1 ;;
        -h|--help)  sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

# Required: updev will not start without these.
CORE_PKGS=(python3-rich python3-click)

# Optional: each one enables one backend's extra detail.
HARDWARE_PKGS=(
    python3-psutil      # net    interface addresses, NIC counters
    python3-smbus2      # i2c    address scanning
    python3-spidev      # spi    mode/clock, loopback
    python3-serial      # serial port enumeration
    python3-lgpio       # gpio   claimed lines
    python3-picamera2   # camera libcamera (apt only — pip cannot build it)
    python3-pil         # vm     QEMU screenshot to PNG
)

# External commands. All probed with shutil.which and skipped when absent.
TOOL_PKGS=(
    util-linux          # lsblk       — storage backend needs this one
    iputils-ping        # ping        — LAN sweep needs this one
    iproute2            # ip
    iw                  # Wi-Fi signal
    bluez               # bluetoothctl
    v4l-utils           # v4l2-ctl
    usb.ids pci.ids     # vendor/product names
    udev                # systemd-hwdb, for MAC OUI lookup
)

say()  { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

run() {
    if [ "$DRY_RUN" = 1 ]; then
        note "would run: $*"
    else
        "$@"
    fi
}

missing_packages() {
    local pkg missing=()
    for pkg in "$@"; do
        if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
            missing+=("$pkg")
        fi
    done
    printf '%s\n' "${missing[@]:-}"
}

apt_install() {
    local label="$1"; shift
    local missing
    mapfile -t missing < <(missing_packages "$@")
    # mapfile keeps one empty line when nothing is missing.
    if [ "${#missing[@]}" -eq 0 ] || [ -z "${missing[0]}" ]; then
        say "$label: 이미 전부 설치됨"
        return 0
    fi
    say "$label: ${#missing[@]}개 설치 — ${missing[*]}"
    run sudo apt-get install -y --no-install-recommends "${missing[@]}"
}

[ "$DO_CORE" = 1 ]     && apt_install "필수 파이썬 패키지" "${CORE_PKGS[@]}"
[ "$DO_HARDWARE" = 1 ] && apt_install "하드웨어 백엔드"     "${HARDWARE_PKGS[@]}"
[ "$DO_TOOLS" = 1 ]    && apt_install "외부 도구"          "${TOOL_PKGS[@]}"

if [ "$DO_WHEEL" = 1 ]; then
    WHEEL=$(ls -1 dist/updev-*.whl 2>/dev/null | tail -1 || true)
    if [ -z "$WHEEL" ]; then
        say "휠 빌드"
        run python3 -m pip wheel . --no-deps --no-build-isolation -w dist/
        WHEEL=$(ls -1 dist/updev-*.whl 2>/dev/null | tail -1 || true)
    fi
    if [ -n "$WHEEL" ]; then
        # The system Python is externally-managed, so --user is the supported
        # path; sudo pip would be refused and pipx is not installed by default.
        say "설치: $WHEEL"
        run python3 -m pip install --user --force-reinstall --no-deps "$WHEEL"
        note "~/.local/bin 이 PATH 에 있어야 'updev' 가 잡힌다:"
        note '  echo '"'"'export PATH="$HOME/.local/bin:$PATH"'"'"' >> ~/.bashrc'
    fi
fi

if [ "$DO_PANEL" = 1 ]; then
    say "ST7735S 패널 서비스 등록"
    # Where updev lives depends on how it was installed. An installed entry
    # point wins; otherwise run straight out of this checkout.
    if command -v updev >/dev/null 2>&1; then
        PANEL_EXEC=$(command -v updev)
    else
        PANEL_EXEC="$PWD/bin/updev"
        note "설치된 updev 가 없어 체크아웃에서 실행: $PANEL_EXEC"
    fi

    for grp in spi gpio; do
        id -nG "$USER" | tr ' ' '\n' | grep -qx "$grp" || {
            note "경고: $USER 가 $grp 그룹에 없다 — 서비스가 패널을 못 연다"
            note "  sudo usermod -aG $grp $USER   (재로그인 필요)"
        }
    done

    UNIT=/etc/systemd/system/updev-panel.service
    if [ "$DRY_RUN" = 1 ]; then
        note "would write $UNIT with ExecStart=$PANEL_EXEC panel boot"
    else
        sed -e "s|@EXEC@|$PANEL_EXEC|g" \
            -e "s|@USER@|$USER|g" \
            -e "s|@WORKDIR@|$PWD|g" \
            systemd/updev-panel.service.in | sudo tee "$UNIT" >/dev/null
    fi
    run sudo systemctl daemon-reload
    run sudo systemctl enable --now updev-panel.service
    note "확인:  systemctl status updev-panel.service"
    exit 0
fi

say "그룹 확인"
for grp in i2c spi gpio dialout plugdev video; do
    if id -nG "$USER" | tr ' ' '\n' | grep -qx "$grp"; then
        note "OK   $grp"
    else
        note "없음 $grp   →  sudo usermod -aG $grp \$USER  (재로그인 필요)"
    fi
done

say "확인"
note "bin/updev backends     — 어느 백엔드가 살아있나"
note "bin/updev doctor       — 빠진 것 + 꺼진 것"
note "python3 -m unittest discover -s tests"
