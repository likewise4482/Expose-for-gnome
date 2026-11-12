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
[ ${#TARGETS[@]} -gt 0 ] || TARGETS=(fedora:39 fedora:40 fedora:41 fedora:42 fedora:43 debian:trixie ubuntu:24.04)

make -C "$ROOT" zip >/dev/null || exit 1
mkdir -p "$OUT"
summary=()

for target in "${TARGETS[@]}"; do
    tag="efg-test-${target//[:\/]/-}"
    dir="$OUT/${target//[:\/]/-}"
    mkdir -p "$dir"
    case "$target" in
        fedora:*)
            base="registry.fedoraproject.org/$target"
            release="${target#fedora:}"
            # End-of-life Fedora releases live on the archive mirror.
            setup="(dnf -y -q makecache >/dev/null 2>&1 || sed -i -e 's|^metalink=|#metalink=|' \
-e 's|^#baseurl=http://download.example/pub/fedora/linux|baseurl=https://archives.fedoraproject.org/pub/archive/fedora/linux|' \
/etc/yum.repos.d/*.repo) && dnf -y -q install gnome-shell python3-gobject gtk4 dbus-daemon \
dconf mesa-dri-drivers glib2 xorg-x11-server-Xvfb >/dev/null && dnf clean all >/dev/null"
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
    echo "== $target: $version"
    $PODMAN run --rm --user tester -v "$ROOT:/src:ro" -v "$dir:/out:rw" "$tag" \
        python3 /src/tests/integration/shell_test.py --mode headless --out /out \
        >"$dir/run.log" 2>&1
    rc=$?
    grep -E '^\[(PASS|FAIL)\]' "$dir/run.log" | sed 's/^/   /'
    summary+=("$target ($version): $([ $rc = 0 ] && echo PASSED || echo FAILED)")
done

echo
printf '%s\n' "${summary[@]}" | tee "$OUT/summary.txt"
