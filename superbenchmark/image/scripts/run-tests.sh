#!/bin/bash
set -e

# 一键执行 SuperBench 测试脚本
# 用法: ./run-tests.sh [options]
#   options:
#     -h, --hosts FILE        hosts 文件路径 (默认: /workspace/hosts)
#     -o, --output DIR        输出目录 (默认: /workspace/outputs)
#     -t, --test-case CASE    测试类型: single-node|multi-node|all (默认: single-node,multi-node)
#     --help                  显示帮助信息
#
# 示例:
#   ./run-tests.sh                                          # 执行默认测试 (single-node + multi-node)
#   ./run-tests.sh -t single-node                           # 仅执行单机测试
#   ./run-tests.sh -t multi-node                            # 仅执行多机测试
#   ./run-tests.sh -t all                                   # 执行全部测试 (single-node + multi-node + all)
#   ./run-tests.sh -h /workspace/hosts -o /workspace/out    # 指定 hosts 和输出目录

# 默认值
HOSTS_FILE="/workspace/hosts"
OUTPUT_BASE="/workspace/outputs"
TEST_CASE=""  # 空表示默认执行 single-node 和 multi-node
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULT_DIR="${OUTPUT_BASE}/${TIMESTAMP}"

# 镜像配置
SBCLI_IMAGE="${SBCLI_IMAGE:-docker.m.daocloud.io/superbench/superbench:v0.12.0-cuda12.9}"

# 配置文件路径（镜像内置）
CONFIG_SINGLE="/workspace/config_single_node.yaml"
CONFIG_MULTI="/workspace/config_multi_node.yaml"
CONFIG_ALL="/workspace/config_all.yaml"
RULE_ALL="/workspace/rule_all.yaml"

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
        -t|--test-case)
            TEST_CASE="$2"
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
            echo "  -t, --test-case CASE    测试类型: single-node|multi-node|all"
            echo "                          默认: single-node,multi-node (执行单机和多机测试)"
            echo "  --help                  显示此帮助信息"
            echo ""
            echo "示例:"
            echo "  ./run-tests.sh                          # 执行默认测试 (single-node + multi-node)"
            echo "  ./run-tests.sh -t single-node             # 仅执行单机测试"
            echo "  ./run-tests.sh -t multi-node              # 仅执行多机测试"
            echo "  ./run-tests.sh -t all                     # 执行全部测试 (包含模型测试，约 8 小时)"
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
        all)
            TESTS_TO_RUN=("single-node" "multi-node" "all")
            ;;
        *)
            echo "错误: 未知的测试类型 '$TEST_CASE'"
            echo "支持的类型: single-node, multi-node, all"
            exit 1
            ;;
    esac
fi

echo "========================================"
echo "SuperBench 一键测试脚本"
echo "========================================"
echo ""
echo "配置信息:"
echo "  - Hosts 文件: ${HOSTS_FILE}"
echo "  - 输出目录: ${RESULT_DIR}"
echo "  - 测试类型: ${TESTS_TO_RUN[*]}"
echo "  - 被测节点镜像: ${SBCLI_IMAGE}"
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
    case "$test_type" in
        single-node)
            if [ -f "${RESULT_DIR}/single-node/results-summary.jsonl" ]; then
                echo "合并单机测试结果..."
                cat "${RESULT_DIR}/single-node/results-summary.jsonl" >> "${MERGED_DATA}"
            fi
            ;;
        multi-node)
            if [ -f "${RESULT_DIR}/multi-node/results-summary.jsonl" ]; then
                echo "合并多机测试结果..."
                cat "${RESULT_DIR}/multi-node/results-summary.jsonl" >> "${MERGED_DATA}"
            fi
            ;;
        all)
            if [ -f "${RESULT_DIR}/all/results-summary.jsonl" ]; then
                echo "合并全量测试结果..."
                cat "${RESULT_DIR}/all/results-summary.jsonl" >> "${MERGED_DATA}"
            fi
            ;;
    esac
done

# 生成统一汇总报告
if [ -s "${MERGED_DATA}" ]; then
    echo "生成统一汇总报告 (Excel)..."
    sb result summary \
        --data-file "${MERGED_DATA}" \
        --rule-file "${RULE_ALL}" \
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
