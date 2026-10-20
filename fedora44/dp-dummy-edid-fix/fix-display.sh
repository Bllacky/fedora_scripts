#!/usr/bin/env bash
# Fedora / AMD dummy-display firmware EDID setup. No automatic reboot.
set -euo pipefail

bundle_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
export PYTHONDONTWRITEBYTECODE=1

usage() {
    cat <<'USAGE'
Usage: ./fix-display.sh COMMAND [--connector DP-1] [--card card1]

  check-edid     Validate the bundled EDID; no root required.
  status         Show kernel, EDID and GNOME display state; no root required.
  plan           Inspect and print intended changes; run with sudo.
  install        Install persistent boot configuration; run with sudo.
  verify         Verify boot parameters and adopted EDID after reboot.
  uninstall      Restore this bundle's saved configuration; run with sudo.
  rdp-physical   Set GNOME RDP to mirror the active physical primary display;
                 run as your desktop user, without sudo.

Defaults: DP-1, preferred 1920x1080 at 60 Hz. Both 1080p and 1440p
advertise nominal 60/75/90 Hz. Installation does not change the live layout.
Read README.md before installing, particularly if you depend on RDP.
USAGE
}

case "${1:-help}" in
    help|-h|--help) usage; exit 0 ;;
    check-edid)
        shift
        if (( $# )); then usage >&2; exit 2; fi
        python3 "$bundle_dir/generate_edid.py" --check "$bundle_dir/edid/dp-dummy-multimode.bin"
        if command -v edid-decode >/dev/null 2>&1; then
            edid-decode --check --skip-hex-dump "$bundle_dir/edid/dp-dummy-multimode.bin"
        fi
        ;;
    plan|install|uninstall)
        if (( EUID != 0 )); then
            printf 'Run this command with sudo. No changes made.\n' >&2
            exit 1
        fi
        exec python3 "$bundle_dir/lib/edid_fix.py" "$@"
        ;;
    status|verify|rdp-physical)
        exec python3 "$bundle_dir/lib/edid_fix.py" "$@"
        ;;
    *) usage >&2; exit 2 ;;
esac
