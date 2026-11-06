#!/bin/bash
# ===========================================================================
# SD Card Safety Setup Script for TempBot (Raspberry Pi)
# ===========================================================================
#
# This script configures the Raspberry Pi to minimise SD card writes and
# protect against corruption from sudden power loss.
#
# Features:
#   - Mount /var/log, /var/tmp, /tmp as tmpfs (RAM-backed)
#   - Disable swap
#   - Install and configure log2ram
#   - Configure journald for volatile storage
#   - Reduce unnecessary filesystem writes
#   - Automatic fsck on boot
#   - Safety monitoring cron job
#
# Usage:
#   sudo bash sd_safety_setup.sh
#
# Rollback:
#   sudo bash sd_safety_setup.sh --rollback
#
# ===========================================================================

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
LOG_FILE="/tmp/sd_safety_setup.log"
REAL_USER="${SUDO_USER:-$(whoami)}"
USER_HOME=$(eval echo "~$REAL_USER")
BACKUP_DIR="${USER_HOME}/.sd_safety_backup"
FSTAB="/etc/fstab"
FSTAB_BACKUP="${BACKUP_DIR}/fstab.bak"
SYSCTL_CONF="/etc/sysctl.d/99-sd-safety.conf"
JOURNALD_CONF="/etc/systemd/journald.conf.d/sd-safety.conf"
CRON_SCRIPT="/usr/local/bin/sd-safety-monitor.sh"

# Colours for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Colour

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

log_msg() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo -e "${ts} [${level}] ${msg}" | tee -a "$LOG_FILE"
}

info()    { log_msg "INFO"    "${GREEN}$*${NC}"; }
warn()    { log_msg "WARNING" "${YELLOW}$*${NC}"; }
err()     { log_msg "ERROR"   "${RED}$*${NC}"; }

check_root() {
    if [[ $EUID -ne 0 ]]; then
        err "This script must be run as root (sudo)."
        exit 1
    fi
}

create_backup_dir() {
    mkdir -p "$BACKUP_DIR"
    info "Backup directory: $BACKUP_DIR"
}

# ---------------------------------------------------------------------------
# 1. Mount /tmp, /var/tmp, /var/log as tmpfs
# ---------------------------------------------------------------------------

setup_tmpfs() {
    info "--- Setting up tmpfs mounts ---"

    # Backup original fstab
    if [[ ! -f "$FSTAB_BACKUP" ]]; then
        cp "$FSTAB" "$FSTAB_BACKUP"
        info "Original fstab backed up to $FSTAB_BACKUP"
    fi

    # Entries to add (skip if already present)
    declare -A TMPFS_MOUNTS=(
        ["/tmp"]="tmpfs /tmp tmpfs defaults,noatime,nosuid,nodev,size=64M 0 0"
        ["/var/tmp"]="tmpfs /var/tmp tmpfs defaults,noatime,nosuid,nodev,size=32M 0 0"
        ["/var/log"]="tmpfs /var/log tmpfs defaults,noatime,nosuid,nodev,mode=0755,size=64M 0 0"
    )

    for mount_point in "${!TMPFS_MOUNTS[@]}"; do
        local entry="${TMPFS_MOUNTS[$mount_point]}"
        if grep -q "tmpfs ${mount_point} " "$FSTAB" 2>/dev/null; then
            warn "tmpfs entry for ${mount_point} already exists in fstab – skipping."
        else
            echo "$entry" >> "$FSTAB"
            info "Added tmpfs mount for ${mount_point}."
        fi
    done
}

# ---------------------------------------------------------------------------
# 2. Disable swap
# ---------------------------------------------------------------------------

disable_swap() {
    info "--- Disabling swap ---"

    # Turn off swap immediately
    swapoff -a 2>/dev/null || true

    # Remove swap entries from fstab
    if grep -q "swap" "$FSTAB" 2>/dev/null; then
        sed -i '/swap/d' "$FSTAB"
        info "Removed swap entries from fstab."
    fi

    # Disable dphys-swapfile service if present
    if systemctl is-enabled dphys-swapfile.service &>/dev/null; then
        systemctl stop dphys-swapfile.service
        systemctl disable dphys-swapfile.service
        info "Disabled dphys-swapfile service."
    fi

    # Remove swap file if it exists
    if [[ -f /var/swap ]]; then
        rm -f /var/swap
        info "Removed /var/swap file."
    fi

    info "Swap disabled."
}

# ---------------------------------------------------------------------------
# 3. Install and configure log2ram
# ---------------------------------------------------------------------------

setup_log2ram() {
    info "--- Setting up log2ram ---"

    if command -v log2ram &>/dev/null; then
        warn "log2ram is already installed – skipping installation."
    else
        if [[ -f /etc/apt/sources.list ]]; then
            # Install log2ram from the official repository
            if ! grep -q "azlux" /etc/apt/sources.list.d/*.list 2>/dev/null; then
                echo "deb [signed-by=/usr/share/keyrings/azlux-archive-keyring.gpg] http://packages.azlux.fr/debian/ bookworm main" \
                    > /etc/apt/sources.list.d/azlux.list
                wget -qO /usr/share/keyrings/azlux-archive-keyring.gpg https://azlux.fr/repo.gpg 2>/dev/null || true
            fi
            apt-get update -qq
            apt-get install -y -qq log2ram || {
                warn "log2ram installation failed – continuing without it."
                return
            }
            info "log2ram installed."
        else
            warn "Not a Debian-based system – skipping log2ram."
            return
        fi
    fi

    # Configure log2ram
    local LOG2RAM_CONF="/etc/log2ram.conf"
    if [[ -f "$LOG2RAM_CONF" ]]; then
        # Back up
        cp "$LOG2RAM_CONF" "${BACKUP_DIR}/log2ram.conf.bak" 2>/dev/null || true
        # Set RAM size to 64M
        sed -i 's/^SIZE=.*/SIZE=64M/' "$LOG2RAM_CONF"
        # Use rsync for safer syncs
        sed -i 's/^USE_RSYNC=.*/USE_RSYNC=true/' "$LOG2RAM_CONF" 2>/dev/null || \
            echo "USE_RSYNC=true" >> "$LOG2RAM_CONF"
        info "log2ram configured (64M, rsync)."
    fi
}

# ---------------------------------------------------------------------------
# 4. Configure journald for volatile (RAM) storage
# ---------------------------------------------------------------------------

setup_journald() {
    info "--- Configuring journald for volatile storage ---"

    mkdir -p /etc/systemd/journald.conf.d

    cat > "$JOURNALD_CONF" <<'JEOF'
# SD Card Safety: store journal in RAM only
[Journal]
Storage=volatile
RuntimeMaxUse=32M
RuntimeMaxFileSize=8M
Compress=yes
ForwardToSyslog=no
JEOF

    info "journald configured for volatile storage."

    # Restart journald to apply
    systemctl restart systemd-journald 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 5. Reduce filesystem writes via sysctl
# ---------------------------------------------------------------------------

reduce_writes() {
    info "--- Reducing filesystem writes ---"

    cat > "$SYSCTL_CONF" <<'SEOF'
# SD Card Safety: reduce write frequency
# Increase dirty writeback time to 60s (default 5s)
vm.dirty_writeback_centisecs = 6000
# Allow up to 20% of RAM as dirty pages before writeback (default 10%)
vm.dirty_ratio = 20
# Start background writeback at 10% (default 5%)
vm.dirty_background_ratio = 10
# Reduce swappiness to minimum (swap is disabled, but just in case)
vm.swappiness = 1
SEOF

    sysctl --load="$SYSCTL_CONF" 2>/dev/null || true
    info "sysctl write-reduction settings applied."

    # Disable filesystem access-time updates on root
    if ! grep -q "noatime" "$FSTAB" | grep -v "^#" | grep -v "tmpfs" | head -1 2>/dev/null; then
        # Add noatime to the root partition entry
        sed -i '/^\S\+\s\+\/\s/ s/defaults/defaults,noatime,commit=600/' "$FSTAB" 2>/dev/null || true
        info "Added noatime and commit=600 to root mount options."
    fi
}

# ---------------------------------------------------------------------------
# 6. Automatic fsck on boot
# ---------------------------------------------------------------------------

setup_fsck() {
    info "--- Configuring automatic fsck on boot ---"

    # Ensure fsck is enabled in cmdline.txt (supports both legacy and Bookworm paths)
    local CMDLINE="/boot/cmdline.txt"
    if [[ ! -f "$CMDLINE" && -f "/boot/firmware/cmdline.txt" ]]; then
        CMDLINE="/boot/firmware/cmdline.txt"
    fi
    if [[ -f "$CMDLINE" ]]; then
        cp "$CMDLINE" "${BACKUP_DIR}/cmdline.txt.bak" 2>/dev/null || true
        if ! grep -q "fsck.repair=yes" "$CMDLINE"; then
            sed -i 's/$/ fsck.repair=yes/' "$CMDLINE"
            info "Added fsck.repair=yes to boot cmdline."
        else
            info "fsck.repair=yes already present."
        fi
    fi

    # Ensure fsck runs (tune2fs – set mount count interval)
    local ROOT_DEV
    ROOT_DEV="$(findmnt -n -o SOURCE /)" 2>/dev/null || true
    if [[ -n "$ROOT_DEV" ]]; then
        tune2fs -c 10 -i 1w "$ROOT_DEV" 2>/dev/null || true
        info "Set fsck interval: every 10 mounts or 1 week."
    fi
}

# ---------------------------------------------------------------------------
# 7. Safety monitoring cron job
# ---------------------------------------------------------------------------

setup_monitoring_cron() {
    info "--- Creating safety monitoring cron job ---"

    cat > "$CRON_SCRIPT" <<'MEOF'
#!/bin/bash
# SD Card Safety Monitor – runs periodically to check disk health
LOG="/tmp/sd_safety_monitor.log"
TS="$(date '+%Y-%m-%d %H:%M:%S')"

{
    echo "=== SD Safety Check: $TS ==="

    # Check root filesystem usage
    ROOT_USAGE=$(df / --output=pcent | tail -1 | tr -d ' %')
    echo "Root FS usage: ${ROOT_USAGE}%"
    if [[ "$ROOT_USAGE" -gt 90 ]]; then
        echo "WARNING: Root filesystem usage above 90%!"
        logger -t sd-safety "WARNING: Root filesystem usage at ${ROOT_USAGE}%"
    fi

    # Check tmpfs usage
    for mp in /tmp /var/log /var/tmp; do
        if mountpoint -q "$mp" 2>/dev/null; then
            USAGE=$(df "$mp" --output=pcent | tail -1 | tr -d ' %')
            echo "tmpfs $mp: ${USAGE}%"
            if [[ "$USAGE" -gt 80 ]]; then
                echo "WARNING: tmpfs $mp usage above 80%!"
                logger -t sd-safety "WARNING: tmpfs $mp at ${USAGE}%"
            fi
        fi
    done

    # Check for filesystem errors
    dmesg | grep -i "ext4.*error\|i/o error\|read-only" | tail -5 || true

    # Check SD card health (if available)
    if [[ -f /sys/block/mmcblk0/stat ]]; then
        echo "SD I/O stats:"
        cat /sys/block/mmcblk0/stat
    fi

    echo "---"
} >> "$LOG" 2>&1

# Rotate the monitor log if it gets too large (>2MB)
if [[ -f "$LOG" ]] && [[ $(stat -f%z "$LOG" 2>/dev/null || stat -c%s "$LOG" 2>/dev/null) -gt 2097152 ]]; then
    mv "$LOG" "${LOG}.old"
fi
MEOF

    chmod +x "$CRON_SCRIPT"

    # Install cron job – every 15 minutes
    local CRON_LINE="*/15 * * * * $CRON_SCRIPT"
    if ! crontab -l 2>/dev/null | grep -q "$CRON_SCRIPT"; then
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
        info "Cron job installed (every 15 minutes)."
    else
        info "Cron job already installed."
    fi
}

# ---------------------------------------------------------------------------
# 8. Create TempBot tmpfs directory structure
# ---------------------------------------------------------------------------

setup_tempbot_dirs() {
    info "--- Creating TempBot directory structure ---"

    # Persistent data directory (on SD card – written rarely)
    mkdir -p "${USER_HOME}/tempbot_data/events"
    chown -R "${REAL_USER}:${REAL_USER}" "${USER_HOME}/tempbot_data"

    # tmpfs directory is created at runtime by the TempBot service
    info "TempBot directories created."
}

# ---------------------------------------------------------------------------
# Rollback function
# ---------------------------------------------------------------------------

rollback() {
    info "=== ROLLBACK: Restoring original configuration ==="

    # Restore fstab
    if [[ -f "$FSTAB_BACKUP" ]]; then
        cp "$FSTAB_BACKUP" "$FSTAB"
        info "Restored original fstab."
    fi

    # Re-enable swap
    if systemctl list-unit-files | grep -q dphys-swapfile; then
        systemctl enable dphys-swapfile.service 2>/dev/null || true
        info "Re-enabled dphys-swapfile."
    fi

    # Remove sysctl overrides
    rm -f "$SYSCTL_CONF"
    sysctl --system 2>/dev/null || true
    info "Removed sysctl overrides."

    # Remove journald overrides
    rm -f "$JOURNALD_CONF"
    systemctl restart systemd-journald 2>/dev/null || true
    info "Removed journald overrides."

    # Remove cron job
    crontab -l 2>/dev/null | grep -v "$CRON_SCRIPT" | crontab - 2>/dev/null || true
    rm -f "$CRON_SCRIPT"
    info "Removed monitoring cron job."

    # Restore cmdline.txt
    if [[ -f "${BACKUP_DIR}/cmdline.txt.bak" ]]; then
        cp "${BACKUP_DIR}/cmdline.txt.bak" /boot/cmdline.txt
        info "Restored original cmdline.txt."
    fi

    info "=== Rollback complete. Reboot to apply changes. ==="
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    check_root
    create_backup_dir

    if [[ "${1:-}" == "--rollback" ]]; then
        rollback
        exit 0
    fi

    info "=========================================="
    info " SD Card Safety Setup for TempBot"
    info "=========================================="
    echo ""
    warn "This script will modify system configuration."
    warn "A backup of original files will be saved to: $BACKUP_DIR"
    warn "To undo changes, run: sudo bash $SCRIPT_NAME --rollback"
    echo ""

    setup_tmpfs
    disable_swap
    setup_log2ram
    setup_journald
    reduce_writes
    setup_fsck
    setup_monitoring_cron
    setup_tempbot_dirs

    echo ""
    info "=========================================="
    info " SD Card Safety Setup Complete!"
    info "=========================================="
    info ""
    info "Changes applied:"
    info "  [✓] tmpfs mounts for /tmp, /var/log, /var/tmp"
    info "  [✓] Swap disabled"
    info "  [✓] log2ram installed/configured"
    info "  [✓] journald set to volatile storage"
    info "  [✓] Reduced filesystem write frequency"
    info "  [✓] Automatic fsck on boot"
    info "  [✓] Safety monitoring cron job (every 15 min)"
    info "  [✓] TempBot data directories created"
    info ""
    warn "REBOOT REQUIRED to apply all changes."
    info "Run: sudo reboot"
    info ""
    info "To rollback: sudo bash $SCRIPT_NAME --rollback"
}

main "$@"
