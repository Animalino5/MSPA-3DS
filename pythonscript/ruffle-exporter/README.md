# ruffle-exporter — MSPA-3DS build

This is the **Ruffle exporter** (headless SWF → PNG frame renderer) used as the
primary flash conversion engine for MSPA-3DS bundles.

It replaces the FFDec (JPEXS Java decompiler) frame pipeline, which was
extremely slow on long flashes and timed out on movies like
[S] Make her pay (5558 frames → the page "would not download").

## Performance (measured on [S] Make her pay, 5558 frames @ 25fps)

| Pipeline | Time | Result |
|---|---|---|
| FFDec `frame:avi` (old) | >600s | **timeout, page lost** |
| **Ruffle exporter (this)** | **~21s render + ~10s encode** | 1333 frames @ exact 6fps |

## Local patches vs upstream `ruffle-rs/ruffle` (nightly-2026-09-08)

The exporter crate was patched in three ways (patch files in
`exporter-patch/` — apply over a fresh ruffle source tree, or build the
included source directly):

1. **Streaming frame writes** — upstream `--frames all` accumulated *every*
   frame in RAM before writing (6.5 GB for Make her pay → OOM). Frames are now
   written to disk as they are rendered, keeping memory constant.
2. **`--frame-indices` / `--frame-indices-file`** — capture only a given set
   of source frame indices (e.g. the exact frames that sample a 25fps movie
   at 6fps). Unselected frames are simulated but never PNG-encoded.
   Indices may be passed inline (comma-separated) or via a file
   (newline/comma/space separated, no command-line length limits).
3. **Single-frame output fix** — `--frames 1 <swf> <outdir>` upstream failed
   with "The image format could not be determined" because it tried to save
   a PNG to an extension-less directory path. Extension-less outputs are now
   treated as directories and `0.png` is written inside.

## Building from source

Requires Rust (https://rustup.rs) and a C toolchain:

    git clone https://github.com/ruffle-rs/ruffle
    cd ruffle
    # apply exporter-patch/*.diff if you want the patches above
    cargo build --release -p exporter
    # binary: target/release/exporter → copy here as ruffle_exporter

Windows builds produce `exporter.exe` — rename to `ruffle_exporter.exe`.
The GUI can also do this automatically: set `RUFFLE_AUTO_BUILD=1`.

## Rendering backends

The exporter uses wgpu and works headless. `build_gui.py` auto-picks a
backend (default → vulkan → gl). On Linux boxes without a GPU, install
software Vulkan (lavapipe: `mesa-vulkan-drivers`) or Mesa GL, and set
`LIBGL_ALWAYS_SOFTWARE=1` (done automatically). On Windows, DX12/Vulkan
are used when available.

## Environment variables

| Variable | Effect |
|---|---|
| `RUFFLE_EXPORTER_PATH` | Explicit path to the exporter binary |
| `RUFFLE_AUTO_BUILD=1` | Build from source automatically if missing |
| `RUFFLE_SOURCE_URL` | Override the pinned ruffle source zip URL |
| `LIBGL_ALWAYS_SOFTWARE=1` | Force software GL on Linux |
