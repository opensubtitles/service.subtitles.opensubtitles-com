# Real (GUI) Kodi under Xvfb, for the JSON-RPC end-to-end test
# (scripts/kodi_e2e_test.sh). The matthuisman/kodi-headless images used by the
# import smoke test have no window manager at all - xbmcgui.Window(10000)
# raises "Window id does not exist" there, so the add-on's window-property
# cache (and therefore every search) cannot run. Distro Kodi + a virtual
# framebuffer gives a complete Kodi: home window, player, plugin directories.
#
# BASE picks the Kodi release via the distro's own package:
#   ubuntu:22.04 -> Kodi 19 Matrix   ubuntu:24.04 -> Kodi 20 Nexus
#   debian:trixie -> Kodi 21 Omega
# Pre-releases without a distro package use ./Dockerfile.flatpak instead.
ARG BASE=debian:trixie
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
# Fail fast instead of hanging: on GitHub's runners a stalled connection to
# the Ubuntu archive once blocked this step until the job timed out.
RUN printf '%s\n' 'Acquire::Retries "5";' 'Acquire::http::Timeout "30";' \
      'Acquire::https::Timeout "30";' 'Acquire::ForceIPv4 "true";' \
      > /etc/apt/apt.conf.d/80-e2e-network
# Ubuntu bases only (no-op on Debian): archive.ubuntu.com is the round-robin
# that stalled; this mirror serves the same archive, security pocket included.
ARG UBUNTU_MIRROR=azure.archive.ubuntu.com
RUN find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) -exec \
      sed -i -E "s#(archive|security)\.ubuntu\.com#${UBUNTU_MIRROR}#g" {} +
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      kodi xvfb libgl1-mesa-dri ffmpeg curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
ENV HOME=/root
# The driver copies add-ons and userdata (advancedsettings.xml enabling
# JSON-RPC over HTTP) in here before the first start.
RUN mkdir -p /root/.kodi /media/e2e
# Not xvfb-run: as PID 1 it never receives Xvfb's SIGUSR1 "ready" signal and
# hangs before starting Kodi. The lock is removed so `docker restart` works.
CMD ["sh", "-c", "rm -f /tmp/.X99-lock; Xvfb :99 -screen 0 1280x720x24 -nolisten tcp & sleep 2; DISPLAY=:99 exec kodi --standalone"]
