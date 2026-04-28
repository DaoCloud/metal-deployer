FROM python:3.11-slim

ARG SUPERBENCH_REPO_URL=https://github.com/microsoft/superbenchmark
ARG SUPERBENCH_TAG=v0.12.0

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    openssh-client \
    rsync \
    sshpass \
    vim \
    && rm -rf /var/lib/apt/lists/*

COPY patches/control-node-use-inventory-hosts.patch /tmp/control-node-use-inventory-hosts.patch

WORKDIR /opt/superbenchmark

RUN git clone --branch ${SUPERBENCH_TAG} --depth 1 ${SUPERBENCH_REPO_URL} /opt/superbenchmark

RUN git apply /tmp/control-node-use-inventory-hosts.patch

RUN python -m pip install --retries 10 --timeout 300 .

RUN mkdir -p /etc/ssh && \
    printf '%s\n' \
    'Host *' \
    '    StrictHostKeyChecking no' \
    '    UserKnownHostsFile /dev/null' \
    '    LogLevel ERROR' \
    > /etc/ssh/ssh_config && \
    chmod 600 /etc/ssh/ssh_config

RUN sb version

COPY configs/ /workspace/
COPY scripts/ /workspace/scripts/
RUN chmod +x /workspace/scripts/*.sh

WORKDIR /workspace

ENTRYPOINT ["sleep"]
CMD ["infinity"]