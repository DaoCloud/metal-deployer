# Fake GB300 host with systemd, netplan and sshd, for the S3/S4 end-to-end test.
# BASE can point at a registry mirror, for example docker.m.daocloud.io/library/ubuntu:24.04.
ARG BASE=ubuntu:24.04
FROM ${BASE}
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       systemd systemd-sysv dbus netplan.io iproute2 iputils-ping openssh-server python3 \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl enable ssh \
    && mkdir -p /root/.ssh && chmod 700 /root/.ssh \
    && rm -f /etc/netplan/*.yaml
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
