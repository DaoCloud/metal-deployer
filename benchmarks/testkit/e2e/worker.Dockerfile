# Test image for mtest end-to-end tests: sh for per-node items, sshd + OpenMPI for mpi mode.
FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server openssh-client openmpi-bin procps \
    && rm -rf /var/lib/apt/lists/*
