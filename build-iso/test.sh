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
PRESERVE_TEST_LOGS="${PRESERVE_TEST_LOGS:-false}"
FAILURE_LOG_DIR="${FAILURE_LOG_DIR:-${TEST_WORK_DIR}/failure-logs}"
DUMP_FAILURE_LOGS="${DUMP_FAILURE_LOGS:-true}"
SSH_PORT=5555
SSH_USER="admin"
SSH_PASS="admin" # Note: Ensure this matches the password in config/cloud-init/user-data
DISK_SIZE="${DISK_SIZE:-40G}"
VM_MEMORY="${VM_MEMORY:-8G}"
GPU_PASSTHROUGH="${GPU_PASSTHROUGH:-auto}"
GPU_PCI_ADDR="${GPU_PCI_ADDR:-}"
NET_RESTRICT="${NET_RESTRICT:-false}"

# Base timeout (seconds)
TIMEOUT=2400
FIRST_BOOT_TIMEOUT="${FIRST_BOOT_TIMEOUT:-3600}"
SSH_PROBE_TIMEOUT=15

ssh_vm() {
    sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -p "$SSH_PORT" "$SSH_USER@localhost" "$@"
}

export_failure_logs() {
    mkdir -p "$FAILURE_LOG_DIR"

    if [ -f "$LOG_FILE" ]; then
        cp -f "$LOG_FILE" "$FAILURE_LOG_DIR/qemu.log" 2>/dev/null || true
    fi

    if ssh_vm "echo ready" >/dev/null 2>&1; then
        sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -p "$SSH_PORT" "$SSH_USER@localhost" "echo '$SSH_PASS' | sudo -S bash -c '
                set +e
                tar -C / -czf /tmp/metal-deployer-failure-logs.tgz \
                    var/log/scripts.log \
                    var/log/metal-deployer \
                    var/log/cloud-init-output.log \
                    var/log/cloud-init.log \
                    2>/dev/null || true
            '" >/dev/null 2>&1 || true

        sshpass -p "$SSH_PASS" scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -P "$SSH_PORT" "$SSH_USER@localhost:/tmp/metal-deployer-failure-logs.tgz" \
            "$FAILURE_LOG_DIR/metal-deployer-failure-logs.tgz" >/dev/null 2>&1 || true

        if [ -f "$FAILURE_LOG_DIR/metal-deployer-failure-logs.tgz" ]; then
            tar -C "$FAILURE_LOG_DIR" -xzf "$FAILURE_LOG_DIR/metal-deployer-failure-logs.tgz" >/dev/null 2>&1 || true
            chmod -R u+rwX,go+rX "$FAILURE_LOG_DIR" >/dev/null 2>&1 || true
        fi
    fi
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

dump_vm_install_state() {
    echo "📊 VM install state："
    ssh_vm "echo '$SSH_PASS' | sudo -S bash -c '
        set +e
        echo \"===== hostname =====\"
        hostname

        echo \"===== cloud-init status =====\"
        cloud-init status --long || true

        echo \"===== /root/installed =====\"
        ls -l /root/installed || true

        echo \"===== install-summary tail =====\"
        tail -n 120 /var/log/metal-deployer/install-summary.log || true

        echo \"===== scripts tail =====\"
        tail -n 120 /var/log/scripts.log || true

        echo \"===== stage status =====\"
        for stage in apt_sources base_packages offline_debs doca_install gpu_debs rdma_modules ssh_keys container_runtime docker_images cpu_performance nvidia_setup hpcx_install gdrcopy_build test_tools_build gpu_burn; do
            stage_line=\$(grep -E \"= (START|DONE |FAIL ) +\${stage}( | =| rc=)\" /var/log/metal-deployer/install-summary.log 2>/dev/null | tail -n 1 || true)
            case \"\$stage_line\" in
                *\"= FAIL  \"*) echo \"\${stage}: FAIL\" ;;
                *\"= DONE  \"*) echo \"\${stage}: DONE\" ;;
                *\"= START \"*) echo \"\${stage}: RUNNING\" ;;
                *) echo \"\${stage}: SKIP/ABSENT\" ;;
            esac
        done

        echo \"===== key packages =====\"
        dpkg -l | awk '\\''\$1==\"ii\" && (\$2 ~ /^(nvidia|cuda|rdma|docker|containerd|ibverbs|infiniband|doca|libnvidia-container|nvidia-container-toolkit)/) {print \$2, \$3}'\\'' | sort || true

        echo \"===== key commands =====\"
        for cmd in cloud-init docker containerd dockerd nvidia-smi ibv_devinfo ibstat hpcx_load gcc make dkms; do
            if [ \"\$cmd\" = \"hpcx_load\" ] && [ -f /etc/profile.d/hpcx.sh ]; then
                # hpcx_load comes from hpcx-init.sh, source profile first.
                # shellcheck disable=SC1091
                . /etc/profile.d/hpcx.sh >/dev/null 2>&1 || true
            fi
            if command -v \"\$cmd\" >/dev/null 2>&1; then
                echo \"\$cmd: present -> \$(command -v \"\$cmd\")\"
            else
                echo \"\$cmd: missing\"
            fi
        done

        echo \"===== hpcx files =====\"
        ls -l /opt/hpcx/hpcx-init.sh /etc/profile.d/hpcx.sh 2>/dev/null || true
        if [ -n \"\${HPCX_HOME:-}\" ]; then
            echo \"HPCX_HOME=\${HPCX_HOME}\"
        fi

        echo \"===== service status =====\"
        systemctl is-active docker 2>/dev/null || true
        systemctl is-active containerd 2>/dev/null || true
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

dump_failure_logs_if_enabled() {
    if ! is_truthy "$DUMP_FAILURE_LOGS"; then
        return 0
    fi

    "$@"
}

is_truthy() {
    case "$1" in
        1|true|True|TRUE|yes|Yes|YES|on|On|ON)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

remove_empty_work_dirs() {
    local dir="$TEST_WORK_DIR"

    while [ -n "$dir" ] && [ "$dir" != "/" ]; do
        if [ ! -d "$dir" ]; then
            break
        fi
        if rmdir "$dir" 2>/dev/null; then
            echo "   -> Remove empty work directory $dir"
        else
            break
        fi

        if [ "$dir" = "$CI_WORK_DIR" ]; then
            break
        fi

        case "$dir" in
            "$CI_WORK_DIR"/*)
                dir="$(dirname "$dir")"
                ;;
            *)
                break
                ;;
        esac
    done
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

find_test_qemu_pids() {
    local proc pid cmdline exe
    local port_arg="hostfwd=tcp::${SSH_PORT}-:22"

    for proc in /proc/[0-9]*; do
        pid="${proc##*/}"
        [ -r "$proc/cmdline" ] || continue

        cmdline="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
        [ -n "$cmdline" ] || continue

        exe="${cmdline%% *}"
        [ -n "$exe" ] || continue
        [ "${exe##*/}" = "qemu-system-x86_64" ] || continue

        if [[ "$cmdline" == *"$DISK_IMG"* ]] ||
           [[ "$cmdline" == *"$ISO_FILE"* ]] ||
           [[ "$cmdline" == *"$port_arg"* ]]; then
            echo "$pid"
        fi
    done
}

find_ci_qemu_pids() {
    local proc pid cmdline exe pid_file pid_from_file
    local port_arg="hostfwd=tcp::${SSH_PORT}-:22"

    for pid_file in "$PID_FILE"; do
        if [ -f "$pid_file" ]; then
            pid_from_file="$(cat "$pid_file" 2>/dev/null || true)"
            if [ -n "$pid_from_file" ] && kill -0 "$pid_from_file" 2>/dev/null; then
                echo "$pid_from_file"
            fi
        fi
    done

    if [ -d "$CI_WORK_DIR" ]; then
        while IFS= read -r pid_file; do
            pid_from_file="$(cat "$pid_file" 2>/dev/null || true)"
            if [ -n "$pid_from_file" ] && kill -0 "$pid_from_file" 2>/dev/null; then
                echo "$pid_from_file"
            fi
        done < <(find "$CI_WORK_DIR" -type f -name qemu.pid 2>/dev/null)
    fi

    for proc in /proc/[0-9]*; do
        pid="${proc##*/}"
        [ -r "$proc/cmdline" ] || continue

        cmdline="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
        [ -n "$cmdline" ] || continue

        exe="${cmdline%% *}"
        [ -n "$exe" ] || continue
        [ "${exe##*/}" = "qemu-system-x86_64" ] || continue

        if [[ "$cmdline" == *"$CI_WORK_DIR"* ]] ||
           [[ "$cmdline" == *"$DISK_IMG"* ]] ||
           [[ "$cmdline" == *"$ISO_FILE"* ]] ||
           [[ "$cmdline" == *"$port_arg"* ]]; then
            echo "$pid"
        fi
    done
}

stop_pids() {
    local pids=("$@")
    local pid alive=()

    [ "${#pids[@]}" -gt 0 ] || return 0

    for pid in "${pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done

    sleep 2

    for pid in "${pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            alive+=("$pid")
        fi
    done

    if [ "${#alive[@]}" -gt 0 ]; then
        echo "   -> [Force] QEMU still running, force kill: ${alive[*]}"
        for pid in "${alive[@]}"; do
            kill -9 "$pid" 2>/dev/null || true
        done
    fi
}

clean_ci_work_dir() {
    local dir="$CI_WORK_DIR"
    local resolved_base resolved_dir

    if [ -z "$dir" ] || [ "$dir" = "/" ]; then
        echo "❌ Refuse to remove unsafe CI_WORK_DIR: ${dir:-<empty>}" >&2
        exit 1
    fi

    if ! resolved_dir="$(realpath -m -- "$dir" 2>/dev/null)"; then
        echo "❌ Refuse to remove unresolved CI_WORK_DIR: $dir" >&2
        exit 1
    fi

    resolved_base="$(cd "$BASE_DIR" && pwd -P)"
    if [ "$resolved_dir" = "$resolved_base" ] || [ "$resolved_dir" = "$(dirname "$resolved_base")" ]; then
        echo "❌ Refuse to remove unsafe CI_WORK_DIR: $resolved_dir" >&2
        exit 1
    fi

    if [ -d "$resolved_dir" ]; then
        echo "   -> Delete CI work directory $resolved_dir"
        rm -rf "$resolved_dir"
    fi
}

# --- Subcommand: CLEAN (cleanup) ---
do_clean() {
    echo "🧹 [Clean] Start cleaning environment..."
    local PID
    local QEMU_PIDS=()
    
    # 1. Try killing process via PID file
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "   -> [PID] Stop QEMU process (PID: $PID)..."
            QEMU_PIDS+=("$PID")
        fi
        rm -f "$PID_FILE"
    fi

    # 2. Double insurance: find QEMU by CI work dir, disk, ISO, PID files, or forwarded SSH port.
    # This handles missing/stale PID files and different TEST_WORK_DIR values.
    while read -r PID; do
        [ -n "$PID" ] || continue
        QEMU_PIDS+=("$PID")
    done < <(find_ci_qemu_pids)

    if [ "${#QEMU_PIDS[@]}" -gt 0 ]; then
        mapfile -t QEMU_PIDS < <(printf '%s\n' "${QEMU_PIDS[@]}" | sort -u)
        echo "   -> Stop QEMU process(es): ${QEMU_PIDS[*]}"
        stop_pids "${QEMU_PIDS[@]}"
    fi

    # 3. Delete all artifacts for the current CI workspace unless caller wants
    # logs/artifacts preserved for post-failure reporting.
    if is_truthy "$PRESERVE_TEST_LOGS"; then
        echo "   -> Preserve test logs and CI work directory."
    else
        clean_ci_work_dir
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
    QEMU_NET_ARGS=(-netdev "user,id=net0,hostfwd=tcp::${SSH_PORT}-:22" -device "e1000,netdev=net0")
    if is_truthy "$NET_RESTRICT"; then
        echo "🌐 [NET] Restrict guest outbound network; keep SSH host forwarding."
        QEMU_NET_ARGS=(-netdev "user,id=net0,restrict=on,hostfwd=tcp::${SSH_PORT}-:22" -device "e1000,netdev=net0")
    fi

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
      "${QEMU_NET_ARGS[@]}" \
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
            export_failure_logs
            dump_failure_logs_if_enabled dump_qemu_log
            exit 1
        fi
        
        # Process alive check
        if ! kill -0 "$QEMU_PID" 2>/dev/null; then 
            echo "❌ [Error] QEMU process exited unexpectedly! Please check logs."
            export_failure_logs
            dump_failure_logs_if_enabled dump_qemu_log
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
    FIRST_BOOT_WAIT_START=$(date +%s)
    # init_system.sh runs in cloud-init's final stage (scripts-user / runcmd),
    # which starts only after sshd is reachable. cloud-init can report done before
    # the runcmd child creates /root/installed, so poll the marker directly.
    if ! sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -p $SSH_PORT $SSH_USER@localhost \
        "echo '$SSH_PASS' | sudo -S timeout \"$FIRST_BOOT_TIMEOUT\" bash -c '
            cloud-init status --wait >/dev/null 2>&1 || true

            while true; do
                if [ -f /root/installed ]; then
                    exit 0
                fi

                if grep -q \"========== FAIL\" /var/log/metal-deployer/install-summary.log 2>/dev/null; then
                    echo \"first-boot stage failed before /root/installed marker was created\" >&2
                    exit 2
                fi

                sleep 5
            done
        '"; then
        echo "❌ first-boot script did not complete normally, please check /var/log/cloud-init-output.log and /var/log/scripts.log."
        export_failure_logs
        dump_failure_logs_if_enabled dump_first_boot_logs
        exit 1
    fi
    FIRST_BOOT_WAIT_END=$(date +%s)
    echo "✅ first-boot wait done in $((FIRST_BOOT_WAIT_END - FIRST_BOOT_WAIT_START))s"

    dump_vm_install_state

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
    if echo '$SSH_PASS' | sudo -S grep -q 'Metal deployer first boot initialization started' /var/log/metal-deployer/install-summary.log; then
        echo '   [Check] First-boot script executed: OK'
    else
        echo '   [Check] First-boot script not executed: FAIL'; exit 1
    fi
    if echo '$SSH_PASS' | sudo -S test -f /root/installed; then
        echo '   [Check] First-boot completion marker: OK'
    else
        echo '   [Check] First-boot completion marker missing: FAIL'; exit 1
    fi
    if echo '$SSH_PASS' | sudo -S grep -q 'Metal deployer first boot initialization finished' /var/log/metal-deployer/install-summary.log; then
        echo '   [Check] First-boot finished log: OK'
    else
        echo '   [Check] First-boot finished log missing: FAIL'; exit 1
    fi
    for cmd in docker containerd dockerd gcc make dkms ibv_devinfo ibstat nvidia-smi; do
        if command -v \"\$cmd\" >/dev/null 2>&1; then
            echo \"   [Check] Command \$cmd: OK\"
        else
            echo \"   [Check] Command \$cmd missing: FAIL\"; exit 1
        fi
    done
    if systemctl is-active --quiet docker; then
        echo '   [Check] docker service active: OK'
    else
        echo '   [Check] docker service inactive: FAIL'; exit 1
    fi
    if systemctl is-active --quiet containerd; then
        echo '   [Check] containerd service active: OK'
    else
        echo '   [Check] containerd service inactive: FAIL'; exit 1
    fi
    if lspci | grep -qiE 'nvidia.*(3d|vga|display)|tesla|h100|h200|a100|a800|l40|l4'; then
        if nvidia-smi -L >/dev/null 2>&1; then
            echo '   [Check] NVIDIA GPU usable by nvidia-smi: OK'
        else
            echo '   [Check] NVIDIA GPU present but nvidia-smi failed: FAIL'; exit 1
        fi
        if echo '$SSH_PASS' | sudo -S modprobe nvidia_peermem 2>/dev/null; then
            echo '   [Check] nvidia_peermem module load: OK'
        else
            echo '   [Check] nvidia_peermem module load: WARN (optional on dma-buf GPUDirect RDMA stacks)'
        fi
    else
        echo '   [Check] NVIDIA GPU absent: SKIP'
    fi
    if [ -d /opt/hpcx ] || [ -f /etc/profile.d/hpcx.sh ]; then
        # shellcheck disable=SC1091
        . /etc/profile.d/hpcx.sh >/dev/null 2>&1 || true
        if command -v hpcx_load >/dev/null 2>&1; then
            echo '   [Check] HPC-X environment: OK'
        else
            echo '   [Check] HPC-X installed but hpcx_load missing: FAIL'; exit 1
        fi
    else
        echo '   [Check] HPC-X absent: SKIP'
    fi
    "
    if ssh_vm "$TEST_CMD"; then
        echo "✅✅✅ Verification passed! ✅✅✅"
    else
        echo "❌❌❌ Verification failed ❌❌❌"
        export_failure_logs
        dump_failure_logs_if_enabled dump_first_boot_logs
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
