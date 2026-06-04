# ISO 输入目录

默认本地构建不会直接使用当前目录，`prepare.sh` 会把基础 Ubuntu ISO 下载到：

```text
build-iso/.ci-work/iso/
```

只有在手动覆盖 `ISO_DIR` 时，才会改为从当前目录读取源 ISO，例如：

```bash
export ISO_DIR="$PWD/build-iso/iso"
cd build-iso && ./prepare.sh --skip-images
```
