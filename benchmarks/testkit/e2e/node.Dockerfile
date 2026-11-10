# Fake GB300 host for mtest end-to-end tests: sshd plus the docker CLI.
# The host's /var/run/docker.sock is mounted, so "ssh node docker run ..." starts
# containers on the machine that runs the test.
FROM docker:cli
RUN apk add --no-cache openssh-server bash rsync tar coreutils \
    && ssh-keygen -A \
    && sed -i 's/^root:!/root:*/' /etc/shadow \
    && mkdir -p /root/.ssh && chmod 700 /root/.ssh
ENTRYPOINT ["/usr/sbin/sshd", "-D", "-e", "-o", "PermitRootLogin=prohibit-password", "-o", "PasswordAuthentication=no"]
