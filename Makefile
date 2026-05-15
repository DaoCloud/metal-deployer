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

# deepep-ucx 镜像变量
DEEPEP_UCX_IMAGE_NAME ?= $(REPOSITORY)/deepep-ucx
DEEPEP_UCX_IMAGE_TAG ?= latest
DEEPEP_UCX_CONTEXT ?= ./benchmarks/deepep-ucx/image
DEEPEP_UCX_DOCKERFILE ?= $(DEEPEP_UCX_CONTEXT)/Dockerfile

# deepep-ucx 构建参数（可覆盖）
ENV_BUILD_TOOLS_IMAGE_NAME ?= nvidia/cuda:12.8.1-devel-ubuntu22.04
ENV_BASEIMAGE_FULL_NAME ?= nvidia/cuda:12.8.1-runtime-ubuntu22.04
ENV_BASEIMAGE_CUDA_VERISON ?= 12.8.1
ENV_DEEPEP_VERSION ?= v1.2.1
ENV_DEEPGEMM_VERSION ?= v2.1.1.post3
ENV_UCX_VERSION ?= v1.20.0
ENV_NVSHMEM_VERSION ?= v3.4.5-0
ENV_BUILD_AND_DOWNLOAD_PARALLEL ?= 4
ENV_TORCH_CUDA_ARCH_LIST ?= "7.0;7.5;8.0;8.6;9.0"

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
	@echo "  make build-deepep-ucx         # 本地构建 deepep-ucx 镜像"
	@echo ""
	@echo "镜像变量:"
	@echo "  SBCLI_IMAGE=$(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG)"
	@echo "  DEEPEP_UCX_IMAGE=$(DEEPEP_UCX_IMAGE_NAME):$(DEEPEP_UCX_IMAGE_TAG)"
	@echo ""
	@echo "deepep-ucx 构建参数:"
	@echo "  ENV_BUILD_TOOLS_IMAGE_NAME=$(ENV_BUILD_TOOLS_IMAGE_NAME)"
	@echo "  ENV_DEEPEP_VERSION=$(ENV_DEEPEP_VERSION)"
	@echo "  ENV_UCX_VERSION=$(ENV_UCX_VERSION)"
	@echo "  ENV_TORCH_CUDA_ARCH_LIST=$(ENV_TORCH_CUDA_ARCH_LIST)"

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

.PHONY: build-deepep-ucx
build-deepep-ucx: ## 本地构建 deepep-ucx 镜像
	@echo "Building deepep-ucx image..."
	@echo "Image: $(DEEPEP_UCX_IMAGE_NAME):$(DEEPEP_UCX_IMAGE_TAG)"
	@echo "Context: $(DEEPEP_UCX_CONTEXT)"
	@if [ ! -f $(DEEPEP_UCX_DOCKERFILE) ]; then \
		echo "Error: Dockerfile not found at $(DEEPEP_UCX_DOCKERFILE)"; \
		exit 1; \
	fi
	docker build \
		--build-arg ENV_BUILD_TOOLS_IMAGE_NAME=$(ENV_BUILD_TOOLS_IMAGE_NAME) \
		--build-arg ENV_BASEIMAGE_FULL_NAME=$(ENV_BASEIMAGE_FULL_NAME) \
		--build-arg ENV_BASEIMAGE_CUDA_VERISON=$(ENV_BASEIMAGE_CUDA_VERISON) \
		--build-arg ENV_DEEPEP_VERSION=$(ENV_DEEPEP_VERSION) \
		--build-arg ENV_DEEPGEMM_VERSION=$(ENV_DEEPGEMM_VERSION) \
		--build-arg ENV_UCX_VERSION=$(ENV_UCX_VERSION) \
		--build-arg ENV_NVSHMEM_VERSION=$(ENV_NVSHMEM_VERSION) \
		--build-arg ENV_BUILD_AND_DOWNLOAD_PARALLEL=$(ENV_BUILD_AND_DOWNLOAD_PARALLEL) \
		--build-arg ENV_TORCH_CUDA_ARCH_LIST=$(ENV_TORCH_CUDA_ARCH_LIST) \
		-t $(DEEPEP_UCX_IMAGE_NAME):$(DEEPEP_UCX_IMAGE_TAG) \
		-f $(DEEPEP_UCX_DOCKERFILE) \
		$(DEEPEP_UCX_CONTEXT)
	@echo "Build complete: $(DEEPEP_UCX_IMAGE_NAME):$(DEEPEP_UCX_IMAGE_TAG)"
