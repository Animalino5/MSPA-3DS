#!/usr/bin/env bash
# build_ruffle_windows.sh — cross-compile the MSPA-3DS ruffle exporter for
# Windows (x86_64) from a Linux machine.
#
# The exporter sources are taken from the EXACT patched copies embedded in
# build_gui.py (RUFFLE_PATCHED_SOURCES), so the Windows binary behaves
# identically to the shipped Linux one — no manual patch application, no
# version drift.
#
# Requirements (one-time):
#   - Rust + rustup          → https://rustup.rs
#   - mingw-w64 cross linker → Debian/Ubuntu: sudo apt-get install mingw-w64
#                              Fedora:        sudo dnf install mingw64-gcc
#
# Usage:
#   ./build_ruffle_windows.sh [output-dir]
#   (default output: ./ruffle-exporter/ruffle_exporter.exe next to this
#    script — exactly where the frozen .exe and build_gui.py look for it)

set -euo pipefail

SOURCE_URL="${RUFFLE_SOURCE_URL:-https://github.com/ruffle-rs/ruffle/releases/download/nightly-2026-09-08/ruffle-nightly-2026_09_08-reproducible-source.zip}"
TARGET="x86_64-pc-windows-gnu"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${1:-$HERE/ruffle-exporter}"

command -v cargo >/dev/null 2>&1 || {
    echo "cargo not found — install Rust from https://rustup.rs first." >&2
    exit 1
}

# rustup may live in ~/.cargo/bin without being on PATH
if ! command -v rustup >/dev/null 2>&1; then
    export PATH="$HOME/.cargo/bin:$PATH"
fi

if ! rustup target list --installed | grep -q "^${TARGET}$"; then
    echo "[1/4] adding Rust target ${TARGET} ..."
    rustup target add "${TARGET}"
fi

WORK="$(mktemp -d /tmp/mspa3ds-ruffle-win.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

echo "[2/4] downloading the pinned ruffle source ..."
curl -fL --retry 3 -o "$WORK/src.zip" "$SOURCE_URL"
mkdir -p "$WORK/src"
unzip -q "$WORK/src.zip" -d "$WORK/src"

# The reproducible zip has either a single root folder or the workspace
# members at the top level — find the one containing exporter/Cargo.toml.
SRC_ROOT="$WORK/src"
if [ ! -f "$SRC_ROOT/exporter/Cargo.toml" ]; then
    SRC_ROOT="$(dirname "$(find "$WORK/src" -name Cargo.toml -path '*/exporter/Cargo.toml' | head -n1)")/.."
fi
SRC_ROOT="$(cd "$SRC_ROOT" && pwd)"
echo "      source root: $SRC_ROOT"

echo "[3/4] writing the MSPA-3DS patched exporter sources (from build_gui.py) ..."
python3 - "$HERE/build_gui.py" "$SRC_ROOT/exporter/src" <<'PY'
import re, sys, bz2, base64

script, outdir = sys.argv[1], sys.argv[2]
src = open(script, encoding="utf-8").read()

m = re.search(r"RUFFLE_PATCHED_SOURCES = \{(.*?)\n\}\n", src, re.S)
if not m:
    sys.exit("RUFFLE_PATCHED_SOURCES not found in build_gui.py")

ns = {}
exec("RUFFLE_PATCHED_SOURCES = {" + m.group(1) + "}", ns)
for fname, packed in ns["RUFFLE_PATCHED_SOURCES"].items():
    raw = bz2.decompress(base64.b64decode("".join(packed.split())))
    path = f"{outdir}/{fname}"
    with open(path, "wb") as f:
        f.write(raw)
    print(f"      wrote {fname} ({len(raw)} bytes)")
PY

echo "[4/4] cargo build — cross-compiling to ${TARGET} (a few minutes) ..."
cargo build --release -p exporter --target "$TARGET" \
    --manifest-path "$SRC_ROOT/Cargo.toml"

BIN="$SRC_ROOT/target/$TARGET/release/exporter.exe"
if [ ! -f "$BIN" ]; then
    echo "build finished but $BIN was not found" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"
cp "$BIN" "$OUT_DIR/ruffle_exporter.exe"
echo "DONE → $OUT_DIR/ruffle_exporter.exe"
echo "The Linux ELF binary in that folder (if any) can stay — Windows only"
echo "loads ruffle_exporter.exe, Linux only loads ruffle_exporter."
