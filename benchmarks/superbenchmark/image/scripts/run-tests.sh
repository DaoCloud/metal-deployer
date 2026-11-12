#!/bin/bash
set -e

# 一键执行 SuperBench 测试脚本
# 用法: ./run-tests.sh [options]
#   options:
#     -h, --hosts FILE        hosts 文件路径 (默认: /workspace/hosts)
#     -o, --output DIR        输出目录 (默认: /workspace/outputs)
#     -p, --profile PROFILE   配置组: default|gb300 (默认: sbcli 运行在 arm64 上为 gb300，否则 default)
#     -t, --test-case CASE    测试类型: single-node|multi-node|all，gb300 另有 nvl-domain (默认: single-node,multi-node)
#     -i, --worker-image IMG  被测节点 SuperBench 镜像 (默认按 profile 选择，见下方 WORKER_IMAGE)
#     --disk DEVS             (仅 gb300) 磁盘测试使用的块设备，逗号分隔；会覆盖写入，未指定则不跑磁盘测试
#     --help                  显示帮助信息
#
# 示例:
#   ./run-tests.sh                                          # 执行默认测试 (single-node + multi-node)
#   ./run-tests.sh -t single-node                           # 仅执行单机测试
#   ./run-tests.sh -t multi-node                            # 仅执行多机测试
#   ./run-tests.sh -t all                                   # 执行全部测试 (single-node + multi-node + all)
#   ./run-tests.sh -p gb300 -t nvl-domain                   # GB300 柜内 MNNVL 测试 (hosts 须同一 NVLink 域)
#   ./run-tests.sh -p gb300 --disk /dev/nvme1n1             # GB300 默认测试 + 指定测试盘的磁盘测试
#   ./run-tests.sh -h /workspace/hosts -o /workspace/out    # 指定 hosts 和输出目录

# 默认值
HOSTS_FILE="/workspace/hosts"
OUTPUT_BASE="/workspace/outputs"
TEST_CASE=""  # 空表示默认执行 single-node 和 multi-node
PROFILE=""    # 空表示按 sbcli 所在机器架构选择
DISK_DEVICES=""
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULT_DIR="${OUTPUT_BASE}/${TIMESTAMP}"

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--hosts)
            HOSTS_FILE="$2"
            shift 2
            ;;
        -o|--output)
            OUTPUT_BASE="$2"
            RESULT_DIR="${OUTPUT_BASE}/${TIMESTAMP}"
            shift 2
            ;;
        -p|--profile)
            PROFILE="$2"
            shift 2
            ;;
        -t|--test-case)
            TEST_CASE="$2"
            shift 2
            ;;
        -i|--worker-image)
            WORKER_IMAGE="$2"
            shift 2
            ;;
        --disk)
            DISK_DEVICES="$2"
            shift 2
            ;;
        --help)
            echo "一键执行 SuperBench 测试脚本"
            echo ""
            echo "用法: ./run-tests.sh [options]"
            echo ""
            echo "选项:"
            echo "  -h, --hosts FILE        hosts 文件路径 (默认: /workspace/hosts)"
            echo "  -o, --output DIR        输出目录 (默认: /workspace/outputs)"
            echo "  -p, --profile PROFILE   配置组: default|gb300"
            echo "                          默认: sbcli 运行在 arm64 上为 gb300，否则 default"
            echo "  -t, --test-case CASE    测试类型: single-node|multi-node|all"
            echo "                          gb300 另支持 nvl-domain (柜内 MNNVL，hosts 须同一 NVLink 域)"
            echo "                          默认: single-node,multi-node (执行单机和多机测试)"
            echo "  -i, --worker-image IMG  被测节点镜像"
            echo "                          默认: default 为 docker.m.daocloud.io/superbench/superbench:v0.12.0-cuda12.9，"
            echo "                                gb300 为 docker.io/superbench/main:cuda13.0 (GB300 ISO 预置)"
            echo "  --disk DEVS             (仅 gb300) 磁盘测试块设备，逗号分隔，如 /dev/nvme1n1"
            echo "                          警告: 会覆盖写入设备数据，只能指定已确认可清空的测试盘"
            echo "  --help                  显示此帮助信息"
            echo ""
            echo "示例:"
            echo "  ./run-tests.sh                          # 执行默认测试 (single-node + multi-node)"
            echo "  ./run-tests.sh -t single-node             # 仅执行单机测试"
            echo "  ./run-tests.sh -t multi-node              # 仅执行多机测试"
            echo "  ./run-tests.sh -t all                     # 执行全部测试 (default 包含模型测试，约 8 小时)"
            echo "  ./run-tests.sh -p gb300 -t nvl-domain     # GB300 柜内 MNNVL 集合通信测试"
            echo "  ./run-tests.sh -p gb300 --disk /dev/nvme1n1  # GB300 默认测试 + 磁盘测试"
            echo "  ./run-tests.sh -h /path/to/hosts -o /out  # 指定 hosts 和输出目录"
            exit 0
            ;;
        *)
            echo "未知参数: $1"
            echo "使用 --help 查看帮助信息"
            exit 1
            ;;
    esac
done

if [ -z "$PROFILE" ]; then
    case "$(uname -m)" in
        aarch64|arm64) PROFILE="gb300" ;;
        *)             PROFILE="default" ;;
    esac
fi

# 被测节点（worker）镜像，需已预先导入节点（sb deploy 使用 --no-image-pull）。
# 优先级：-i/--worker-image > WORKER_IMAGE > SBCLI_IMAGE（兼容旧变量）> 按 profile 默认值。
# gb300 默认用 GB300 ISO 预置的 SuperBench main 镜像（v0.12.0 不支持 sm_103）。ISO 构建时按 digest
# 拉取后以该 tag 保存/导入，docker load 不保留 digest，因此这里只能按 tag 引用。
case "$PROFILE" in
    default)
        DEFAULT_WORKER_IMAGE="docker.m.daocloud.io/superbench/superbench:v0.12.0-cuda12.9"
        CONFIG_SINGLE="/workspace/config_single_node.yaml"
        CONFIG_MULTI="/workspace/config_multi_node.yaml"
        CONFIG_ALL="/workspace/config_all.yaml"
        RULE_FILE="/workspace/rule_all.yaml"
        ;;
    gb300)
        DEFAULT_WORKER_IMAGE="docker.io/superbench/main:cuda13.0"
        CONFIG_SINGLE="/workspace/config_gb300_single_node.yaml"
        CONFIG_MULTI="/workspace/config_gb300_multi_node.yaml"
        CONFIG_NVL_DOMAIN="/workspace/config_gb300_nvl_domain.yaml"
        CONFIG_DISK="/workspace/config_gb300_disk.yaml"
        RULE_FILE="/workspace/rule_gb300.yaml"
        ;;
    *)
        echo "错误: 未知的 profile '$PROFILE'"
        echo "支持的 profile: default, gb300"
        exit 1
        ;;
esac
WORKER_IMAGE="${WORKER_IMAGE:-${SBCLI_IMAGE:-${DEFAULT_WORKER_IMAGE}}}"

if [ -n "$DISK_DEVICES" ] && [ "$PROFILE" != "gb300" ]; then
    echo "错误: --disk 仅支持 gb300 profile；default profile 的磁盘测试设备见 config_single_node.yaml"
    exit 1
fi

# 确定要执行的测试
declare -a TESTS_TO_RUN
if [ -z "$TEST_CASE" ]; then
    # 默认执行 single-node 和 multi-node
    TESTS_TO_RUN=("single-node" "multi-node")
else
    case "$TEST_CASE" in
        single-node)
            TESTS_TO_RUN=("single-node")
            ;;
        multi-node)
            TESTS_TO_RUN=("multi-node")
            ;;
        nvl-domain)
            if [ "$PROFILE" != "gb300" ]; then
                echo "错误: nvl-domain 仅支持 gb300 profile"
                exit 1
            fi
            TESTS_TO_RUN=("nvl-domain")
            ;;
        all)
            if [ "$PROFILE" = "gb300" ]; then
                TESTS_TO_RUN=("single-node" "multi-node" "nvl-domain")
            else
                TESTS_TO_RUN=("single-node" "multi-node" "all")
            fi
            ;;
        *)
            echo "错误: 未知的测试类型 '$TEST_CASE'"
            echo "支持的类型: single-node, multi-node, all$([ "$PROFILE" = "gb300" ] && echo ", nvl-domain")"
            exit 1
            ;;
    esac
fi
if [ -n "$DISK_DEVICES" ]; then
    TESTS_TO_RUN+=("disk")
fi

echo "========================================"
echo "SuperBench 一键测试脚本"
echo "========================================"
echo ""
echo "配置信息:"
echo "  - Profile: ${PROFILE}"
echo "  - Hosts 文件: ${HOSTS_FILE}"
echo "  - 输出目录: ${RESULT_DIR}"
echo "  - 测试类型: ${TESTS_TO_RUN[*]}"
echo "  - 被测节点镜像: ${WORKER_IMAGE}"
if [ -n "$DISK_DEVICES" ]; then
    echo "  - 磁盘测试设备 (将被覆盖写入): ${DISK_DEVICES}"
fi
echo ""

# 检查 hosts 文件
if [ ! -f "${HOSTS_FILE}" ]; then
    echo "错误: Hosts 文件不存在: ${HOSTS_FILE}"
    echo "请准备 hosts 文件并挂载到 /workspace/hosts"
    exit 1
fi

# 创建输出目录
mkdir -p "${RESULT_DIR}"

# 部署到所有节点（如果需要运行测试）
if [ ${#TESTS_TO_RUN[@]} -gt 0 ]; then
    echo "========================================"
    echo "步骤 1: 部署 SuperBench 到被测节点..."
    echo "========================================"
    sb deploy -f "${HOSTS_FILE}" --no-image-pull -i "${WORKER_IMAGE}" -o "${RESULT_DIR}/deploy"
    echo "部署完成"
    echo ""
fi

# 执行测试
for test_type in "${TESTS_TO_RUN[@]}"; do
    case "$test_type" in
        single-node)
            echo "========================================"
            echo "执行单机测试 (约 2 小时)..."
            echo "========================================"
            sb run -f "${HOSTS_FILE}" \
                --config-file "${CONFIG_SINGLE}" \
                --output-dir "${RESULT_DIR}/single-node" \
                --get-info
            echo "单机测试完成"
            echo ""
            ;;
        multi-node)
            echo "========================================"
            echo "执行多机测试 (约 2 小时)..."
            echo "========================================"
            sb run -f "${HOSTS_FILE}" \
                --config-file "${CONFIG_MULTI}" \
                --output-dir "${RESULT_DIR}/multi-node" \
                --get-info
            echo "多机测试完成"
            echo ""
            ;;
        nvl-domain)
            echo "========================================"
            echo "执行 NVLink 域 (MNNVL) 测试..."
            echo "========================================"
            sb run -f "${HOSTS_FILE}" \
                --config-file "${CONFIG_NVL_DOMAIN}" \
                --output-dir "${RESULT_DIR}/nvl-domain" \
                --get-info
            echo "NVLink 域测试完成"
            echo ""
            ;;
        disk)
            echo "========================================"
            echo "执行磁盘测试 (覆盖写入 ${DISK_DEVICES})..."
            echo "========================================"
            sb run -f "${HOSTS_FILE}" \
                --config-file "${CONFIG_DISK}" \
                --config-override "superbench.benchmarks.disk-benchmark.parameters.block_devices=[${DISK_DEVICES}]" \
                --output-dir "${RESULT_DIR}/disk" \
                --get-info
            echo "磁盘测试完成"
            echo ""
            ;;
        all)
            echo "========================================"
            echo "执行全量测试 (约 8 小时)..."
            echo "========================================"
            sb run -f "${HOSTS_FILE}" \
                --config-file "${CONFIG_ALL}" \
                --output-dir "${RESULT_DIR}/all" \
                --get-info
            echo "全量测试完成"
            echo ""
            ;;
    esac
done

# 生成汇总报告
echo "========================================"
echo "生成汇总报告..."
echo "========================================"

REPORT_DIR="${RESULT_DIR}/reports"
mkdir -p "${REPORT_DIR}"

# 合并结果文件用于统一报告
MERGED_DATA="${RESULT_DIR}/merged-results.jsonl"
: > "${MERGED_DATA}"  # 清空或创建文件

# 收集所有测试结果到合并文件
for test_type in "${TESTS_TO_RUN[@]}"; do
    if [ -f "${RESULT_DIR}/${test_type}/results-summary.jsonl" ]; then
        echo "合并 ${test_type} 测试结果..."
        cat "${RESULT_DIR}/${test_type}/results-summary.jsonl" >> "${MERGED_DATA}"
    fi
done

# 生成统一汇总报告
if [ -s "${MERGED_DATA}" ]; then
    echo "生成统一汇总报告 (Excel)..."
    sb result summary \
        --data-file "${MERGED_DATA}" \
        --rule-file "${RULE_FILE}" \
        --output-file-format excel \
        --output-dir "${REPORT_DIR}"
    mv "${REPORT_DIR}/results-summary.xlsx" "${REPORT_DIR}/superbench-test-summary.xlsx"
    echo "统一汇总报告: ${REPORT_DIR}/superbench-test-summary.xlsx"
fi

echo ""
echo "========================================"
echo "测试完成！"
echo "========================================"
echo ""
echo "结果输出路径:"
echo "  - 原始数据: ${RESULT_DIR}"
echo "  - 汇总报告: ${REPORT_DIR}/superbench-test-summary.xlsx"
echo ""
echo "如果挂载了宿主机目录，可在宿主机查看:"
echo "  结果路径: <挂载路径>/${TIMESTAMP}/"
echo "  汇总报告: <挂载路径>/${TIMESTAMP}/reports/superbench-test-summary.xlsx"
echo ""
