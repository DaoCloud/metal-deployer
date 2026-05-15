#!/bin/bash

# ==============================================================================
# QEMU ISO automated test tool (ultimate fix)
# Features: smart accelerate setup / force clean / login / pull files
# ==============================================================================

set -o pipefail

# --- Global configuration ---
BASE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CI_WORK_DIR="${CI_WORK_DIR:-${BASE_DIR}/.ci-work}"
TEST_WORK_DIR="${TEST_WORK_DIR:-${CI_WORK_DIR}}"

ISO_FILE="${ISO_FILE:-${CI_WORK_DIR}/custom-ubuntu.iso}"
DISK_IMG="${DISK_IMG:-${TEST_WORK_DIR}/test_disk.qcow2}"
PID_FILE="${PID_FILE:-${TEST_WORK_DIR}/qemu.pid}"
LOG_FILE="${LOG_FILE:-${TEST_WORK_DIR}/qemu.log}"
SSH_PORT=5555
SSH_USER="admin"
SSH_PASS="admin" # Note: Ensure this matches the password in config/cloud-init/user-data
DISK_SIZE="${DISK_SIZE:-40G}"
VM_MEMORY="${VM_MEMORY:-8G}"
GPU_PASSTHROUGH="${GPU_PASSTHROUGH:-auto}"
GPU_PCI_ADDR="${GPU_PCI_ADDR:-}"

# Base timeout (seconds)
TIMEOUT=2400
FIRST_BOOT_TIMEOUT="${FIRST_BOOT_TIMEOUT:-3600}"
SSH_PROBE_TIMEOUT=15
FIRST_BOOT_TIMEOUT="${FIRST_BOOT_TIMEOUT:-3600}"

ssh_vm() {
    sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -p "$SSH_PORT" "$SSH_USER@localhost" "$@"
}

dump_first_boot_logs() {
    echo "📄 first-boot diagnostic logs："
    ssh_vm "echo '$SSH_PASS' | sudo -S bash -c '
        set +e
        echo \"===== cloud-init status =====\"
        cloud-init status --long || true

        echo \"===== /root/installed =====\"
        ls -l /root/installed || true

        echo \"===== running init processes =====\"
        ps -ef | grep -E \"cloud-init|init_system|/opt/resource/scripts|apt-get|dpkg|nvidia|cuda\" | grep -v grep || true

        echo \"===== /var/log/cloud-init-output.log tail =====\"
        tail -n 200 /var/log/cloud-init-output.log || true

        echo \"===== /var/log/cloud-init.log tail =====\"
        tail -n 120 /var/log/cloud-init.log || true

        echo \"===== /var/log/scripts.log tail =====\"
        tail -n 200 /var/log/scripts.log || true

        echo \"===== /var/log/metal-deployer/install-summary.log =====\"
        cat /var/log/metal-deployer/install-summary.log || true

        echo \"===== /var/log/metal-deployer/*.log tails =====\"
        for f in /var/log/metal-deployer/*.log; do
            [ -f \"\$f\" ] || continue
            echo \"----- \$f -----\"
            tail -n 120 \"\$f\" || true
        done
    '" || true
}

dump_qemu_log() {
    if [ -f "$LOG_FILE" ]; then
        echo "📄 QEMU Serial log tail："
        tail -n 300 "$LOG_FILE" || true
    else
        echo "📄 QEMU Serial logdoes not exist: ${LOG_FILE}"
    fi
}

# --- Check dependencies ---
check_deps() {
    local tools="qemu-system-x86_64 sshpass qemu-img scp lspci"
    for tool in $tools; do
        if ! command -v $tool &> /dev/null; then
            echo "❌ Error: command not found $tool,Please install first."
            exit 1
        fi
    done
}

configure_gpu_passthrough() {
    QEMU_GPU_ARGS=()

    case "$GPU_PASSTHROUGH" in
        0|false|False|FALSE|no|No|NO|off|Off|OFF)
            echo "🎛️  [GPU] GPU passthrough disabled."
            return 0
            ;;
        auto|1|true|True|TRUE|yes|Yes|YES|on|On|ON)
            ;;
        *)
            echo "❌ Error: GPU_PASSTHROUGH must be auto/true/false,Current value: $GPU_PASSTHROUGH" >&2
            exit 1
            ;;
    esac

    local selected_gpu="$GPU_PCI_ADDR"
    local addr

    if [ -z "$selected_gpu" ]; then
        while read -r addr _; do
            if lspci -nnk -s "$addr" | grep -q 'Kernel driver in use: vfio-pci'; then
                selected_gpu="$addr"
                break
            fi
        done < <(lspci -Dnnd 10de: | awk '/\[(0300|0302)\]/ {print $1}')
    fi

    if [ -z "$selected_gpu" ]; then
        if [ "$GPU_PASSTHROUGH" = "auto" ]; then
            echo "🎛️  [GPU] No NVIDIA GPU bound to vfio-pci found, skipping GPU passthrough."
            return 0
        fi
        echo "❌ Error: No pass-through capable NVIDIA GPU found. Please confirm GPU is bound to vfio-pci, or set GPU_PCI_ADDR." >&2
        exit 1
    fi

    if ! lspci -nnk -s "$selected_gpu" | grep -q 'Kernel driver in use: vfio-pci'; then
        echo "❌ Error: GPU $selected_gpu not bound to vfio-pci, cannot safely pass through." >&2
        lspci -nnk -s "$selected_gpu" >&2 || true
        exit 1
    fi

    if [ ! -e /dev/vfio/vfio ]; then
        echo "❌ Error: /dev/vfio/vfio does not exist, please confirm IOMMU/vfio is enabled." >&2
        exit 1
    fi

    local iommu_group
    iommu_group="$(basename "$(readlink -f "/sys/bus/pci/devices/${selected_gpu}/iommu_group")")"
    if [ -z "$iommu_group" ] || [ ! -e "/dev/vfio/${iommu_group}" ]; then
        echo "❌ Error: GPU $selected_gpu  vfio group device does not exist." >&2
        exit 1
    fi

    echo "🎛️  [GPU] Enable NVIDIA GPU passthrough: $selected_gpu (IOMMU group: $iommu_group)"
    QEMU_GPU_ARGS=(-device "vfio-pci,host=${selected_gpu}")
}

# --- Helper: check if VM is running ---
check_vm_running() {
    if [ ! -f "$PID_FILE" ] || ! kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "❌ Error: VM is not running. Please run setup first."
        exit 1
    fi
}

# --- Subcommand: CLEAN (cleanup) ---
do_clean() {
    echo "🧹 [Clean] Start cleaning environment..."
    
    # 1. Try killing process via PID file
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "   -> [PID] Stop QEMU process (PID: $PID)..."
            kill -9 "$PID" 2>/dev/null
        fi
        rm -f "$PID_FILE"
    fi

    # 2. [New] Double insurance: kill by disk image filename
    # Prevent PID file recording error (e.g. recording tee's PID), causing QEMU to not die
    # pgrep -f matches full command line parameters, find processes using current disk image
    if pgrep -f "$DISK_IMG" >/dev/null; then
        echo "   -> [Force] Detected residual QEMU process, force clearing..."
        pkill -f "$DISK_IMG"
    fi

    # 3. Delete disk file
    if [ -f "$DISK_IMG" ]; then
        echo "   -> Delete virtual disk $DISK_IMG"
        rm -f "$DISK_IMG"
    fi
    
    echo "✅ Environment cleanup complete."
}

# --- Subcommand: SETUP (install) ---
do_setup() {
    check_deps
    mkdir -p "$TEST_WORK_DIR"
    
    # Check if residual processes exist
    if pgrep -f "$DISK_IMG" >/dev/null; then
        echo "⚠️  Warning: Detected QEMU already running."
        echo "   Please first run '$0 clean' to perform cleanup."
        exit 1
    fi

    echo "🚀 [Setup] Start test: $ISO_FILE"

    # KVM detection logic
    QEMU_ACCEL_ARGS=""
    SMP_CORES="2"
    if [ -e /dev/kvm ] && [ -w /dev/kvm ]; then
        echo "⚡ [KVM] Hardware acceleration detected, enabling KVM mode."
        QEMU_ACCEL_ARGS="-enable-kvm -cpu host"
        SMP_CORES="4"
        TIMEOUT=2400
    else
        echo "🐢 [KVM] No available KVM detected, using software emulation mode (slower)."
    fi
    configure_gpu_passthrough

    echo "💿 Create disk..."
    rm -f "$DISK_IMG"
    qemu-img create -f qcow2 "$DISK_IMG" "$DISK_SIZE" > /dev/null

    echo "🔥 Start VM (Log: $LOG_FILE)..."
    
    # [Key fix] Use > >(tee ...) syntax
    # so qemu is the main process, $! obtained is qemu's PID, instead of tee's PID
    qemu-system-x86_64 \
      $QEMU_ACCEL_ARGS \
      -m "$VM_MEMORY" \
      -smp "$SMP_CORES" \
      -hda "$DISK_IMG" \
      -cdrom "$ISO_FILE" \
      -boot once=d \
      -nographic \
      -serial mon:stdio \
      "${QEMU_GPU_ARGS[@]}" \
      -net nic -net user,hostfwd=tcp::${SSH_PORT}-:22 \
      > >(tee "$LOG_FILE") 2>&1 &
    
    QEMU_PID=$!
    echo "$QEMU_PID" > "$PID_FILE"
    echo "   -> QEMU PID: $QEMU_PID"

    echo "⏳ Installing in background, waiting for SSH..."
    START_TIME=$(date +%s)
    
    while true; do
        CURRENT_TIME=$(date +%s)
        ELAPSED=$((CURRENT_TIME - START_TIME))
        
        if [ "$ELAPSED" -gt "$TIMEOUT" ]; then
            echo "❌ [Timeout] Installation timeout!"
            dump_qemu_log
            exit 1
        fi
        
        # Process alive check
        if ! kill -0 "$QEMU_PID" 2>/dev/null; then 
            echo "❌ [Error] QEMU process exited unexpectedly! Please check logs."
            dump_qemu_log
            exit 1
        fi

        if timeout "$SSH_PROBE_TIMEOUT" sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
           -o ConnectTimeout=2 -p "$SSH_PORT" "$SSH_USER@localhost" "echo 'Ready'" >/dev/null 2>&1; then
            echo ""
            echo "✅ SSH connection successful! System is ready."
            break
        fi
        sleep 10
    done

    echo "⏳ Wait for cloud-init/first-boot scripts to complete..."
    if ! sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -p $SSH_PORT $SSH_USER@localhost \
        "echo '$SSH_PASS' | sudo -S timeout \"$FIRST_BOOT_TIMEOUT\" bash -c '
            while [ ! -f /root/installed ]; do
                if ! pgrep -af \"/opt/resource/scripts/init_system.sh|/var/lib/cloud/instance/scripts/runcmd\" \
                    | grep -v -E \"pgrep|grep|timeout .*bash -c\" >/dev/null; then
                    echo \"first-boot script is no longer running and /root/installed is missing\" >&2
                    exit 2
                fi
                sleep 10
            done
        '"; then
        echo "❌ first-boot script did not complete normally, please check /var/log/cloud-init-output.log and /var/log/scripts.log."
        dump_first_boot_logs
        exit 1
    fi

    echo "🔍 Execute automated verification..."
    # More detailed verification logic added here
    TEST_CMD="
    if [ -d /opt/resource/scripts ]; then
        echo '   [Check] Script directory exists: OK'
    else
        echo '   [Check] Script directory missing: FAIL'; exit 1
    fi
    if [ -d /opt/resource/config ]; then
        echo '   [Check] Config directory exists: OK'
    else
        echo '   [Check] Config directory missing: FAIL'; exit 1
    fi
    package_count=\$(find /opt/resource/packages -maxdepth 1 -type f | wc -l)
    if [ \"\$package_count\" -ge 20 ]; then
        echo \"   [Check] Packages copied: OK (\${package_count} files)\"
    else
        echo \"   [Check] Packages count abnormal: FAIL (\${package_count} files)\"; exit 1
    fi
    if grep -q 'Metal deployer first boot initialization started' /var/log/metal-deployer/install-summary.log; then
        echo '   [Check] First-boot script executed: OK'
    else
        echo '   [Check] First-boot script not executed: FAIL'; exit 1
    fi
    "
    if ssh_vm "$TEST_CMD"; then
        echo "✅✅✅ Verification passed! ✅✅✅"
    else
        echo "❌❌❌ Verification failed ❌❌❌"
        dump_first_boot_logs
        exit 1
    fi
}

# --- Subcommand: LOGIN (login) ---
do_login() {
    check_vm_running
    echo "🔑 Logging in..."
    sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -p $SSH_PORT $SSH_USER@localhost
}

# --- Subcommand: PULL (copy files from VM) ---
do_pull() {
    check_vm_running
    REMOTE_PATH="$1"
    
    if [ -z "$REMOTE_PATH" ]; then
        echo "❌ Usage error: Please specify the remote path to pull."
        echo "   Examples: $0 pull /var/log/custom-setup.log"
        exit 1
    fi

    echo "📥 Pulling from VM: $REMOTE_PATH"
    echo "   -> Target: $(pwd)/"
    
    # Use scp -r (recursive) -P (specify port)
    sshpass -p "$SSH_PASS" scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -P $SSH_PORT -r "$SSH_USER@localhost:$REMOTE_PATH" .
    
    if [ $? -eq 0 ]; then
        echo "✅ Pull successful!"
        LOCAL_NAME=$(basename "$REMOTE_PATH")
        ls -ld "$LOCAL_NAME"
    else
        echo "❌ Pull failed (please check if path exists or root privileges are needed)"
    fi
}

main() {
    case "$1" in
        setup)
            do_setup
            ;;
        clean)
            do_clean
            ;;
        login)
            do_login
            ;;
        pull)
            shift
            do_pull "$@"
            ;;
        *)
            echo "Usage: $0 {setup|clean|login|pull}"
            echo "  setup           : Create and start test"
            echo "  login           : SSH login to VM"
            echo "  pull <VM_Path>  : Copy files from VM to current directory"
            echo "  clean           : Clean environment"
            exit 1
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
