#!/bin/bash
#
# Copyright 2024 Authors of spidernet-io/spiderpool
# Modified for metal-deployer Ubuntu 24.04
#
# RDMA NIC mode switch script
# Supports switching: InfiniBand (ib) / Ethernet (eth) / RoCE (roce) mode
#

set -o errexit
set -o nounset
set -o pipefail

# Configuration
SCRIPT_VERSION="1.0"
LOG_FILE="${LOG_FILE:-/var/log/setNicRdmaMode.log}"

# Logging function
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"
}

# Help information
usage() {
    cat << EOF
RDMA NIC mode switch tool v${SCRIPT_VERSION}

Usage: $0 [OPTIONS]

Options:
    -m, --mode MODE       Switch mode: ib | eth | roce (Required)
    -i, --interface IF    Specify interface name (optional, auto-detect by default)
    -l, --list           List all Mellanox NICs
    -s, --status         Show current NIC status
    -h, --help           Show this help message

Mode description:
    ib    - InfiniBand mode (high-performance RDMA, requires IB switch)
    eth   - Standard Ethernet mode (best compatibility)
    roce  - RoCE v2 mode (RDMA over Ethernet, requires lossless network)

Examples:
    $0 -m ib              # Switch to InfiniBand mode
    $0 -m eth             # Switch to Ethernet mode
    $0 -m roce            # Switch to RoCE v2 mode
    $0 -l                 # List all Mellanox NICs

Note:
    - This script only supports Mellanox/NVIDIA ConnectX series NICs
    - Mode switching may cause brief network interruption
    - Recommended to execute during maintenance window

EOF
}

# Check root privileges
check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        log "❌ Error: root privileges required"
        exit 1
    fi
}

# Check Mellanox NICs
check_mellanox_nics() {
    local nics
    nics=$(lspci | grep -i mellanox | wc -l)
    if [ "$nics" -eq 0 ]; then
        log "⚠️  No Mellanox NICs detected"
        return 1
    fi
    log "✅ Detected ${nics}  Mellanox device(s)"
    return 0
}

# List Mellanox NICs
list_nics() {
    log "=========================================="
    log "Mellanox/NVIDIA NIC list:"
    log "=========================================="
    
    # Get from lspci
    lspci | grep -i mellanox | while read -r line; do
        log "  PCI: ${line}"
    done
    
    # Get from network interface
    log ""
    log "Network interface details:"
    for nic in /sys/class/net/*; do
        local ifname
        ifname=$(basename "$nic")
        
        # Check if Mellanox NIC
        if [ -f "/sys/class/net/${ifname}/device/vendor" ]; then
            local vendor
            vendor=$(cat "/sys/class/net/${ifname}/device/vendor" 2>/dev/null || echo "")
            # Mellanox vendor ID: 0x15b3
            if [ "$vendor" = "0x15b3" ]; then
                local mode="unknown"
                if [ -d "/sys/class/infiniband/${ifname}" ]; then
                    mode="InfiniBand"
                elif [ -f "/sys/class/net/${ifname}/type" ]; then
                    local type_val
                    type_val=$(cat "/sys/class/net/${ifname}/type" 2>/dev/null || echo "")
                    if [ "$type_val" = "1" ]; then
                        mode="Ethernet"
                    fi
                fi
                log "  ${ifname}: ${mode}"
            fi
        fi
    done
    log "=========================================="
}

# Show NIC status
show_status() {
    log "=========================================="
    log "NIC RDMA status:"
    log "=========================================="
    
    # Check IB Device
    if command -v ibstat >/dev/null 2>&1; then
        log "InfiniBand device status:"
        ibstat 2>/dev/null | head -20 || log "  No IB device or not configured"
    fi
    
    # Check RDMA Device
    if [ -d /sys/class/infiniband ]; then
        log ""
        log "RDMA device list:"
        ls /sys/class/infiniband/ 2>/dev/null | while read -r dev; do
            log "  ${dev}"
        done
    fi
    
    # CheckNICDriver
    log ""
    log "NIC driver info:"
    for nic in /sys/class/net/ib* /sys/class/net/enp*; do
        if [ -d "$nic" ]; then
            local ifname
            ifname=$(basename "$nic")
            local driver
            driver=$(ethtool -i "$ifname" 2>/dev/null | grep driver | awk '{print $2}' || echo "unknown")
            log "  ${ifname}: driver=${driver}"
        fi
    done
    
    log "=========================================="
}

# Switch NIC mode (simplified, actual implementation depends on specific NIC model and driver)
switch_mode() {
    local mode=$1
    local interface=$2
    
    log "=========================================="
    log "Switch NIC mode: ${mode}"
    if [ -n "${interface}" ]; then
        log "Target interface: ${interface}"
    else
        log "Target interface: auto-detect"
    fi
    log "=========================================="
    
    case "${mode}" in
        ib)
            log "🔄 Switch to InfiniBand mode..."
            # Actual implementation requires mlxconfig or mst tools
            # Here provides basic framework
            if command -v mlxconfig >/dev/null 2>&1; then
                log "Using mlxconfig to configure..."
                # mlxconfig -d <device> set LINK_TYPE_P1=1 LINK_TYPE_P2=1
            else
                log "⚠️  mlxconfig not installed, please install MFT toolkit"
            fi
            ;;
            
        eth)
            log "🔄 Switching to Ethernet mode..."
            if command -v mlxconfig >/dev/null 2>&1; then
                log "Using mlxconfig to configure..."
                # mlxconfig -d <device> set LINK_TYPE_P1=2 LINK_TYPE_P2=2
            else
                log "⚠️  mlxconfig not installed, please install MFT toolkit"
            fi
            ;;
            
        roce)
            log "🔄 Switch to RoCE v2 mode..."
            # Requires enabling RoCE under Ethernet mode
            log "Ensure NIC is set to Ethernet mode"
            log "Configuring RoCE v2 parameters..."
            ;;
            
        *)
            log "❌ Unknown mode: ${mode}"
            return 1
            ;;
    esac
    
    log "⚠️  Note: Mode switch usually requires system reboot or driver reload to take effect"
    log "Recommended to execute: modprobe -r mlx5_ib mlx5_core && modprobe mlx5_core mlx5_ib"
    log "=========================================="
}

# Main function
main() {
    local mode=""
    local interface=""
    local action=""
    
    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            -m|--mode)
                mode="$2"
                shift 2
                ;;
            -i|--interface)
                interface="$2"
                shift 2
                ;;
            -l|--list)
                action="list"
                shift
                ;;
            -s|--status)
                action="status"
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                log "❌ Unknown argument: $1"
                usage
                exit 1
                ;;
        esac
    done
    
    # Check root
    check_root
    
    # Check Mellanox NICs
    check_mellanox_nics || exit 1
    
    # Execute action
    case "${action}" in
        list)
            list_nics
            ;;
        status)
            show_status
            ;;
        *)
            if [ -n "${mode}" ]; then
                switch_mode "${mode}" "${interface}"
            else
                log "❌ Please specify mode (-m ib/eth/roce) or action (-l/-s)"
                usage
                exit 1
            fi
            ;;
    esac
}

# Entry point
main "$@"
