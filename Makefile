# Metal Deployer Makefile
# Provides common operations for local image building

# Variable definitions
REGISTRY ?= ghcr.io
REPOSITORY ?= daocloud/metal-deployer

# sbcli 镜像变量
SBCLI_IMAGE_NAME ?= $(REPOSITORY)/sbcli
SBCLI_IMAGE_TAG ?= latest
SBCLI_CONTEXT ?= ./benchmarks/superbenchmark/image
SBCLI_DOCKERFILE ?= $(SBCLI_CONTEXT)/Dockerfile
ISO_DIR ?= ./build-iso
CUDA_PROFILE ?= cuda13
ISO_MANIFEST ?= $(ISO_DIR)/manifest.yaml
CACHE_BASE_DIR ?= $(ISO_DIR)/.ci-work/cache

# gpu-rdma-bench 镜像变量
GPU_RDMA_BENCH_IMAGE_NAME ?= $(REPOSITORY)/gpu-rdma-bench
GPU_RDMA_BENCH_IMAGE_TAG ?= latest
GPU_RDMA_BENCH_CONTEXT ?= ./benchmarks/gpu-rdma-bench/image
GPU_RDMA_BENCH_DOCKERFILE ?= $(GPU_RDMA_BENCH_CONTEXT)/Dockerfile

# gpu-rdma-bench 构建参数（可覆盖）
ENV_BUILD_TOOLS_IMAGE_NAME ?= nvidia/cuda:12.8.1-devel-ubuntu22.04
ENV_BASEIMAGE_FULL_NAME ?= nvidia/cuda:12.8.1-runtime-ubuntu22.04
ENV_BASEIMAGE_CUDA_VERISON ?= 12.8.1
ENV_DEEPEP_VERSION ?= v1.2.1
ENV_DEEPGEMM_VERSION ?= v2.1.1.post3
ENV_UCX_VERSION ?= v1.20.0
ENV_NVSHMEM_VERSION ?= v3.4.5-0
ENV_BUILD_AND_DOWNLOAD_PARALLEL ?= 4
ENV_TORCH_CUDA_ARCH_LIST ?= 9.0
ENV_NIXL_REPO ?= https://github.com/ai-dynamo/nixl.git
ENV_NIXL_REF ?= main
ENV_LIBFABRIC_VERSION ?= v1.21.0
ENV_GDRCOPY_VERSION ?= v2.5.1
ENV_ABSL_TAG ?= lts_2025_08_14
ENV_GRPC_TAG ?= v1.73.0

# 默认目标
.PHONY: help
help: ## Show help information
	@echo "Metal Deployer build tool"
	@echo ""
	@echo "可用目标:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-25s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "常用命令:"
	@echo "  make build-sbcli              # 本地构建 sbcli 镜像"
	@echo "  make build-gpu-rdma-bench     # 本地构建 GPU/RDMA benchmark 镜像"
	@echo ""
	@echo "镜像变量:"
	@echo "  SBCLI_IMAGE=$(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG)"
	@echo "  GPU_RDMA_BENCH_IMAGE=$(GPU_RDMA_BENCH_IMAGE_NAME):$(GPU_RDMA_BENCH_IMAGE_TAG)"
	@echo ""
	@echo "gpu-rdma-bench 构建参数:"
	@echo "  ENV_BUILD_TOOLS_IMAGE_NAME=$(ENV_BUILD_TOOLS_IMAGE_NAME)"
	@echo "  ENV_DEEPEP_VERSION=$(ENV_DEEPEP_VERSION)"
	@echo "  ENV_UCX_VERSION=$(ENV_UCX_VERSION)"
	@echo "  ENV_TORCH_CUDA_ARCH_LIST=$(ENV_TORCH_CUDA_ARCH_LIST)"
	@echo "  ENV_NIXL_REF=$(ENV_NIXL_REF)"
	@echo "  ENV_LIBFABRIC_VERSION=$(ENV_LIBFABRIC_VERSION)"
	@echo "  ENV_GDRCOPY_VERSION=$(ENV_GDRCOPY_VERSION)"
	@echo "  ENV_GRPC_TAG=$(ENV_GRPC_TAG)"

.PHONY: build-sbcli
build-sbcli: ## Build sbcli image locally
	@echo "Building sbcli image..."
	@echo "Image: $(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG)"
	@echo "Context: $(SBCLI_CONTEXT)"
	@if [ ! -f $(SBCLI_DOCKERFILE) ]; then \
		echo "Error: Dockerfile not found at $(SBCLI_DOCKERFILE)"; \
		exit 1; \
	fi
	docker build \
		-t $(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG) \
		-f $(SBCLI_DOCKERFILE) \
		$(SBCLI_CONTEXT)
	@echo "Build complete: $(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG)"

.PHONY: build-gpu-rdma-bench
build-gpu-rdma-bench: ## 本地构建 GPU/RDMA benchmark 镜像
	@echo "Building gpu-rdma-bench image..."
	@echo "Image: $(GPU_RDMA_BENCH_IMAGE_NAME):$(GPU_RDMA_BENCH_IMAGE_TAG)"
	@echo "Context: $(GPU_RDMA_BENCH_CONTEXT)"
	@if [ ! -f $(GPU_RDMA_BENCH_DOCKERFILE) ]; then \
		echo "Error: Dockerfile not found at $(GPU_RDMA_BENCH_DOCKERFILE)"; \
		exit 1; \
	fi
	docker build \
		--build-arg "ENV_BUILD_TOOLS_IMAGE_NAME=$(ENV_BUILD_TOOLS_IMAGE_NAME)" \
		--build-arg "ENV_BASEIMAGE_FULL_NAME=$(ENV_BASEIMAGE_FULL_NAME)" \
		--build-arg "ENV_BASEIMAGE_CUDA_VERISON=$(ENV_BASEIMAGE_CUDA_VERISON)" \
		--build-arg "ENV_DEEPEP_VERSION=$(ENV_DEEPEP_VERSION)" \
		--build-arg "ENV_DEEPGEMM_VERSION=$(ENV_DEEPGEMM_VERSION)" \
		--build-arg "ENV_UCX_VERSION=$(ENV_UCX_VERSION)" \
		--build-arg "ENV_NVSHMEM_VERSION=$(ENV_NVSHMEM_VERSION)" \
		--build-arg "ENV_BUILD_AND_DOWNLOAD_PARALLEL=$(ENV_BUILD_AND_DOWNLOAD_PARALLEL)" \
		--build-arg "ENV_TORCH_CUDA_ARCH_LIST=$(ENV_TORCH_CUDA_ARCH_LIST)" \
		--build-arg "ENV_NIXL_REPO=$(ENV_NIXL_REPO)" \
		--build-arg "ENV_NIXL_REF=$(ENV_NIXL_REF)" \
		--build-arg "ENV_LIBFABRIC_VERSION=$(ENV_LIBFABRIC_VERSION)" \
		--build-arg "ENV_GDRCOPY_VERSION=$(ENV_GDRCOPY_VERSION)" \
		--build-arg "ENV_ABSL_TAG=$(ENV_ABSL_TAG)" \
		--build-arg "ENV_GRPC_TAG=$(ENV_GRPC_TAG)" \
		-t $(GPU_RDMA_BENCH_IMAGE_NAME):$(GPU_RDMA_BENCH_IMAGE_TAG) \
		-f $(GPU_RDMA_BENCH_DOCKERFILE) \
		$(GPU_RDMA_BENCH_CONTEXT)
	@echo "Build complete: $(GPU_RDMA_BENCH_IMAGE_NAME):$(GPU_RDMA_BENCH_IMAGE_TAG)"
