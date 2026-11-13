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
# 目标平台，留空为本机架构；GB300 用 linux/arm64（非 arm64 主机需 QEMU binfmt）
SBCLI_PLATFORM ?=
# 基础镜像，Docker Hub 不通时可改为 docker.m.daocloud.io/library/python:3.11-slim
SBCLI_BASE_IMAGE ?= python:3.11-slim
ISO_DIR ?= ./build-iso
CUDA_PROFILE ?= cuda13
ISO_MANIFEST ?= $(ISO_DIR)/manifests/manifest.yaml
CACHE_BASE_DIR ?= $(ISO_DIR)/.ci-work/cache

# moe-bench 镜像变量（GB300/B300：HybridEP + DeepEP V1 + MoonEP + DeepGEMM，仅 linux/arm64）
MOE_BENCH_IMAGE_NAME ?= $(REPOSITORY)/moe-bench
MOE_BENCH_IMAGE_TAG ?= latest
MOE_BENCH_CONTEXT ?= ./benchmarks/moe-bench/image
MOE_BENCH_DOCKERFILE ?= $(MOE_BENCH_CONTEXT)/Dockerfile
MOE_BENCH_PLATFORM ?= linux/arm64
MOE_BENCH_MAX_JOBS ?= 16

# nixl-benchmark 镜像变量（NIXL + NIXLBench）。一个 Dockerfile，场景差异在 profiles/<NIXL_PROFILE>.env；
# 与 Dockerfile ARG 同名的环境变量 / make 变量覆盖 profile，如 make build-nixl-benchmark NIXL_REF=v1.5.1
NIXL_PROFILE ?= cuda13-gb300
NIXL_BENCHMARK_IMAGE_NAME ?= $(REPOSITORY)/nixl-benchmark
NIXL_BENCHMARK_IMAGE_TAG ?= latest-$(NIXL_PROFILE)
NIXL_BENCHMARK_CONTEXT ?= ./benchmarks/nixl-benchmark/image
NIXL_BENCHMARK_PROFILE_SH = $(NIXL_BENCHMARK_CONTEXT)/profile.sh

# 默认目标
.PHONY: help
help: ## Show help information
	@echo "Metal Deployer build tool"
	@echo ""
	@echo "可用目标:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-25s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "常用命令:"
	@echo "  make build-sbcli              # 本地构建 sbcli 镜像"
	@echo "  make build-sbcli-arm64        # 本地构建 arm64 sbcli 镜像（GB300）"
	@echo "  make build-moe-bench          # 本地构建 GB300 MoE 测试镜像（HybridEP、DeepEP V1、MoonEP、DeepGEMM，arm64）"
	@echo "  make build-nixl-benchmark     # 本地构建 NIXL/NIXLBench 镜像（NIXL_PROFILE=$(NIXL_PROFILE)，可选: $$($(NIXL_BENCHMARK_PROFILE_SH) list | tr '\n' ' ')）"
	@echo "  make prepare-iso / sudo make build-iso / make test-iso   # ISO（GB300: ISO_MANIFEST=build-iso/manifests/manifest-gb300.yaml CUDA_PROFILE=cuda13-gb300）"
	@echo ""
	@echo "镜像变量:"
	@echo "  SBCLI_IMAGE=$(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG)"
	@echo "  NIXL_BENCHMARK_IMAGE=$(NIXL_BENCHMARK_IMAGE_NAME):$(NIXL_BENCHMARK_IMAGE_TAG)"

.PHONY: build-sbcli
build-sbcli: ## Build sbcli image locally
	@echo "Building sbcli image..."
	@echo "Image: $(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG)"
	@echo "Context: $(SBCLI_CONTEXT)"
	@if [ ! -f $(SBCLI_DOCKERFILE) ]; then \
		echo "Error: Dockerfile not found at $(SBCLI_DOCKERFILE)"; \
		exit 1; \
	fi
	docker buildx build --load \
		$(if $(SBCLI_PLATFORM),--platform $(SBCLI_PLATFORM)) \
		--build-arg BASE_IMAGE=$(SBCLI_BASE_IMAGE) \
		-t $(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG) \
		-f $(SBCLI_DOCKERFILE) \
		$(SBCLI_CONTEXT)
	@echo "Build complete: $(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG)"

.PHONY: build-sbcli-arm64
build-sbcli-arm64: ## Build arm64 sbcli image locally (GB300)
	$(MAKE) build-sbcli SBCLI_PLATFORM=linux/arm64 SBCLI_IMAGE_TAG=$(SBCLI_IMAGE_TAG)-arm64

.PHONY: build-moe-bench
build-moe-bench: ## Build GB300 MoE test image (HybridEP, DeepEP V1, MoonEP, DeepGEMM, linux/arm64)
	docker buildx build --load \
		--platform $(MOE_BENCH_PLATFORM) \
		--build-arg MAX_JOBS=$(MOE_BENCH_MAX_JOBS) \
		-t $(MOE_BENCH_IMAGE_NAME):$(MOE_BENCH_IMAGE_TAG) \
		-f $(MOE_BENCH_DOCKERFILE) \
		$(MOE_BENCH_CONTEXT)

.PHONY: test-moe-bench
test-moe-bench: ## Print test list and build info of the moe-bench image (no GPU needed)
	docker run --rm --platform $(MOE_BENCH_PLATFORM) $(MOE_BENCH_IMAGE_NAME):$(MOE_BENCH_IMAGE_TAG) \
		bash -c 'cat /etc/moe-bench-build.info && moe-bench --list'

.PHONY: build-nixl-benchmark
build-nixl-benchmark: ## Build NIXL/NIXLBench image for NIXL_PROFILE (profiles/*.env; env vars override)
	@set -e; \
	args="$$($(NIXL_BENCHMARK_PROFILE_SH) $(NIXL_PROFILE) args)"; \
	platform="$$($(NIXL_BENCHMARK_PROFILE_SH) $(NIXL_PROFILE) get PLATFORM)"; \
	echo "Profile: $(NIXL_PROFILE) ($$platform)"; echo "$$args" | sed 's/^/  /'; \
	set --; for a in $$args; do set -- "$$@" --build-arg "$$a"; done; \
	docker buildx build --load $${platform:+--platform $$platform} "$$@" \
		--build-arg "GIT_COMMIT_VERSION=$$(git rev-parse --short HEAD 2>/dev/null)" \
		-t $(NIXL_BENCHMARK_IMAGE_NAME):$(NIXL_BENCHMARK_IMAGE_TAG) \
		$(NIXL_BENCHMARK_CONTEXT)
	@echo "Build complete: $(NIXL_BENCHMARK_IMAGE_NAME):$(NIXL_BENCHMARK_IMAGE_TAG)"

.PHONY: test-nixl-benchmark
test-nixl-benchmark: ## Print build info and plugins of the nixl-benchmark image (no GPU needed)
	docker run --rm --platform $$($(NIXL_BENCHMARK_PROFILE_SH) $(NIXL_PROFILE) get PLATFORM) \
		$(NIXL_BENCHMARK_IMAGE_NAME):$(NIXL_BENCHMARK_IMAGE_TAG) \
		bash -c 'cat /etc/nixl-benchmark-build.info && ls /usr/local/nixl/lib/plugins/ && run-nixlbench nixlbench --help | head -n 5'

.PHONY: test-sbcli
test-sbcli: ## Run sb version in the local sbcli image
	docker run --rm $(if $(SBCLI_PLATFORM),--platform $(SBCLI_PLATFORM)) --entrypoint sb $(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG) version

.PHONY: test-sbcli-interactive
test-sbcli-interactive: ## Open a shell in the local sbcli image
	docker run -it --rm $(if $(SBCLI_PLATFORM),--platform $(SBCLI_PLATFORM)) --entrypoint /bin/bash $(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG)

.PHONY: test-testkit
test-testkit: ## Run testkit unit tests (benchmarks/testkit)
	cd benchmarks/testkit && python3 -m unittest discover -s tests -v

.PHONY: build-testkit-bin
build-testkit-bin: ## Build single-file testkit binaries for amd64 and arm64 (benchmarks/testkit/dist)
	benchmarks/testkit/build-binary.sh

.PHONY: clean
clean: ## Remove locally built sbcli images
	-docker rmi $(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG) $(SBCLI_IMAGE_NAME):$(SBCLI_IMAGE_TAG)-arm64 2>/dev/null

# ISO targets. The build-iso scripts read ISO_DIR as the base-ISO cache
# directory, which clashes with this Makefile's ISO_DIR, so it is unset first.
ISO_ENV = MANIFEST_FILE=$(abspath $(ISO_MANIFEST)) CUDA_PROFILE=$(CUDA_PROFILE) CACHE_BASE_DIR=$(abspath $(CACHE_BASE_DIR))

.PHONY: prepare-iso
prepare-iso: ## Download base ISO and offline packages (prepare.sh --skip-images)
	unset ISO_DIR; cd $(ISO_DIR) && $(ISO_ENV) ./prepare.sh --skip-images

.PHONY: build-iso
build-iso: ## Build the custom ISO (needs root: sudo make build-iso)
	unset ISO_DIR; cd $(ISO_DIR) && $(ISO_ENV) ./build.sh

.PHONY: test-iso
test-iso: ## Run the QEMU autoinstall smoke test (test.sh setup)
	cd $(ISO_DIR) && ./test.sh setup
