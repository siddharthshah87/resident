# resident — agent-native Debian rootfs, booted as a systemd container with a read-only root.
# Build:  docker build -t resident-image:dev image/
# Run:    see image/README.md (--read-only root, tmpfs /etc populated from /usr/share/factory/etc, /var volume)
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive container=docker RESIDENT_HOME=/var/lib/resident

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus python3 iproute2 procps ca-certificates curl jq less \
 && apt-get clean && rm -rf /var/lib/apt/lists/* /var/cache/apt/*.bin /var/log/apt /var/log/dpkg.log

# --- resident tooling (verbatim copy of ~/resident/lib/agentos.py; symlinks give the three CLIs)
COPY agentos.py seed-intents.py intents.json resident-init /usr/lib/resident/
COPY units/ /usr/lib/systemd/system/
RUN chmod 0755 /usr/lib/resident/agentos.py /usr/lib/resident/seed-intents.py /usr/lib/resident/resident-init \
 && for n in resident hf agentos intent why; do ln -s /usr/lib/resident/agentos.py /usr/bin/$n; done \
 && echo 'RESIDENT_HOME=/var/lib/resident' >> /etc/environment \
 # the resident agent's identity: system user, no password, no shell login, owns the state dir
 && useradd --system --user-group --home-dir /var/lib/resident --shell /usr/sbin/nologin agent \
 && install -d -o agent -g agent -m 2770 /var/lib/resident \
 && install -d /var/log/journal \
 && systemctl enable resident-world.timer resident-firstboot.service \
 # apt cannot run on a read-only root; its daily timers would only fail (and page the agent)
 && systemctl mask apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service \
 # tmpfiles line for the systemd-native factory path (no-op when resident-init already populated /etc)
 && printf '# resident: populate an empty /etc from the factory copy\nC /etc - - - - -\n' > /usr/lib/tmpfiles.d/resident-factory-etc.conf

# --- immutable-root plumbing
# 1. The package database describes the (read-only) image, so it lives with the image, not in mutable /var.
#    This keeps dpkg-query/apt-mark correct after the image is rebuilt under an existing /var volume.
RUN mkdir -p /usr/share/resident && mv /var/lib/dpkg /usr/share/resident/dpkg \
 && ln -s /usr/share/resident/dpkg /var/lib/dpkg \
 && mv /var/lib/apt /usr/share/resident/apt && ln -s /usr/share/resident/apt /var/lib/apt \
 # 2. Factory copy of /etc: at boot /etc is an empty tmpfs; resident-init copies this tree in (config is a cache).
 && mkdir -p /usr/share/factory && cp -a /etc /usr/share/factory/etc \
 && rm -f /usr/share/factory/etc/hostname /usr/share/factory/etc/hosts /usr/share/factory/etc/resolv.conf \
 && : > /usr/share/factory/etc/machine-id

STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/usr/lib/resident/resident-init"]
CMD ["/sbin/init"]

# Identify as Resident (a Debian derivative). /etc is rebuilt from factory each boot; os-release lives in /usr.
RUN rm -f /usr/lib/os-release && printf '%s\n' \
  'PRETTY_NAME="Resident Linux 26.11 (Kelp) — make your machine your own assistant"' \
  'NAME="Resident Linux"' 'ID=resident' 'ID_LIKE=debian' \
  'VERSION="26.11 (Kelp)"' 'VERSION_ID=26.11' 'VERSION_CODENAME=kelp' \
  'VARIANT="container prototype"' 'VARIANT_ID=container' > /usr/lib/os-release
