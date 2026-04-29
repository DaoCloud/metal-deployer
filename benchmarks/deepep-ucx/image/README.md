# DeepEP + UCX 镜像构建指南

本文档描述 DeepEP + UCX 测试工具镜像的构建方法。

---

## 目录结构

```
benchmarks/deepep-ucx/image/
├── Dockerfile              # 镜像构建文件
├── README.md               # 本文档
├── scripts/
│   ├── build-deepep.sh     # DeepEP/DeepGEMM 构建脚本
│   └── build-ucx.sh        # UCX 构建脚本
└── tools/
    ├── testDeepEP          # DeepEP 测试工具
    └── testUcx             # UCX RDMA 测试工具
```

---

## 本地构建

### 使用 Makefile（推荐）

从项目根目录执行：

```bash
# 构建 deepep-ucx 镜像（使用默认参数）
make build-deepep-ucx

# 指定镜像标签
make build-deepep-ucx DEEPEP_UCX_IMAGE_TAG=v1.0.0

# 指定 CUDA 版本
make build-deepep-ucx ENV_BASEIMAGE_CUDA_VERISON=12.8

# 指定组件版本
make build-deepep-ucx ENV_DEEPEP_VERSION=v1.2.1 ENV_UCX_VERSION=v1.20.0
```

### 使用 Docker 直接构建

```bash
cd benchmarks/deepep-ucx/image

# 基础构建
docker build -t deepep-ucx:latest .

# 带构建参数
docker build \
  --build-arg ENV_BUILD_TOOLS_IMAGE_NAME=nvidia/cuda:12.8-devel-ubuntu22.04 \
  --build-arg ENV_BASEIMAGE_FULL_NAME=nvidia/cuda:12.8-runtime-ubuntu22.04 \
  --build-arg ENV_DEEPEP_VERSION=v1.2.1 \
  --build-arg ENV_DEEPGEMM_VERSION=v2.1.1.post3 \
  --build-arg ENV_UCX_VERSION=v1.20.0 \
  --build-arg ENV_NVSHMEM_VERSION=v3.4.5-0 \
  --build-arg ENV_TORCH_CUDA_ARCH_LIST="7.0;7.5;8.0;8.6;9.0" \
  -t deepep-ucx:latest \
  .
```

---

## 构建参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ENV_BUILD_TOOLS_IMAGE_NAME` | `nvidia/cuda:12.8.1-devel-ubuntu22.04` | 编译阶段基础镜像 |
| `ENV_BASEIMAGE_FULL_NAME` | `nvidia/cuda:12.8.1-runtime-ubuntu22.04` | 运行阶段基础镜像 |
| `ENV_BASEIMAGE_CUDA_VERISON` | `12.8` | CUDA 版本 |
| `ENV_DEEPEP_VERSION` | `v1.2.1` | DeepEP 版本 |
| `ENV_DEEPGEMM_VERSION` | `v2.1.1.post3` | DeepGEMM 版本 |
| `ENV_UCX_VERSION` | `v1.20.0` | UCX 版本 |
| `ENV_NVSHMEM_VERSION` | `v3.4.5-0` | NVSHMEM 版本 |
| `ENV_BUILD_AND_DOWNLOAD_PARALLEL` | `4` | 编译并行度 |
| `ENV_TORCH_CUDA_ARCH_LIST` | `7.0;7.5;8.0;8.6;9.0` | GPU 架构列表 |

---

## CI/CD 自动构建

GitHub Actions 工作流 `.github/workflows/build-deepep-ucx-image.yml` 会在以下情况自动构建并推送镜像：

- **PR 开启/更新**：修改 `benchmarks/deepep-ucx/image/**` 目录时触发构建（不推送）
- **main 分支推送**：构建并推送 `latest` 标签
- **Release 标签**：构建并推送对应版本标签（如 `v1.0.0` → `ghcr.io/daocloud/metal-deployer/deepep-ucx:v1.0.0`）

### 镜像地址

```
ghcr.io/daocloud/metal-deployer/deepep-ucx:latest
ghcr.io/daocloud/metal-deployer/deepep-ucx:v1.0.0
```

---

## 参考链接

- [DeepEP GitHub](https://github.com/deepseek-ai/DeepEP)
- [DeepGEMM GitHub](https://github.com/deepseek-ai/DeepGEMM)
- [UCX Documentation](https://openucx.readthedocs.io/)
- [NVIDIA NVSHMEM](https://developer.nvidia.com/nvshmem)
