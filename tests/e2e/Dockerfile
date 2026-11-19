# Real (GUI) Kodi under Xvfb, for the JSON-RPC end-to-end test
# (scripts/kodi_e2e_test.sh). The matthuisman/kodi-headless images used by the
# import smoke test have no window manager at all - xbmcgui.Window(10000)
# raises "Window id does not exist" there, so the add-on's window-property
# cache (and therefore every search) cannot run. Distro Kodi + a virtual
# framebuffer gives a complete Kodi: home window, player, plugin directories.
#
# BASE picks the Kodi release via the distro's own package:
#   ubuntu:22.04 -> Kodi 19 Matrix   debian:bookworm -> Kodi 20 Nexus
#   debian:trixie -> Kodi 21 Omega
# (Debian where possible: its CDN-backed mirror has been the reliable one.
# Matrix stays on Ubuntu because Debian 11, the only Debian with Kodi 19, is
# end-of-life and its security pool already returns 404s.)
# Pre-releases without a distro package use ./Dockerfile.flatpak instead.
ARG BASE=debian:trixie
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
# Package downloads must neither hang nor fail on one bad mirror. The first
# GitHub runs lost Ubuntu-based builds both ways within an hour: a connection
# to archive.ubuntu.com that trickled until the job timed out, then
# azure.archive.ubuntu.com refusing connections. So: connection timeouts, and
# on Ubuntu each failed attempt moves on to the next mirror of the same
# archive (the sed is a no-op on Debian). Already downloaded .debs are kept
# between attempts, so every retry only fetches what is still missing. Each
# attempt is also time-boxed: apt's timeouts catch a dead connection, not one
# that delivers a few bytes per second.
RUN printf '%s\n' 'Acquire::Retries "3";' 'Acquire::http::Timeout "30";' \
      'Acquire::https::Timeout "30";' 'Acquire::ForceIPv4 "true";' \
      > /etc/apt/apt.conf.d/80-e2e-network
RUN set -e; current='(archive|security)\.ubuntu\.com'; ok=; \
    for mirror in '' mirrors.edge.kernel.org azure.archive.ubuntu.com archive.ubuntu.com; do \
      if [ -n "$mirror" ]; then \
        find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) -exec \
          sed -i -E "s#${current}#${mirror}#g" {} +; \
        current=$(printf '%s' "$mirror" | sed 's/\./\\./g'); \
      fi; \
      if timeout 300 apt-get update && timeout 300 apt-get install -y --no-install-recommends \
           kodi xvfb libgl1-mesa-dri ffmpeg curl ca-certificates tzdata; then ok=1; break; fi; \
      echo "apt failed or too slow (mirror: ${mirror:-default}), trying the next one"; \
      dpkg --configure -a || true; \
    done; \
    [ -n "$ok" ] && rm -rf /var/lib/apt/lists/*
ENV HOME=/root
# The driver copies add-ons and userdata (advancedsettings.xml enabling
# JSON-RPC over HTTP) in here before the first start.
RUN mkdir -p /root/.kodi /media/e2e
# Not xvfb-run: as PID 1 it never receives Xvfb's SIGUSR1 "ready" signal and
# hangs before starting Kodi. The lock is removed so `docker restart` works.
CMD ["sh", "-c", "rm -f /tmp/.X99-lock; Xvfb :99 -screen 0 1280x720x24 -nolisten tcp & sleep 2; DISPLAY=:99 exec kodi --standalone"]
