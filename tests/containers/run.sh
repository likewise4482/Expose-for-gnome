#!/usr/bin/env bash
# Run tests/integration/shell_test.py (headless Wayland) against several GNOME
# Shell versions in containers. Needs podman (or docker via PODMAN=docker).
#   tests/containers/run.sh                 # all targets
#   tests/containers/run.sh fedora:42 ...   # selected targets
# Results: test-output/containers/<target>/
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PODMAN="${PODMAN:-podman}"
OUT="$ROOT/test-output/containers"
TARGETS=("$@")
[ ${#TARGETS[@]} -gt 0 ] || TARGETS=(fedora:39 fedora:41 fedora:42 debian:trixie fedora:43 fedora:44)

make -C "$ROOT" zip >/dev/null || exit 1
mkdir -p "$OUT"
summary=()

for target in "${TARGETS[@]}"; do
    tag="efg-test-${target//[:\/]/-}"
    dir="$OUT/${target//[:\/]/-}"
    mkdir -p "$dir" && chmod a+rwx "$dir"
    case "$target" in
        fedora:*)
            base="registry.fedoraproject.org/$target"
            release="${target#fedora:}"
            # End-of-life Fedora releases live on the archive mirror.
            pkgs="gnome-shell python3-gobject gtk4 dbus-daemon dconf mesa-dri-drivers glib2 xorg-x11-server-Xvfb"
            setup="rm -f /etc/yum.repos.d/fedora-cisco-openh264.repo /etc/yum.repos.d/fedora-updates-testing.repo; \
(dnf -y -q install $pkgs || (sed -i -e 's|^metalink=|#metalink=|' \
-e 's|^#baseurl=http://download.example/pub/fedora/linux|baseurl=https://archives.fedoraproject.org/pub/archive/fedora/linux|' \
/etc/yum.repos.d/*.repo && dnf -y -q install $pkgs)) >/dev/null && dnf clean all >/dev/null"
            ;;
        debian:*|ubuntu:*)
            base="docker.io/library/$target"
            setup="apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
--no-install-recommends gnome-shell python3-gi gir1.2-gtk-4.0 dbus dbus-x11 dconf-service \
dconf-cli libgl1-mesa-dri libglib2.0-bin gsettings-desktop-schemas xvfb >/dev/null && rm -rf /var/lib/apt/lists/*"
            ;;
        *) echo "unknown target $target"; continue ;;
    esac
    if ! $PODMAN image exists "$tag" 2>/dev/null; then
        echo "== building $tag from $base"
        printf 'FROM %s\nRUN %s\nRUN useradd -m tester\n' "$base" "$setup" |
            $PODMAN build -q -t "$tag" -f - "$ROOT" >"$dir/build.log" 2>&1 || {
                echo "build failed: see $dir/build.log"; summary+=("$target: BUILD FAILED"); continue; }
    fi
    version="$($PODMAN run --rm "$tag" gnome-shell --version 2>/dev/null)"
    major="$(echo "$version" | sed -n 's/.* \([0-9][0-9]*\).*/\1/p')"
    . "$ROOT/project.env"
    extra="" note=""
    if ! printf ' %s ' "$SHELL_VERSIONS" | grep -q " $major "; then
        extra="--ignore-version" note=" [probe: $major is not in metadata.json]"
    fi
    echo "== $target: $version$note"
    # GNOME 48 talks to logind over the system bus at startup, so give it one.
    $PODMAN run --rm -v "$ROOT:/src:ro" -v "$dir:/out:rw" "$tag" sh -c '
        mkdir -p -m 1777 /tmp/.X11-unix; mkdir -p /run/dbus
        printf "%s" "<busconfig><policy context=\"default\"><allow own=\"org.freedesktop.login1\"/><allow send_destination=\"org.freedesktop.login1\"/></policy></busconfig>" \
            > /etc/dbus-1/system.d/efg-test-login1.conf
        dbus-daemon --system --fork 2>/dev/null
        python3 /src/tests/containers/fake_logind.py & sleep 1
        exec runuser -u tester -- python3 /src/tests/integration/shell_test.py --mode headless --out /out '"$extra" \
        >"$dir/run.log" 2>&1
    rc=$?
    grep -E '^\[(PASS|FAIL)\]' "$dir/run.log" | sed 's/^/   /'
    summary+=("$target ($version)$note: $([ $rc = 0 ] && echo PASSED || echo FAILED)")
done

echo
printf '%s\n' "${summary[@]}" | tee "$OUT/summary.txt"
