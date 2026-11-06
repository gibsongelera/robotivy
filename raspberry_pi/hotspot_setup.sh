#!/bin/bash
# ===========================================================================
# WiFi Hotspot Setup Script for TempBot (Raspberry Pi 3 Model B)
# ===========================================================================
#
# Configures the Raspberry Pi as a WiFi access point so users can connect
# to the TempBot web dashboard without an existing network.
#
# Network configuration:
#   SSID:      TempBot-Monitor
#   Password:  tempbot123
#   IP range:  192.168.4.2 – 192.168.4.20
#   Gateway:   192.168.4.1
#
# Usage:
#   sudo bash hotspot_setup.sh
#
# To remove:
#   sudo bash hotspot_setup.sh --remove
#
# ===========================================================================

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

# Configuration
SSID="TempBot-Monitor"
WPA_PASSPHRASE="tempbot123"
CHANNEL=6
INTERFACE="wlan0"
IP_ADDRESS="192.168.4.1"
NETMASK="255.255.255.0"
DHCP_RANGE_START="192.168.4.2"
DHCP_RANGE_END="192.168.4.20"
DHCP_LEASE="24h"
REAL_USER="${SUDO_USER:-$(whoami)}"
USER_HOME=$(eval echo "~$REAL_USER")
BACKUP_DIR="${USER_HOME}/.hotspot_backup"

# Colours
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

check_root() {
    if [[ $EUID -ne 0 ]]; then
        err "This script must be run as root (sudo)."
        exit 1
    fi
}

check_interface() {
    if ! ip link show "$INTERFACE" &>/dev/null; then
        err "Wireless interface '$INTERFACE' not found."
        err "Available interfaces:"
        ip -br link show
        exit 1
    fi
    info "Found wireless interface: $INTERFACE"
}

create_backup() {
    mkdir -p "$BACKUP_DIR"

    # Backup existing configurations
    for file in /etc/hostapd/hostapd.conf /etc/dnsmasq.conf /etc/dhcpcd.conf /etc/sysctl.conf; do
        if [[ -f "$file" ]]; then
            cp "$file" "${BACKUP_DIR}/$(basename "$file").bak" 2>/dev/null || true
        fi
    done
    info "Configuration files backed up to $BACKUP_DIR"
}

# ---------------------------------------------------------------------------
# 1. Install required packages
# ---------------------------------------------------------------------------

install_packages() {
    info "--- Installing hostapd, dnsmasq, and iptables ---"

    apt-get update -qq
    apt-get install -y -qq hostapd dnsmasq iptables

    # Stop services while we configure them
    systemctl stop hostapd 2>/dev/null || true
    systemctl stop dnsmasq 2>/dev/null || true

    info "Packages installed."
}

# ---------------------------------------------------------------------------
# 2. Configure static IP for wlan0
# ---------------------------------------------------------------------------

configure_static_ip() {
    info "--- Configuring static IP for $INTERFACE ---"

    local DHCPCD_CONF="/etc/dhcpcd.conf"

    # Remove any existing static config for wlan0
    if [[ -f "$DHCPCD_CONF" ]]; then
        sed -i '/# TempBot Hotspot Config START/,/# TempBot Hotspot Config END/d' "$DHCPCD_CONF"
    fi

    cat >> "$DHCPCD_CONF" <<SEOF

# TempBot Hotspot Config START
interface ${INTERFACE}
    static ip_address=${IP_ADDRESS}/24
    nohook wpa_supplicant
# TempBot Hotspot Config END
SEOF

    info "Static IP $IP_ADDRESS configured for $INTERFACE."
}

# ---------------------------------------------------------------------------
# 3. Configure hostapd
# ---------------------------------------------------------------------------

configure_hostapd() {
    info "--- Configuring hostapd ---"

    cat > /etc/hostapd/hostapd.conf <<HEOF
# TempBot WiFi Hotspot Configuration
interface=${INTERFACE}
driver=nl80211

# Network settings
ssid=${SSID}
hw_mode=g
channel=${CHANNEL}
wmm_enabled=0
macaddr_acl=0

# Security settings
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=${WPA_PASSPHRASE}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP

# Performance
ieee80211n=1
country_code=US
HEOF

    # Tell hostapd where to find its config
    local HOSTAPD_DEFAULT="/etc/default/hostapd"
    if [[ -f "$HOSTAPD_DEFAULT" ]]; then
        sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' "$HOSTAPD_DEFAULT"
    else
        echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' > "$HOSTAPD_DEFAULT"
    fi

    info "hostapd configured (SSID: $SSID, Channel: $CHANNEL)."
}

# ---------------------------------------------------------------------------
# 4. Configure dnsmasq (DHCP server)
# ---------------------------------------------------------------------------

configure_dnsmasq() {
    info "--- Configuring dnsmasq ---"

    # Rename existing config (dnsmasq reads all .conf files in /etc/dnsmasq.d/)
    if [[ -f /etc/dnsmasq.conf ]]; then
        mv /etc/dnsmasq.conf /etc/dnsmasq.conf.orig 2>/dev/null || true
    fi

    cat > /etc/dnsmasq.conf <<DEOF
# TempBot DHCP & DNS Configuration

# Only listen on the hotspot interface
interface=${INTERFACE}
bind-interfaces

# DHCP range
dhcp-range=${DHCP_RANGE_START},${DHCP_RANGE_END},${NETMASK},${DHCP_LEASE}

# DNS – use Google DNS as upstream
server=8.8.8.8
server=8.8.4.4

# Domain
domain=tempbot.local
address=/tempbot.local/${IP_ADDRESS}

# Misc
bogus-priv
domain-needed

# Logging (to tmpfs)
log-facility=/tmp/dnsmasq.log
log-queries
DEOF

    info "dnsmasq configured (DHCP: $DHCP_RANGE_START - $DHCP_RANGE_END)."
}

# ---------------------------------------------------------------------------
# 5. Enable IP forwarding
# ---------------------------------------------------------------------------

enable_ip_forwarding() {
    info "--- Enabling IP forwarding ---"

    local SYSCTL_CONF="/etc/sysctl.conf"

    # Enable IP forwarding
    if ! grep -q "^net.ipv4.ip_forward=1" "$SYSCTL_CONF" 2>/dev/null; then
        echo "net.ipv4.ip_forward=1" >> "$SYSCTL_CONF"
    fi

    # Apply immediately
    sysctl -w net.ipv4.ip_forward=1 >/dev/null

    info "IP forwarding enabled."
}

# ---------------------------------------------------------------------------
# 6. Configure iptables (NAT)
# ---------------------------------------------------------------------------

configure_iptables() {
    info "--- Configuring iptables ---"

    if ! command -v iptables &>/dev/null; then
        apt-get install -y -qq iptables 2>/dev/null || true
    fi

    if ! command -v iptables &>/dev/null; then
        warn "iptables not available – skipping NAT configuration (hotspot will still work locally)."
        return
    fi

    # Flush existing NAT rules
    iptables -t nat -F 2>/dev/null || true

    # NAT masquerade on eth0 (if connected to internet via ethernet)
    iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
    iptables -A FORWARD -i eth0 -o "$INTERFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT
    iptables -A FORWARD -i "$INTERFACE" -o eth0 -j ACCEPT

    # Allow traffic to TempBot web dashboard
    iptables -A INPUT -i "$INTERFACE" -p tcp --dport 5000 -j ACCEPT

    # Save iptables rules
    mkdir -p /etc/iptables
    iptables-save > /etc/iptables/rules.v4

    # Create systemd service to restore iptables on boot
    cat > /etc/systemd/system/iptables-restore.service <<'IEOF'
[Unit]
Description=Restore iptables rules
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
ExecStart=/sbin/iptables-restore /etc/iptables/rules.v4
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
IEOF

    systemctl daemon-reload
    systemctl enable iptables-restore.service

    info "iptables NAT rules configured and saved."
}

# ---------------------------------------------------------------------------
# 7. Create systemd services
# ---------------------------------------------------------------------------

create_services() {
    info "--- Enabling services ---"

    # Unmask hostapd (it's masked by default on some distros)
    systemctl unmask hostapd 2>/dev/null || true

    # Enable services to start on boot
    systemctl enable hostapd
    systemctl enable dnsmasq

    # Restart networking to apply changes
    systemctl restart dhcpcd 2>/dev/null || true

    info "Services enabled for auto-start on boot."
}

# ---------------------------------------------------------------------------
# 8. Start the hotspot
# ---------------------------------------------------------------------------

start_hotspot() {
    info "--- Starting hotspot services ---"

    systemctl restart hostapd
    systemctl restart dnsmasq

    # Verify services are running
    sleep 2

    if systemctl is-active --quiet hostapd; then
        info "hostapd is running."
    else
        err "hostapd failed to start. Check: journalctl -u hostapd"
    fi

    if systemctl is-active --quiet dnsmasq; then
        info "dnsmasq is running."
    else
        err "dnsmasq failed to start. Check: journalctl -u dnsmasq"
    fi
}

# ---------------------------------------------------------------------------
# Remove / uninstall
# ---------------------------------------------------------------------------

remove_hotspot() {
    info "=== Removing TempBot Hotspot Configuration ==="

    # Stop services
    systemctl stop hostapd 2>/dev/null || true
    systemctl stop dnsmasq 2>/dev/null || true
    systemctl disable hostapd 2>/dev/null || true
    systemctl disable dnsmasq 2>/dev/null || true

    # Restore dhcpcd.conf
    local DHCPCD_CONF="/etc/dhcpcd.conf"
    if [[ -f "$DHCPCD_CONF" ]]; then
        sed -i '/# TempBot Hotspot Config START/,/# TempBot Hotspot Config END/d' "$DHCPCD_CONF"
    fi

    # Restore original dnsmasq.conf
    if [[ -f /etc/dnsmasq.conf.orig ]]; then
        mv /etc/dnsmasq.conf.orig /etc/dnsmasq.conf
    fi

    # Restore backed-up files
    for bak in "${BACKUP_DIR}"/*.bak; do
        if [[ -f "$bak" ]]; then
            local orig_name
            orig_name="$(basename "$bak" .bak)"
            # Determine the original path based on known files
            case "$orig_name" in
                hostapd.conf) cp "$bak" /etc/hostapd/hostapd.conf ;;
                dhcpcd.conf)  cp "$bak" /etc/dhcpcd.conf ;;
                sysctl.conf)  cp "$bak" /etc/sysctl.conf ;;
            esac
        fi
    done

    # Remove iptables service
    rm -f /etc/systemd/system/iptables-restore.service
    rm -f /etc/iptables/rules.v4
    systemctl daemon-reload

    # Flush iptables
    iptables -t nat -F 2>/dev/null || true
    iptables -F 2>/dev/null || true

    info "Hotspot configuration removed."
    info "Reboot to fully restore networking: sudo reboot"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    check_root

    if [[ "${1:-}" == "--remove" ]]; then
        remove_hotspot
        exit 0
    fi

    echo ""
    info "=========================================="
    info " TempBot WiFi Hotspot Setup"
    info "=========================================="
    echo ""
    info "SSID:     $SSID"
    info "Password: $WPA_PASSPHRASE"
    info "IP:       $IP_ADDRESS"
    info "DHCP:     $DHCP_RANGE_START - $DHCP_RANGE_END"
    echo ""

    check_interface
    create_backup
    install_packages
    configure_static_ip
    configure_hostapd
    configure_dnsmasq
    enable_ip_forwarding
    configure_iptables
    create_services
    start_hotspot

    echo ""
    info "=========================================="
    info " Hotspot Setup Complete!"
    info "=========================================="
    echo ""
    info "WiFi Network:  $SSID"
    info "Password:      $WPA_PASSPHRASE"
    info "Dashboard URL: http://$IP_ADDRESS:5000"
    echo ""
    info "Connect your phone/laptop to '$SSID' and open"
    info "http://$IP_ADDRESS:5000 in a browser."
    echo ""
    info "To remove: sudo bash $SCRIPT_NAME --remove"
}

main "$@"
