# Repository Guidelines

## Project Structure & Module Organization

This repository has two main areas. `build-iso/` builds a custom Ubuntu 24.04 autoinstall ISO; its key files are `manifest.yaml`, `user-data`, `prepare.sh`, `build.sh`, `test.sh`, `scripts/`, `packages/`, `config/`, and `tools/`. `superbenchmark/` contains GPU/RDMA benchmarking assets, including the sbcli Docker image under `superbenchmark/image/`. General docs live in `README.md`, `guide.md`, and `docs/`. GitHub Actions workflows are under `.github/workflows/`.

Avoid committing generated artifacts such as `build-iso/custom-ubuntu.iso`, `build-iso/build_workspace/`, `build-iso/qemu.log`, `build-iso/qemu.pid`, or test disk images.

## Build, Test, and Development Commands

Run commands from the repository root unless noted.

- `make help`: show available targets.
- `make prepare-iso`: run `build-iso/prepare.sh --skip-images` to download the base ISO and packages.
- `sudo make build-iso`: build `build-iso/custom-ubuntu.iso`.
- `make test-iso`: run the QEMU autoinstall smoke test.
- `cd build-iso && ./test.sh login`: SSH into the running QEMU test VM.
- `cd build-iso && ./test.sh clean`: stop and remove QEMU test artifacts.
- `make build-sbcli`: build the SuperBench control Docker image locally.

## Coding Style & Naming Conventions

Shell scripts use Bash with clear functions, quoted variables, and `set -e` or `set -euo pipefail` where practical. Use two or four spaces consistently within edited blocks, matching the surrounding file. YAML files use two-space indentation. Keep manifest keys descriptive and snake_case where the project owns the schema; preserve upstream autoinstall keys such as `install-server` and `allow-pw`.

Use `apply_patch`-style small edits and keep unrelated formatting churn out of changes.

## Testing Guidelines

For shell edits, run syntax checks:

```bash
bash -n build-iso/build.sh
bash -n build-iso/prepare.sh
```

For Ruby helpers, run:

```bash
ruby -c build-iso/tools/render_user_data.rb
```

For ISO behavior, prefer `make test-iso` or `cd build-iso && ./test.sh setup`. Check `/var/log/scripts.log` and `/var/log/metal-deployer/` inside the VM when debugging first-boot failures.

## Commit & Pull Request Guidelines

Recent commits use short descriptive subjects, for example `Add manifest.yaml for Ubuntu 24.04 ISO build configuration` or `Update README.md`. Keep subjects imperative or descriptive, under about 72 characters when possible.

Pull requests should include a concise summary, the affected area (`build-iso`, `superbenchmark`, docs, CI), validation commands run, and any known limitations. Link related issues when available. For ISO changes, mention whether QEMU testing was run and whether generated ISO artifacts were intentionally excluded.

## Security & Configuration Tips

Do not commit private keys, credentials, local registry tokens, or environment-specific secrets. SSH public keys may be provided through `build-iso/config/ssh_authorized_keys`, but private keys must stay out of the ISO. Passwords in autoinstall must be SHA-512 hashes, not plaintext.

Respond terse like smart caveman. All technical substance stay. Only fluff die.

Rules:
- Drop: articles (a/an/the), filler (just/really/basically), pleasantries, hedging
- Fragments OK. Short synonyms. Technical terms exact. Code unchanged.
- Pattern: [thing] [action] [reason]. [next step].
- Not: "Sure! I'd be happy to help you with that."
- Yes: "Bug in auth middleware. Fix:"

Switch level: /caveman lite|full|ultra|wenyan
Stop: "stop caveman" or "normal mode"

Auto-Clarity: drop caveman for security warnings, irreversible actions, user confused. Resume after.

Boundaries: code/commits/PRs written normal.
