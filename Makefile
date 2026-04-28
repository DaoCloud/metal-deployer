# Metal Deployer Makefile
# 提供本地构建镜像等常用操作

# 变量定义
REGISTRY ?= ghcr.io
REPOSITORY ?= Daocloud/metal-deployer
SBCLI_IMAGE_NAME ?= $(REPOSITORY)/sbcli
SBCLI_IMAGE_TAG ?= latest
SBCLI_CONTEXT ?= ./superbenchmark/image
SBCLI_DOCKERFILE ?= $(SBCLI_CONTEXT)/Dockerfile

# 默认目标
.PHONY: help
help: ## 显示帮助信息
	@echo "Metal Deployer 构建工具"
	@echo ""
	@echo "可用目标:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "常用命令:"
	@echo "  make build-sbcli       # 本地构建 sbcli 镜像"
	@echo ""
	@echo "变量:"
	@echo "  REGISTRY=$(REGISTRY)"
	@echo "  SBCLI_IMAGE=$(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG)"

.PHONY: build-sbcli
build-sbcli: ## 本地构建 sbcli 镜像
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
