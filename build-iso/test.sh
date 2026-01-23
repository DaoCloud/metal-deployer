#!/bin/bash

# ==============================================================================
# QEMU ISO 自动化测试工具 (终极修复版)
# 功能: 智能加速 setup / 强力清理 clean / 登录 login / 拉取文件 pull
# ==============================================================================

set -o pipefail

# --- 全局配置 ---
BASE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

ISO_FILE="${BASE_DIR}/custom-ubuntu.iso"
DISK_IMG="${BASE_DIR}/test_disk.qcow2"
PID_FILE="${BASE_DIR}/qemu.pid"
LOG_FILE="${BASE_DIR}/qemu.log"
SSH_PORT=5555
SSH_USER="admin"
SSH_PASS="admin" # 注意：请确保这里与 user-data 中的密码一致

# 基础超时时间 (秒)
TIMEOUT=2400

# --- 检查依赖 ---
check_deps() {
    local tools="qemu-system-x86_64 sshpass qemu-img scp"
    for tool in $tools; do
        if ! command -v $tool &> /dev/null; then
            echo "❌ 错误: 未找到命令 $tool，请先安装。"
            exit 1
        fi
    done
}

# --- 辅助函数: 检查 VM 是否运行 ---
check_vm_running() {
    if [ ! -f "$PID_FILE" ] || ! kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "❌ 错误: 虚拟机未运行。请先运行 setup。"
        exit 1
    fi
}

# --- 子命令: CLEAN (清理) ---
do_clean() {
    echo "🧹 [Clean] 开始清理环境..."
    
    # 1. 尝试通过 PID 文件杀进程
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "   -> [PID] 停止 QEMU 进程 (PID: $PID)..."
            kill -9 "$PID" 2>/dev/null
        fi
        rm -f "$PID_FILE"
    fi

    # 2. [新增] 双重保险：通过磁盘文件名特征查杀
    # 防止 PID 文件记录错误(例如记录了 tee 的 PID)，导致 QEMU 没死掉
    # pgrep -f 会匹配完整的命令行参数，查找使用了当前磁盘镜像的进程
    if pgrep -f "$DISK_IMG" >/dev/null; then
        echo "   -> [Force] 检测到残留 QEMU 进程，正在强制清除..."
        pkill -f "$DISK_IMG"
    fi

    # 3. 删除磁盘文件
    if [ -f "$DISK_IMG" ]; then
        echo "   -> 删除虚拟磁盘 $DISK_IMG"
        rm -f "$DISK_IMG"
    fi
    
    echo "✅ 环境清理完毕。"
}

# --- 子命令: SETUP (安装) ---
do_setup() {
    check_deps
    
    # 检查是否已有残留进程
    if pgrep -f "$DISK_IMG" >/dev/null; then
        echo "⚠️  警告: 检测到 QEMU 已经在运行中。"
        echo "   请先运行 '$0 clean' 进行清理。"
        exit 1
    fi

    echo "🚀 [Setup] 开始测试: $ISO_FILE"

    # KVM 检测逻辑
    QEMU_ACCEL_ARGS=""
    SMP_CORES="2"
    if [ -e /dev/kvm ] && [ -w /dev/kvm ]; then
        echo "⚡ [KVM] 检测到硬件加速，启用 KVM 模式。"
        QEMU_ACCEL_ARGS="-enable-kvm -cpu host"
        SMP_CORES="4"
        TIMEOUT=1200
    else
        echo "🐢 [KVM] 未检测到可用 KVM，使用软件模拟模式 (较慢)。"
    fi

    echo "💿 创建磁盘..."
    rm -f "$DISK_IMG"
    qemu-img create -f qcow2 "$DISK_IMG" 10G > /dev/null

    echo "🔥 启动虚拟机 (日志: $LOG_FILE)..."
    
    # [关键修复] 使用 > >(tee ...) 语法
    # 这样 qemu 是主进程，$! 获取的就是 qemu 的 PID，而不是 tee 的 PID
    qemu-system-x86_64 \
      $QEMU_ACCEL_ARGS \
      -m 4G \
      -smp "$SMP_CORES" \
      -hda "$DISK_IMG" \
      -cdrom "$ISO_FILE" \
      -boot once=d \
      -nographic \
      -serial mon:stdio \
      -net nic -net user,hostfwd=tcp::${SSH_PORT}-:22 \
      > >(tee "$LOG_FILE") 2>&1 &
    
    QEMU_PID=$!
    echo "$QEMU_PID" > "$PID_FILE"
    echo "   -> QEMU PID: $QEMU_PID"

    echo "⏳ 后台安装中，等待 SSH..."
    START_TIME=$(date +%s)
    
    while true; do
        CURRENT_TIME=$(date +%s)
        ELAPSED=$((CURRENT_TIME - START_TIME))
        
        if [ "$ELAPSED" -gt "$TIMEOUT" ]; then echo "❌ [Timeout] 安装超时！"; exit 1; fi
        
        # 进程存活检查
        if ! kill -0 $QEMU_PID 2>/dev/null; then 
            echo "❌ [Error] QEMU 进程意外退出！请检查日志。"
            exit 1
        fi

        if sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
           -o ConnectTimeout=2 -p $SSH_PORT $SSH_USER@localhost "echo 'Ready'" >/dev/null 2>&1; then
            echo ""
            echo "✅ SSH 连接成功！系统已就绪。"
            break
        fi
        sleep 10
    done

    echo "🔍 执行自动化验证..."
    # 这里增加了更详细的验证逻辑
    TEST_CMD="
    if [ -d /opt/resource/scripts ]; then
        echo '   [Check] 脚本目录存在: OK'
    else
        echo '   [Check] 脚本目录丢失: FAIL'; exit 1
    fi
    "
    sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -p $SSH_PORT $SSH_USER@localhost "$TEST_CMD"
    
    if [ $? -eq 0 ]; then
        echo "✅✅✅ 验证通过！ ✅✅✅"
    else
        echo "❌❌❌ 验证失败 ❌❌❌"
        exit 1
    fi
}

# --- 子命令: LOGIN (登录) ---
do_login() {
    check_vm_running
    echo "🔑 正在登录..."
    sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -p $SSH_PORT $SSH_USER@localhost
}

# --- 子命令: PULL (从 VM 复制文件) ---
do_pull() {
    check_vm_running
    REMOTE_PATH="$1"
    
    if [ -z "$REMOTE_PATH" ]; then
        echo "❌ 用法错误: 请指定要拉取的远程路径。"
        echo "   示例: $0 pull /var/log/custom-setup.log"
        exit 1
    fi

    echo "📥 正在从虚拟机拉取: $REMOTE_PATH"
    echo "   -> 目标: $(pwd)/"
    
    # 使用 scp -r (递归) -P (指定端口)
    sshpass -p "$SSH_PASS" scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -P $SSH_PORT -r "$SSH_USER@localhost:$REMOTE_PATH" .
    
    if [ $? -eq 0 ]; then
        echo "✅ 拉取成功！"
        LOCAL_NAME=$(basename "$REMOTE_PATH")
        ls -ld "$LOCAL_NAME"
    else
        echo "❌ 拉取失败 (请检查路径是否存在，或是否需要 root 权限)"
    fi
}

# --- 主逻辑 ---
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
        echo "用法: $0 {setup|clean|login|pull}"
        echo "  setup           : 创建并启动测试"
        echo "  login           : SSH 登录虚拟机"
        echo "  pull <VM_Path>  : 从虚拟机复制文件到当前目录"
        echo "  clean           : 清理环境"
        exit 1
        ;;
esac
