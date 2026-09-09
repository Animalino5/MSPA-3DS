#!/usr/bin/env python3
"""
MSPA-3DS Bundle Builder — GUI Edition
======================================
A tkinter GUI wrapper for the MSPA-3DS scraper/packager.
Can be frozen into a standalone .exe with PyInstaller (see
BUILD-WINDOWS-EXE.md for the full recipe, including how to build the
Windows .exe from Linux with Docker/Wine):

    pip install pyinstaller
    pyinstaller --onefile --windowed --name "MSPA-3DS-Builder" \
        --add-data "ruffle-exporter:ruffle-exporter" build_gui.py

When frozen, tools are searched next to the .exe first (portable
"tools beside the exe" layout), then in the bundled --add-data payload,
then next to this script - see _is_frozen/_app_dirs/_tool_install_dir.

Requires: pip install requests beautifulsoup4 Pillow
Optional: ffmpeg (for [S] page video conversion)
"""

import os
import sys
import io
import time
import json
import struct
import re
import shutil
import subprocess
import threading
import queue
from datetime import datetime
from html import unescape
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk(); root.withdraw()
    messagebox.showerror("Missing Dependency",
        "The 'requests' library is required.\n\n"
        "Install with: pip install requests beautifulsoup4 Pillow")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk(); root.withdraw()
    messagebox.showerror("Missing Dependency",
        "The 'beautifulsoup4' library is required.\n\n"
        "Install with: pip install requests beautifulsoup4 Pillow")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk(); root.withdraw()
    messagebox.showerror("Missing Dependency",
        "Pillow is required.\n\nInstall with: pip install requests beautifulsoup4 Pillow")
    sys.exit(1)


import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ==============================================================================
# FROZEN-EXE (PyInstaller) SUPPORT
# ==============================================================================
# When this script is frozen with PyInstaller --onefile, __file__ points
# into a TEMPORARY extraction dir (sys._MEIPASS) that is deleted when the
# app exits. Read-only payloads added via --add-data live there, but
# anything we INSTALL at runtime (the ruffle build, the ffdec download)
# must persist. Tool lookup therefore searches, in order:
#   1. the directory containing the .exe  ("tools beside the exe" layout)
#   2. sys._MEIPASS                       (payload bundled with --add-data)
#   3. the directory of this .py file     (normal, unfrozen operation)
# Runtime installs always go to the .exe directory when frozen (writable
# and persistent), else next to this script.

def _is_frozen():
    """True when running from a PyInstaller-frozen executable."""
    return bool(getattr(sys, "frozen", False))

def _app_dirs():
    """Ordered list of directories to SEARCH for bundled tools."""
    dirs = []
    if _is_frozen():
        try:
            dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
        except Exception:
            pass
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(os.path.abspath(meipass))
    dirs.append(os.path.dirname(os.path.abspath(__file__)))
    out, seen = [], set()
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out

def _tool_install_dir():
    """Directory where runtime installs (ruffle build, ffdec download)
    are persisted - must be writable and survive a restart."""
    if _is_frozen():
        try:
            return os.path.dirname(os.path.abspath(sys.executable))
        except Exception:
            pass
    return os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

MIRROR_BASE = "https://mspa.chadthundercock.com"
MSPFA_BASE = "https://mspfa.com"
FLASH_MP4_BASE = "http://file.garden/aQ9-6gw2fD_KMuI9/mspa-3ds/"
FLASH_WAV_BASE = "http://file.garden/aQ9-6gw2fD_KMuI9/mspa-3ds/"
BUNDLE_SCHEMA = 5
REQUEST_TIMEOUT = 15
REQUEST_DELAY = 0.35

PANEL_MAX_W = 320
PANEL_MAX_H = 240
TEX_MAGIC = 0x58455435
ANIM_MAGIC = 0x53485432
GPU_RGBA8 = 0

# Known comics on the mspa.chadthundercock.com mirror
# slug = URL path segment, offset = global page number offset
# For Homestuck, internal page 1 = global page 1901
# Note: Bard Quest is excluded (branching narrative with multiple next-links)
# Note: Homestuck Beta is excluded (flash-only, no static images)
COMICS = {
    "jailbreak":       {"name": "Jailbreak",              "offset": 0},
    "problemsleuth":   {"name": "Problem Sleuth",         "offset": 0},
    "homestuck":       {"name": "Homestuck",              "offset": 1900},
}

# MSPFA stories use a virtual page numbering scheme:
#   virtual_page = story_id * 10000 + mspfa_page_num
# This ensures unique page numbers across multiple MSPFA packs
# and avoids collisions with Homestuck's 1901+ range.
MSPFA_PAGE_MULTIPLIER = 10000

def parse_comic_url(url_or_slug):
    """Parse a URL or slug to extract comic info.
    
    Accepts:
      - Full URL: https://mspa.chadthundercock.com/homestuck/1
      - Partial:  /homestuck/1
      - Just slug: homestuck
      
    Returns (comic_slug, comic_offset, start_page) or (None, 0, 1) if unrecognized.
    """
    if not url_or_slug:
        return None, 0, 1
    
    url_or_slug = url_or_slug.strip()
    if not url_or_slug:
        return None, 0, 1
    
    # Try to extract path from a full URL
    parsed = urlparse(url_or_slug)
    path = parsed.path or url_or_slug
    
    # Remove leading slash
    path = path.lstrip("/")
    
    # Pattern: <slug>/<page_num>
    m = re.match(r'([a-z0-9-]+)/(\d+)$', path)
    if m:
        slug = m.group(1)
        page = int(m.group(2))
        info = COMICS.get(slug)
        if info:
            return slug, info["offset"], page
        # Unknown slug but valid format — assume offset 0
        return slug, 0, page
    
    # Pattern: read/<story_id>/<page_num> (global numbering)
    m = re.match(r'read/\d+/(\d+)$', path)
    if m:
        global_page = int(m.group(1))
        # Try to figure out which comic this belongs to based on offset ranges
        # Homestuck: global 1901+  →  internal = global - 1900
        # Others: global = internal
        if global_page > 1900:
            return "homestuck", 1900, global_page - 1900
        return None, 0, global_page
    
    # Pattern: just a slug (no page number)
    if path in COMICS:
        info = COMICS[path]
        return path, info["offset"], 1
    
    # Check if it's a slug-like string (lowercase, no slashes)
    if re.match(r'^[a-z0-9-]+$', path):
        return path, 0, 1
    
    return None, 0, 1

def parse_mspfa_url(url_or_id):
    """Parse an MSPFA URL or story ID.
    
    Accepts:
      - Full URL: https://mspfa.com/?s=27317&p=1
      - Story ID only: 27317
      - URL with page: https://mspfa.com/?s=27317&p=5
    
    Returns (story_id, start_page) or (None, 1) if not an MSPFA URL.
    """
    if not url_or_id:
        return None, 1
    
    url_or_id = url_or_id.strip()
    if not url_or_id:
        return None, 1
    
    # Try as a plain integer (story ID)
    try:
        sid = int(url_or_id)
        if sid > 0:
            return sid, 1
    except ValueError:
        pass
    
    # Try as a URL
    parsed = urlparse(url_or_id)
    
    # If no scheme was provided, try adding https://
    if not parsed.scheme and "mspfa.com" in url_or_id:
        parsed = urlparse("https://" + url_or_id)
    
    # Check if it's an MSPFA URL
    host = (parsed.hostname or "").lower()
    if "mspfa.com" not in host:
        return None, 1
    
    # Extract s= and p= from query string
    query = parsed.query or ""
    fragment = parsed.fragment or ""
    full_query = query
    if fragment:
        full_query += "&" + fragment
    
    story_id = None
    page_num = 1
    
    for part in full_query.split("&"):
        if part.startswith("s="):
            try:
                story_id = int(part[2:])
            except ValueError:
                pass
        elif part.startswith("p="):
            try:
                page_num = int(part[2:])
            except ValueError:
                pass
    
    return story_id, page_num

# Detect external tools
HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_JAVA = shutil.which("java") is not None

def _find_yt_dlp():
    """Find yt-dlp executable."""
    path = shutil.which("yt-dlp")
    if path:
        return path
    # Next to the script / .exe (portable + PyInstaller layouts)
    exe = "yt-dlp.exe" if os.name == "nt" else "yt-dlp"
    for d in _app_dirs():
        cand = os.path.join(d, exe)
        if os.path.isfile(cand):
            return cand
    # Check common pip install locations
    candidates = [
        os.path.expanduser("~/.local/bin/yt-dlp"),
        "/usr/local/bin/yt-dlp",
        "/usr/bin/yt-dlp",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None

YT_DLP_PATH = _find_yt_dlp()
_YT_DLP_VERSION_CACHE = None  # filled by _yt_dlp_version()
HAS_YT_DLP = YT_DLP_PATH is not None

def _find_ffdec():
    """Find FFDec (JPEXS Free Flash Decompiler) installation.
    
    Looks for a directory containing ffdec.jar + lib/ subdirectory.
    Returns (jar_path, lib_dir) or (None, None).
    """
    env_path = os.environ.get("FFDEC_PATH")
    if env_path and os.path.isfile(env_path):
        env_dir = os.path.dirname(env_path)
        env_lib = os.path.join(env_dir, "lib")
        if os.path.isdir(env_lib):
            return env_path, env_lib
    
    search_dirs = []
    for d in _app_dirs():
        search_dirs.append(d)
        search_dirs.append(os.path.join(d, "ffdec"))
    search_dirs += [
        "/usr/share/ffdec",
        "/usr/local/share/ffdec",
        os.path.expanduser("~/ffdec"),
    ]
    
    for d in search_dirs:
        gui_path = os.path.join(d, "ffdec.jar")
        lib_dir = os.path.join(d, "lib")
        if os.path.isfile(gui_path) and os.path.isdir(lib_dir):
            return gui_path, lib_dir
    
    if shutil.which("ffdec"):
        return "ffdec", None
    
    return None, None

def _test_ffdec(jar_path):
    """Test if FFDec can actually run. Returns True if it works."""
    if not HAS_JAVA or not jar_path or jar_path == "ffdec":
        return bool(jar_path)
    try:
        result = subprocess.run(
            ["java", "-jar", jar_path, "-help"],
            capture_output=True, timeout=15,
            cwd=os.path.dirname(jar_path)
        )
        # FFDec -help returns 0 and prints usage to stdout
        return result.returncode == 0 and b"JPEXS" in result.stdout
    except Exception:
        return False

def _download_ffdec():
    """Download FFDec from GitHub releases and extract it.
    
    Downloads to <script_dir>/ffdec/ and returns (jar_path, lib_dir).
    Returns (None, None) on failure.
    """
    import zipfile, io
    
    version = "22.0.1"
    url = f"https://github.com/jindrapetrik/jpexs-decompiler/releases/download/version{version}/ffdec_{version}.zip"
    
    target_dir = os.path.join(_tool_install_dir(), "ffdec")
    
    try:
        resp = requests.get(url, timeout=120, stream=True)
        if resp.status_code != 200:
            return None, None
        
        # Read all content
        content = resp.content
        
        # Extract zip to target directory
        os.makedirs(target_dir, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            z.extractall(target_dir)
        
        jar_path = os.path.join(target_dir, "ffdec.jar")
        lib_dir = os.path.join(target_dir, "lib")
        
        if os.path.isfile(jar_path) and os.path.isdir(lib_dir):
            return jar_path, lib_dir
    except Exception:
        pass
    
    return None, None

def _ensure_ffdec():
    """Ensure FFDec is available and working.
    
    1. Try to find it locally
    2. Test if it actually works
    3. If not, download from GitHub
    4. Test again
    
    Returns (jar_path, lib_dir) or (None, None).
    """
    # Try local first
    jar, lib = _find_ffdec()
    if jar and jar != "ffdec":
        if _test_ffdec(jar):
            return jar, lib
    
    # Download from GitHub
    jar, lib = _download_ffdec()
    if jar and _test_ffdec(jar):
        return jar, lib
    
    return None, None

FFDEC_JAR, FFDEC_LIB = _ensure_ffdec()
HAS_FFDEC = HAS_JAVA and FFDEC_JAR is not None


# ═══════════════════════════════════════════════════════════════════════════════
# RUFFLE EXPORTER (primary SWF renderer — modern replacement for FFDec)
# ═══════════════════════════════════════════════════════════════════════════════
# FFDec's Java renderer chokes on long flashes (e.g. [S] Make her pay:
# 5558 frames @ 25fps → monolithic run takes >10 min, hits the timeout,
# accumulates memory and gets slower over time). Ruffle's exporter is a
# headless wgpu renderer with correct AVM1/AVM2 emulation that renders the
# same movie in ~20 seconds.
#
# No official prebuilt exporter binaries are published, so we look for one
# next to the script (ruffle-exporter/), on PATH, or via $RUFFLE_EXPORTER_PATH.
# It can be built from source automatically (needs cargo) — see _build_ruffle.

RUFFLE_DIR_NAME = "ruffle-exporter"           # subdir next to this script
RUFFLE_EXE = "ruffle_exporter.exe" if os.name == "nt" else "ruffle_exporter"
# Patched exporter sources (bz2+base64), applied by _build_ruffle().
# Based on ruffle nightly-2026-09-08 exporter crate with streaming writes,
# --frame-indices(-file) support and the single-frame output fix.
RUFFLE_PATCHED_SOURCES = {
    "cli.rs": (
        "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" \
        "w26sYb0zhxPru6+uH/F3JFOFCQuKyPlQ"
    ),
    "lib.rs": (
        "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" \
        "9OPvjjnfxdyRThQkKv//m4A="
    ),
}

RUFFLE_SOURCE_URL = os.environ.get(
    "RUFFLE_SOURCE_URL",
    "https://github.com/ruffle-rs/ruffle/releases/download/nightly-2026-09-08/"
    "ruffle-nightly-2026_09_08-reproducible-source.zip")
RUFFLE_AUTO_BUILD = os.environ.get("RUFFLE_AUTO_BUILD", "") not in ("", "0")


def parse_swf_header(path):
    """Parse a SWF header in pure Python.

    Returns dict {version, width, height, frame_rate, frame_count} or None.
    Handles uncompressed (FWS), zlib (CWS) and LZMA (ZWS) SWFs.
    """
    import zlib as _zlib
    try:
        with open(path, "rb") as f:
            sig = f.read(3)
            if sig not in (b"FWS", b"CWS", b"ZWS"):
                return None
            version = f.read(1)[0]
            f.read(4)  # file length (uncompressed)
            rest = f.read()

        if sig == b"CWS":
            rest = _zlib.decompress(rest)
        elif sig == b"ZWS":
            # ZWS: 4-byte compressed length, 5-byte LZMA props, 4-byte
            # uncompressed length, then the raw LZMA1 stream.
            try:
                import lzma as _lzma
                props = rest[4:9]
                filt = _lzma._decode_filter_properties(_lzma.FILTER_LZMA1, props)
                dec = _lzma.LZMADecompressor(format=_lzma.FORMAT_RAW, filters=[filt])
                rest = dec.decompress(rest[13:])
            except Exception:
                return None

        if len(rest) < 9:
            return None

        # RECT: 5-bit nbits, then 4 signed values of nbits bits (twips)
        data, bitpos = rest, 0

        def _bits(n):
            nonlocal bitpos
            val = 0
            for _ in range(n):
                byte = data[bitpos >> 3]
                val = (val << 1) | ((byte >> (7 - (bitpos & 7))) & 1)
                bitpos += 1
            return val

        nbits = _bits(5)
        xmin = _bits(nbits); xmax = _bits(nbits)
        ymin = _bits(nbits); ymax = _bits(nbits)

        consumed = (bitpos + 7) >> 3
        frame_rate_fixed, frame_count = struct.unpack_from("<HH", rest, consumed)

        return {
            "version": version,
            "width": (xmax - xmin) // 20,
            "height": (ymax - ymin) // 20,
            "frame_rate": frame_rate_fixed >> 8,   # fixed 8.8
            "frame_count": frame_count,
        }
    except Exception:
        return None


def compute_decimated_frames(swf_fps, total_frames, target_fps):
    """Pick source-frame indices that sample a movie at an exact target FPS.

    Returns (indices, delay_ms):
      indices — ordered source frame indices, one per output frame
                (duplicates possible when target_fps > swf_fps — that keeps
                playback duration correct by holding frames)
      delay_ms — uniform display duration of each output frame
    """
    delay_ms = max(1, int(round(1000.0 / target_fps)))
    if not swf_fps or swf_fps < 1 or not total_frames or total_frames < 1:
        return [0], delay_ms
    duration_s = total_frames / float(swf_fps)
    n_out = int(duration_s * target_fps)
    if n_out < 1:
        return [0], delay_ms
    indices = []
    for j in range(n_out):
        idx = min(round(j * swf_fps / target_fps), total_frames - 1)
        indices.append(max(0, idx))
    return indices, delay_ms


def _make_test_swf(path):
    """Write a minimal valid 1-frame white SWF (for smoke-testing renderers)."""
    # RECT 320x240 (twips 6400x4800): nbits=15, all values fit
    rect_bits = "01111" + format(0, "015b") + format(6400, "015b") \
                + format(0, "015b") + format(4800, "015b")
    rect_bits += "0" * ((-len(rect_bits)) % 8)
    rect = bytes(int(rect_bits[i:i + 8], 2) for i in range(0, len(rect_bits), 8))
    body = rect + struct.pack("<HH", 30 << 8, 1)  # frame rate 30.0, 1 frame
    body += struct.pack("<H", (9 << 6) | 3) + b"\xff\xff\xff"  # SetBackgroundColor white
    body += struct.pack("<H", (1 << 6) | 0)  # ShowFrame
    body += struct.pack("<H", (0 << 6) | 0)  # End
    with open(path, "wb") as f:
        f.write(b"FWS" + bytes([6]) + struct.pack("<I", 8 + len(body)) + body)


def _find_ruffle():
    """Locate the ruffle_exporter binary. Returns path or None."""
    env_path = os.environ.get("RUFFLE_EXPORTER_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    candidates = []
    for d in _app_dirs():
        candidates.append(os.path.join(d, RUFFLE_DIR_NAME, RUFFLE_EXE))
        candidates.append(os.path.join(d, RUFFLE_EXE))
    for c in candidates:
        if os.path.isfile(c):
            return c

    return shutil.which("ruffle_exporter")


def _test_ruffle(path):
    """Render the built-in test SWF to verify wgpu works headlessly.

    Tries graphics backends in order and returns the working backend's CLI
    args (e.g. [] for default, ["-g", "gl"]), or None if all fail.
    """
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="mspa3ds_ruffle_test_")
    try:
        swf = os.path.join(tmpdir, "test.swf")
        out = os.path.join(tmpdir, "out")
        _make_test_swf(swf)

        for backend in (None, "vulkan", "gl"):
            cmd = [path]
            if backend:
                cmd += ["-g", backend]
            cmd += ["--frames", "1", "--silent", swf, out]
            try:
                env = dict(os.environ)
                # Let software renderers work headless on Linux
                if os.name != "nt":
                    env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
                result = subprocess.run(cmd, capture_output=True, timeout=90,
                                        env=env)
                png = os.path.join(out, "0.png")
                if result.returncode == 0 and os.path.isfile(png) \
                        and os.path.getsize(png) > 100:
                    return ([] if backend is None else ["-g", backend])
            except Exception:
                continue
        return None
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def _build_ruffle(log=print):
    """Build the ruffle exporter from source with cargo (one-time, ~5-10 min).

    Downloads the pinned source zip, builds just the exporter crate, and
    installs the binary to <script_dir>/ruffle-exporter/.
    Returns the binary path or None.
    """
    import zipfile, tempfile
    cargo = shutil.which("cargo")
    if not cargo:
        # common rustup location
        cargo = os.path.expanduser("~/.cargo/bin/cargo")
        if not os.path.isfile(cargo):
            log("[Ruffle] cargo not found — cannot build from source "
                "(install Rust from https://rustup.rs and re-run)")
            return None

    target_dir = os.path.join(_tool_install_dir(), RUFFLE_DIR_NAME)

    workdir = tempfile.mkdtemp(prefix="mspa3ds_ruffle_build_")
    try:
        log("[Ruffle] downloading source...")
        try:
            resp = requests.get(RUFFLE_SOURCE_URL, timeout=300)
            if resp.status_code != 200:
                log(f"[Ruffle] download failed: HTTP {resp.status_code}")
                return None
        except Exception as e:
            log(f"[Ruffle] download failed: {e}")
            return None

        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            z.extractall(workdir)
        # The reproducible-source zip may have either a single root folder or
        # several workspace members at the top level — handle both.
        src_root = None
        if os.path.isfile(os.path.join(workdir, "exporter", "Cargo.toml")):
            src_root = workdir
        else:
            for d in sorted(os.listdir(workdir)):
                cand = os.path.join(workdir, d)
                if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "exporter", "Cargo.toml")):
                    src_root = cand
                    break
        if src_root is None:
            log("[Ruffle] source archive does not contain the exporter crate")
            return None

        # Apply the MSPA-3DS exporter patches (streaming writes + frame
        # indices) — the pinned source matches, so full-file overwrite is safe.
        import bz2 as _bz2, base64 as _b64
        for fname, packed in RUFFLE_PATCHED_SOURCES.items():
            patched = _bz2.decompress(_b64.b64decode(packed))
            dest = os.path.join(src_root, "exporter", "src", fname)
            with open(dest, "wb") as f:
                f.write(patched)
        log("[Ruffle] applied exporter patches (streaming + frame indices)")

        log("[Ruffle] building exporter with cargo (this takes a few minutes)...")
        try:
            result = subprocess.run(
                [cargo, "build", "--release", "-p", "exporter"],
                cwd=src_root, capture_output=True, timeout=1800)
            if result.returncode != 0:
                tail = result.stderr.decode(errors="replace")[-400:]
                log(f"[Ruffle] cargo build failed:\n{tail}")
                return None
        except Exception as e:
            log(f"[Ruffle] cargo build failed: {e}")
            return None

        exe = "exporter.exe" if os.name == "nt" else "exporter"
        built = os.path.join(src_root, "target", "release", exe)
        if not os.path.isfile(built):
            log("[Ruffle] build succeeded but binary not found")
            return None

        os.makedirs(target_dir, exist_ok=True)
        dest = os.path.join(target_dir, RUFFLE_EXE)
        shutil.copy2(built, dest)
        if os.name != "nt":
            try:
                os.chmod(dest, 0o755)
            except Exception:
                pass
        log(f"[Ruffle] installed to {dest}")
        return dest
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _ensure_ruffle():
    """Find a working ruffle_exporter, building from source if allowed.

    Returns (path_or_None, graphics_args_list).
    Auto-build happens when RUFFLE_AUTO_BUILD=1 is set, or when neither
    Ruffle nor FFDec is available (so the tool remains self-sufficient).
    """
    path = _find_ruffle()
    if path:
        backend = _test_ruffle(path)
        if backend is not None:
            return path, backend

    if RUFFLE_AUTO_BUILD or (path is None and not HAS_FFDEC):
        path = _build_ruffle()
        if path:
            backend = _test_ruffle(path)
            if backend is not None:
                return path, backend

    return None, []


RUFFLE_EXPORTER, RUFFLE_GRAPHICS_ARGS = _ensure_ruffle()
HAS_RUFFLE = RUFFLE_EXPORTER is not None


# ═══════════════════════════════════════════════════════════════════════════════
# HTML PARSING
# ═══════════════════════════════════════════════════════════════════════════════

def html_decode(text):
    if not text:
        return ""
    text = re.sub(r"<br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()

def make_absolute_url(src):
    if not src:
        return ""
    if src.startswith("http://") or src.startswith("https://"):
        if "homestuck.com" in src or "storage.homestuck.com" in src or "mspaintadventures.com" in src:
            try:
                return MIRROR_BASE + urlparse(src).path
            except Exception:
                return src
        return src
    return MIRROR_BASE + ("/" if not src.startswith("/") else "") + src

def looks_like_flash(html):
    if re.search(r"<embed\b", html, re.IGNORECASE): return True
    if re.search(r"<iframe\b", html, re.IGNORECASE): return True
    if "application/x-shockwave-flash" in html.lower(): return True
    if re.search(r'(?:src|href)\s*=\s*["\'][^"\']*\.swf["\']', html, re.IGNORECASE): return True
    return False

def find_swf_urls(html):
    """Extract SWF URLs from the page HTML.
    
    Looks in <object data="...">, <embed src="...">, <iframe src="...">,
    and <param name="movie" value="..."> tags for flash content.
    
    Detects both explicit .swf URLs and flash embeds without .swf extension
    (e.g. <embed type="application/x-shockwave-flash" src="/mspa/loader">).
    
    Converts relative and old-domain URLs to the mirror's domain.
    
    Returns a list of absolute SWF URLs (may be empty).
    """
    urls = []
    soup = BeautifulSoup(html, "html.parser")
    
    for tag in soup.find_all("object", data=True):
        data = tag.get("data", "")
        if data and (".swf" in data.lower() or "flash" in tag.get("classid", "").lower()):
            abs_url = make_absolute_url(data)
            if abs_url:
                urls.append(abs_url)
    
    # Check <param name="movie"> inside <object> tags
    for tag in soup.find_all("param", attrs={"name": "movie"}):
        val = tag.get("value", "")
        if val:
            abs_url = make_absolute_url(val)
            if abs_url and abs_url not in urls:
                urls.append(abs_url)
    
    for tag in soup.find_all("embed", src=True):
        src = tag.get("src", "")
        is_flash = (
            ".swf" in src.lower() or
            tag.get("type", "").lower() == "application/x-shockwave-flash"
        )
        if src and is_flash:
            abs_url = make_absolute_url(src)
            if abs_url and abs_url not in urls:
                urls.append(abs_url)
    
    for tag in soup.find_all("iframe", src=True):
        src = tag.get("src", "")
        if src and ".swf" in src.lower():
            abs_url = make_absolute_url(src)
            if abs_url and abs_url not in urls:
                urls.append(abs_url)
    
    # Also check for .swf URLs in any other param tags
    for tag in soup.find_all("param", value=True):
        if tag.get("name", "").lower() == "movie":
            continue  # Already handled above
        val = tag.get("value", "")
        if ".swf" in val.lower():
            abs_url = make_absolute_url(val)
            if abs_url and abs_url not in urls:
                urls.append(abs_url)
    
    # Deduplicate while preserving order
    # Also ensure all flash URLs end with .swf (some embeds omit the extension)
    seen = set()
    unique = []
    for u in urls:
        # Append .swf if the URL doesn't already have it
        if not u.lower().endswith(".swf"):
            u = u + ".swf"
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique

def extract_title(soup):
    tag = soup.find("h2", id="title")
    return html_decode(tag.get_text()) if tag else ""

def extract_command_and_next(soup, comic_slug, comic_offset):
    """Extract command text and next page number from the page's 'next' link.
    
    comic_slug: e.g. 'homestuck', 'jailbreak', 'problemsleuth'
    comic_offset: global page offset (1900 for Homestuck, 0 for others)
    Returns (command_text, next_internal_page) where internal pages are 1-based.
    """
    cmd_div = soup.find("div", class_="commands")
    if not cmd_div: return "", 0
    link = cmd_div.find("a", href=True)
    if not link: return "", 0
    command = html_decode(link.get_text())
    href = link["href"]

    # Pattern 1: /<comic_slug>/<N> (e.g. /homestuck/2, /jailbreak/5)
    m = re.search(rf'/{re.escape(comic_slug)}/(\d+)/?$', href)
    if m:
        return command, int(m.group(1))

    # Pattern 2: /read/<story_id>/<N> (global numbering)
    m = re.search(r'/read/\d+/(\d+)/?$', href)
    if m:
        n = int(m.group(1))
        return command, max(1, n - comic_offset)

    # Pattern 3: just /<N> at the end
    m = re.search(r'/(\d+)/?$', href)
    if m:
        n = int(m.group(1))
        return command, max(1, n - comic_offset) if comic_offset else n

    return command, 0

ALLOWED_EXTENSIONS = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mpg", ".mpeg", ".swf"}

# Known media extension appearing inside a filename — not necessarily at the
# end: MSPFA CDN files sometimes look like "13(real.gif)", where the raw
# splitext() extension is ".gif)" (issue #6: such images were dropped).
MEDIA_EXT_RE = re.compile(r'\.(gif|png|jpeg|jpg|webp|mp4|mpg|mpeg|swf)(?![a-z0-9])', re.IGNORECASE)

def is_media_url_allowed(url):
    try:
        path = urlparse(url).path.lower()
        _, ext = os.path.splitext(path)
        return ext in ALLOWED_EXTENSIONS
    except Exception:
        return False

def mspfa_media_url_ok(url):
    """Media filter for MSPFA [img]-style URLs (looser than the strict
    extension check used for scraped HTML):
      - strict extension at the end of the path → OK
      - extension-less filename → OK ([img] content is an image by definition)
      - known extension inside the filename (e.g. "13(real.gif)") → OK
    """
    if is_media_url_allowed(url):
        return True
    try:
        fname = urlparse(url).path.rsplit("/", 1)[-1]
    except Exception:
        return False
    if "." not in fname:
        return True
    return bool(MEDIA_EXT_RE.search(fname))

def mspfa_media_ext(url):
    """Clean local file extension for an MSPFA media URL — always one of
    ALLOWED_EXTENSIONS (fallback .gif; PIL detects the real format anyway)."""
    ext = _get_ext(url).lower()
    if ext in ALLOWED_EXTENSIONS:
        return ext
    m = MEDIA_EXT_RE.search(url)
    if m:
        e = "." + m.group(1).lower()
        return ".jpg" if e == ".jpeg" else e
    return ".gif"

def extract_media_urls(soup, global_page, html, comic_slug):
    if looks_like_flash(html):
        # [S] page: try to find SWF URL in the HTML first
        swf_urls = find_swf_urls(html)
        if swf_urls and (HAS_RUFFLE or HAS_FFDEC):
            return swf_urls[:1], True  # Use the first SWF found
        # Fall back to pre-converted MP4 from archive
        return [f"{FLASH_MP4_BASE}{global_page:06d}.mp4"], True
    media_div = soup.find("div", id="media")
    if not media_div: return [], False
    urls = []
    for tag in media_div.find_all(["img", "embed", "source", "iframe"], src=True):
        src = tag.get("src", "")
        if not src: continue
        abs_url = make_absolute_url(src)
        if abs_url and is_media_url_allowed(abs_url): urls.append(abs_url)
    for tag in media_div.find_all("object", data=True):
        data = tag.get("data", "")
        if not data: continue
        abs_url = make_absolute_url(data)
        if abs_url and is_media_url_allowed(abs_url): urls.append(abs_url)
    return urls, False

def extract_texts(soup):
    content_div = soup.find("div", id="content")
    if not content_div: return []
    return [html_decode(str(p)) for p in content_div.find_all("p") if html_decode(str(p))]

def _get_ext(url):
    try:
        _, ext = os.path.splitext(urlparse(url).path)
        return ext
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# MSPFA JSON API
# ═══════════════════════════════════════════════════════════════════════════════
# MSPFA is a JavaScript SPA — the HTML has empty divs; all content is loaded
# via a POST API that returns the entire story as JSON.
#
# API: POST https://mspfa.com/
#   Content-Type: application/x-www-form-urlencoded
#   Accept: application/json
#   Body: do=story&s=STORY_ID
#
# Response JSON structure:
#   {
#     "i": 65860,          // story ID
#     "n": "Story Name",   // story title
#     "y": "...css...",     // custom CSS (contains @mspfa audio directives)
#     "v": "...js...",      // custom JS code
#     "p": [               // array of pages (0-indexed, page 1 = p[0])
#       {
#         "c": "Command",   // command text
#         "b": "[img]url[/img]\nBody text",  // BBCode body
#         "n": [2],         // array of next page numbers
#         "d": 1761517699   // timestamp
#       },
#       ...
#     ]
#   }
#
# BBCode tags used in body:
#   [img]URL[/img]       → image
#   [b]...[/b]           → bold
#   [i]...[/i]           → italic
#   [color=#hex]...[/color] → colored text
#   [size=N]...[/size]   → font size
#   [url=LINK]...[/url]  → hyperlink
#   [spoiler]...[/spoiler] → spoiler block
#   [flash]URL[/flash]   → flash/SWF embed
#   [user]NAME[/user]    → user mention
#   [s]...[/s]           → strikethrough

def mspfa_fetch_story(session, story_id):
    """Fetch a complete MSPFA story via the JSON API.
    
    Returns the story dict (with keys i, n, p, y, v, ...) or None on failure.
    """
    try:
        resp = session.post(
            MSPFA_BASE + "/",
            data={"do": "story", "s": str(story_id)},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("p"):
            return None
        return data
    except Exception:
        return None


def mspfa_parse_images(body):
    """Extract media URLs from MSPFA body text.
    
    MSPFA bodies contain a mix of BBCode tags and raw HTML:
      - [img]URL[/img]                     → image (GIF/PNG/JPEG)
      - [img=650x489]URL[/img]             → image with dimensions
      - [img=650x489 swap=OTHER]URL[/img]  → image with click-swap variant
      - [img swap=OTHER]URL[/img]          → click-swap only
      - [flash]URL[/flash]                 → SWF animation
      - [flash=WxH]URL[/flash]             → SWF with dimensions
      - <iframe src="...youtube/embed/ID"> → YouTube video
      - <video src="URL">                  → direct video (MP4/WebM)
      - <video><source src="URL"></video>  → direct video with source tag
    
    Media priority is decided by ORDER OF APPEARANCE in the body: the first
    embedded media is the page's primary media (e.g. the Crow Strider AU cover
    page has a cover [img] followed by a YouTube trailer <iframe> — the cover
    is the primary media, so the page stays an image page). If the first media
    is an image, ALL images are returned (multi-image pages get split into
    sub-pages); otherwise the single video/flash media is returned.
    
    Returns (urls, media_type) where media_type is one of:
      'image'  — static image, use convert_gif_to_tex
      'swf'    — Flash animation, use convert_swf_to_frames (Ruffle/FFDec)
      'video'  — direct video file, use convert_mp4_to_frames (ffmpeg)
      'youtube' — YouTube video, use convert_youtube_to_frames (yt-dlp)
    """
    # [img] tags — the attribute part accepts any shape: none, "=WxH",
    # "=WxH swap=U" or " swap=U" (issue #6: the old pattern only matched
    # "[img]" and "[img=WxH]", so pages using swap= lost EVERY image).
    img_re = re.compile(r'\[img(?:[=\s][^\]]*)?\](.+?)\[/img\]', re.IGNORECASE)
    img_matches = [m for m in img_re.finditer(body) if m.group(1).strip()]

    # Raw HTML <img src="..."> tags — some adventures embed images via raw
    # HTML instead of BBCode (previously ignored: those pages ended up
    # text-only). Filtered like [img] URLs; data: URIs are skipped.
    img_html_matches = []
    for m in re.finditer(r'<img\b[^>]*?\bsrc=["\']([^"\']+)["\']', body,
                         re.IGNORECASE):
        u = (m.group(1) or "").strip()
        if u and not u.lower().startswith("data:") and mspfa_media_url_ok(u):
            img_html_matches.append(m)
    img_matches = [m for m in img_matches
                   if mspfa_media_url_ok((m.group(1) or "").strip())]
    
    # [flash] tags (SWF) — same attribute shapes
    flash_re = re.compile(r'\[flash(?:[=\s][^\]]*)?\](.+?)\[/flash\]', re.IGNORECASE)
    flash_matches = [m for m in flash_re.finditer(body) if m.group(1).strip()]
    
    # <iframe> YouTube embeds
    # MSPFA users embed YouTube via: <iframe src="https://www.youtube.com/embed/VIDEO_ID">
    yt_re = re.compile(
        r'<iframe[^>]+src=["\'](?:https?://)?(?:www\.)?youtube\.com/embed/([\w-]{11})',
        re.IGNORECASE
    )
    yt_matches = list(yt_re.finditer(body))
    if not yt_matches:
        # Also check youtu.be short URLs
        yt_matches = list(re.finditer(
            r'<iframe[^>]+src=["\'](?:https?://)?youtu\.be/([\w-]{11})',
            body, re.IGNORECASE
        ))
    
    # <video> tags with direct video URLs
    # Pattern: <video><source src="URL"> (checked first) or <video src="URL">
    vid_matches = list(re.finditer(
        r'<video[^>]*>.*?<source[^>]+src=["\']([^"\']+)["\']',
        body, re.IGNORECASE | re.DOTALL
    ))
    if not vid_matches:
        vid_matches = list(re.finditer(
            r'<video[^>]+src=["\']([^"\']+)["\']',
            body, re.IGNORECASE
        ))
    
    def _first_pos(matches):
        return matches[0].start() if matches else float('inf')
    
    # Earliest media in the body wins (see docstring). Images are a special
    # case: when the first image precedes any video, the page is an image
    # page and ALL images are kept (the reader cycles them with A/B).
    all_img_matches = img_matches + img_html_matches
    if all_img_matches and _first_pos(all_img_matches) < min(
            _first_pos(flash_matches), _first_pos(yt_matches), _first_pos(vid_matches)):
        urls = []
        for m in all_img_matches:
            u = (m.group(1) or "").strip()
            if u and u not in urls:
                urls.append(u)
        return urls, 'image'
    
    if flash_matches:
        return [flash_matches[0].group(1).strip()], 'swf'
    if yt_matches:
        return [yt_matches[0].group(1)], 'youtube'
    if vid_matches:
        return [vid_matches[0].group(1).strip()], 'video'
    
    # No usable media at all — collect whatever images survived the filter
    urls = []
    for m in img_matches + img_html_matches:
        u = (m.group(1) or "").strip()
        if u and u not in urls:
            urls.append(u)
    return urls, 'image'


def mspfa_parse_text(body):
    """Extract plain text from MSPFA BBCode body text.
    
    Strips all BBCode tags AND raw HTML/CSS/JS widget code, and returns a
    list of non-empty lines.

    Some adventures embed functional code in page bodies that mspfa.com
    renders as widgets but which leaked into the reader's text lines
    (reported: "[Open:Show Dialoguelog,Close:Close Dialoglog] Feferi: blah
    blah [color]" — it takes forever to find the actual dialog). Handled:
      - <script>/<style> blocks and HTML comments — removed ENTIRELY
        (their contents are code, not text)
      - raw HTML tags (<div>, <span>, <table>…) — stripped; <br> and
        block-level closing tags become line breaks
      - dialoguelog toggle headers like [Open:…,Close:…] (custom
        pseudo-BBCode some stories use for collapsible pesterlogs)
      - regular BBCode tags (as before), now also tolerating spaces
        around the "=" in attribute forms and the bare [url]…[/url] form
    """
    text = body
    # Raw HTML/CSS/JS functional code — remove whole blocks FIRST so their
    # contents (CSS rules, JS source) don't leak as text lines. mspfa.com
    # renders these as widgets; the reader only wants the visible words.
    text = re.sub(r'<script\b[^>]*>.*?</script\s*>', '', text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<style\b[^>]*>.*?</style\s*>', '', text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    # <br> → newline; block-level closers → newline; other HTML tags → gone.
    # The generic tag pattern requires a letter right after "<" (or "</"),
    # so plain-text arrows like "==>" / "<==" / "<3" are never touched.
    text = re.sub(r'<br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</\s*(?:p|div|li|tr|td|table|blockquote|h[1-6])\s*>',
                  '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?\s*[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?/?>', '', text)
    # Remove [img]...[/img] entirely (images aren't text) — including the
    # attribute forms [img=WxH] and [img swap=URL] (issue #6: the old
    # pattern left the raw image URL in the text lines)
    text = re.sub(r'\[img(?:[=\s][^\]]*)?\].+?\[/img\]', '', text, flags=re.IGNORECASE)
    # Remove [flash]...[/flash] entirely (same attribute forms)
    text = re.sub(r'\[flash(?:[=\s][^\]]*)?\].+?\[/flash\]', '', text, flags=re.IGNORECASE)
    # Remove [url=...]...[/url] — keep the link text only (may be empty)
    text = re.sub(r'\[url=[^\]]*\](.*?)\[/url\]', r'\1', text, flags=re.IGNORECASE)
    # Dialoguelog toggle headers — custom pseudo-BBCode widget labels used
    # by some stories, e.g. [Open:Show Dialoguelog,Close:Close Dialoglog]
    # (labels may be quoted). These never render as text on mspfa.com.
    text = re.sub(
        r'\[\s*Open\s*:\s*(?:"[^"]*"|[^,\]]*?)\s*,\s*Close\s*:\s*(?:"[^"]*"|[^\]]*?)\s*\]',
        '', text, flags=re.IGNORECASE)
    # Remove all other BBCode tags. Note: opening tags may carry attributes
    # (e.g. [spoiler open="x" close="y"]) and closing tags carry none
    # (e.g. [/size]), so each alternative allows an optional attribute tail
    # (with tolerance for stray spaces around the "=").
    text = re.sub(
        r'\[/?\s*'
        r'(?:b|i|u|s|'
        r'size\s*(?:=[^\]]*)?|'
        r'color\s*(?:=[^\]]*?)?|'
        r'spoiler(?:[=\s][^\]]*)?|'
        r'alt\s*(?:=[^\]]*)?|'
        r'user\s*(?:=[^\]]*)?|'
        r'background\s*(?:=[^\]]*?)?|'
        r'font\s*(?:=[^\]]*?)?|'
        r'url\s*(?:=[^\]]*)?|'
        r'log\s*(?:=[^\]]*)?|'
        r'left|center|right|justify)'
        r'\s*\]',
        '', text, flags=re.IGNORECASE)
    # Clean up
    text = unescape(text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return lines


def mspfa_find_audio(css_text, page_num):
    """Find the audio URL for a given MSPFA page number from @mspfa audio CSS directives.
    
    CSS directive format: @mspfa audio START END URL;
    Where START and END are 1-based page numbers and URL is the audio file.
    """
    if not css_text:
        return ""
    for m in re.finditer(r'@mspfa\s+audio\s+(\d+)\s+(\d+)\s+(\S+?)\s*;', css_text):
        start_p = int(m.group(1))
        end_p = int(m.group(2))
        url = m.group(3).strip()
        if start_p <= page_num <= end_p:
            if url.startswith("http://") or url.startswith("https://"):
                return url
            else:
                return MSPFA_BASE + "/" + url.lstrip("/")
    return ""


def is_mirror_404(html):
    """Detect the mspa.chadthundercock.com mirror's 404 pages.

    The mirror serves its "404 Not Found" page with HTTP 200, so a plain
    status check can't catch it. Such pages must be skipped: they would
    otherwise be written into the bundle as broken pages AND break the
    next-link scan (a 404 page has no commands div, so the scan stopped
    early and lost every page after the hole — e.g. Homestuck page 78).
    """
    if not html:
        return True
    m = re.search(r'<h2[^>]*id="title"[^>]*>(.*?)</h2>', html, re.DOTALL)
    if m:
        t = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        if t in ("404 Not Found", "404", "Not Found", "Page not found"):
            return True
    return False


def _file_is_swf(path):
    """True if the file starts with an SWF magic (FWS/CWS/ZWS).

    Used to route flash downloads by CONTENT instead of by URL extension —
    some mirror flash URLs are extensionless (AC_RunActiveContent JS
    embeds) while some "video" URLs actually serve SWFs.
    """
    try:
        with open(path, "rb") as f:
            return f.read(3) in (b"FWS", b"CWS", b"ZWS")
    except Exception:
        return False


# The 3DS reader loads the whole decoded WAV into its linear heap, which is
# limited — keep audio files under this size (long tracks get downsampled).
AUDIO_MAX_BYTES = 24 * 1024 * 1024


def _wav_is_3ds_compatible(path):
    """True if the file is a RIFF/WAVE PCM 16- or 8-bit mono/stereo WAV —
    the only audio format the 3DS reader (mspa_audio.c) can play."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(40)
        if len(hdr) < 36 or hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
            return False
        if hdr[12:16] != b"fmt ":
            return False  # unexpected chunk order — let ffmpeg normalize
        fmt = struct.unpack("<H", hdr[20:22])[0]
        ch = struct.unpack("<H", hdr[22:24])[0]
        bits = struct.unpack("<H", hdr[34:36])[0]
        return fmt == 1 and bits in (8, 16) and ch in (1, 2)
    except Exception:
        return False


def _normalize_audio_wav(path):
    """Make the audio file at `path` playable on the 3DS reader.

    - converts any input (MP3/OGG/float-WAV/…) to 16-bit PCM stereo WAV
    - downsamples (44100 stereo → 22050 stereo → 22050 mono) if the file
      is too big for the 3DS linear heap
    Returns True if `path` holds a playable WAV afterwards.
    """
    if not path or not os.path.isfile(path) or os.path.getsize(path) < 200:
        return False
    if _wav_is_3ds_compatible(path) and os.path.getsize(path) <= AUDIO_MAX_BYTES:
        return True
    if not HAS_FFMPEG:
        # Can't convert — only a source that is already compatible works
        return _wav_is_3ds_compatible(path)
    for ar, ac in ((44100, 2), (22050, 2), (22050, 1)):
        tmp = path + ".norm.wav"
        result = None
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-vn",
                 "-acodec", "pcm_s16le", "-ar", str(ar), "-ac", str(ac), tmp],
                capture_output=True, timeout=180)
        except Exception:
            result = None
        if result is not None and result.returncode == 0 and \
                os.path.isfile(tmp) and os.path.getsize(tmp) > 200:
            if os.path.getsize(tmp) <= AUDIO_MAX_BYTES or (ar, ac) == (22050, 1):
                os.replace(tmp, path)
                return True
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
    return _wav_is_3ds_compatible(path) and os.path.getsize(path) <= AUDIO_MAX_BYTES


# ═══════════════════════════════════════════════════════════════════════════════
# 3DS TEXTURE CONVERSION
# ═══════════════════════════════════════════════════════════════════════════════

def next_pow2(v):
    p = 64
    while p < v: p <<= 1
    return p

# Magic for raw (untiled) RGBA .tex files.
# The 3DS loader checks this format value and does GPU tiling via
# upload_rgba, which is proven to work correctly.
# Format 0x80 = "untiled RGBA, needs GPU transfer"
# Format 0x00 = GPU_RGBA8 (tiled, direct load — produced by the 3DS itself)
TEX_FMT_RAW_RGBA = 0x80

def write_tex_file(path, rgba_bytes, w, h):
    """
    Write a .tex file with UNTILED RGBA pixel data.
    
    Format: same header as GPU-tiled .tex, but format=0x80 and data is
    raw RGBA (R,G,B,A) row-major pixels, tightly packed (w stride, NOT
    padded to power-of-2). The 3DS upload_rgba() function reads with
    w stride and handles the power-of-2 padding internally.
    
    The 3DS reads this, does the GPU transfer (which handles tiling
    and ABGR swizzle automatically), then re-saves as format=0 for caching.
    
    This avoids trying to replicate the PICA200 GPU's morton tiling
    in Python, which is error-prone and hardware-specific.
    """
    tex_w = next_pow2(w)
    tex_h = next_pow2(h)

    # Write TIGHTLY PACKED RGBA data — no power-of-2 padding.
    # upload_rgba on the 3DS reads with stride w*4 and handles
    # padding to texW internally. If we pad here, the strides
    # mismatch and every row after the first reads from the wrong
    # offset, resulting in garbled textures.
    data_size = w * h * 4

    with open(path, "wb") as f:
        f.write(struct.pack("<IIIIIII",
            TEX_MAGIC, w, h, tex_w, tex_h, TEX_FMT_RAW_RGBA, data_size))
        f.write(rgba_bytes)

def write_anim_file(path, frame_count, delays_ms):
    with open(path, "wb") as f:
        f.write(struct.pack("<III", ANIM_MAGIC, 2, frame_count))
        for d in delays_ms:
            f.write(struct.pack("<I", d))

def convert_gif_to_tex(gif_path, output_base, is_video=False):
    if is_video:
        return 0, []
    try:
        img = Image.open(gif_path)
    except Exception:
        return 0, []
    n_frames = getattr(img, "n_frames", 1)
    if n_frames <= 1:
        try:
            img.seek(0)
            rgba = img.convert("RGBA")
            w, h = rgba.size
            dw, dh = w, h
            if dw > PANEL_MAX_W: dh = dh * PANEL_MAX_W // dw; dw = PANEL_MAX_W
            if dh > PANEL_MAX_H: dw = dw * PANEL_MAX_H // dh; dh = PANEL_MAX_H
            if dw > 0 and dh > 0 and dw <= 1024 and dh <= 1024:
                if dw != w or dh != h: rgba = rgba.resize((dw, dh), Image.NEAREST)
                write_tex_file(f"{output_base}-000.tex", rgba.tobytes(), dw, dh)
                return 1, [100]
            return 0, []
        except Exception:
            return 0, []
    delays_ms = []
    prev_frame = None
    for frame_idx in range(n_frames):
        try:
            img.seek(frame_idx)
            current = img.convert("RGBA")
            if prev_frame is not None and img.info.get("disposal", 0) == 0:
                composed = Image.alpha_composite(prev_frame, current)
            else:
                composed = current
            prev_frame = composed.copy()
            w, h = composed.size
            dw, dh = w, h
            if dw > PANEL_MAX_W: dh = dh * PANEL_MAX_W // dw; dw = PANEL_MAX_W
            if dh > PANEL_MAX_H: dw = dw * PANEL_MAX_H // dh; dh = PANEL_MAX_H
            if dw <= 0 or dh <= 0 or dw > 1024 or dh > 1024: continue
            if dw != w or dh != h: composed = composed.resize((dw, dh), Image.NEAREST)
            write_tex_file(f"{output_base}-{frame_idx:03d}.tex", composed.tobytes(), dw, dh)
            delay_cs = img.info.get("duration", 100) / 10
            if delay_cs < 5: delay_cs = 5
            delays_ms.append(int(delay_cs * 10))
        except Exception:
            if delays_ms: delays_ms.append(delays_ms[-1])
            else: delays_ms.append(100)
            continue
    frame_count = len(delays_ms)
    if frame_count > 0:
        write_anim_file(f"{output_base}.anim", frame_count, delays_ms)
    return frame_count, delays_ms


# ═══════════════════════════════════════════════════════════════════════════════
# MP4 → FRAME SEQUENCE CONVERSION (ffmpeg)
# ═══════════════════════════════════════════════════════════════════════════════

def convert_mp4_to_frames(mp4_path, output_base, wav_path, fps=6):
    """
    Convert an MP4 video into a frame sequence for 3DS playback.
    
    Uses ffmpeg to:
    1. Extract frames at the given FPS → convert each to .tex
    2. Extract audio as WAV
    
    This replaces the old pl_mpeg video approach. Frame sequences use
    the same proven .tex/.anim animation pipeline as GIFs, so no
    GPU texture deletion/recreation is needed on the 3DS.
    
    Returns (frame_count, delays_ms) or (0, []) on failure.
    """
    if not HAS_FFMPEG:
        return 0, []

    import tempfile
    
    # Create a temp directory for extracted PNG frames
    tmpdir = tempfile.mkdtemp(prefix="mspa3ds_")
    frame_pattern = os.path.join(tmpdir, "frame_%04d.png")
    
    frame_count = 0
    delays_ms = []
    
    try:
        # Step 1: Extract frames at target FPS with ffmpeg
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", mp4_path,
                 "-vf", f"fps={fps},scale='min({PANEL_MAX_W},iw)':'min({PANEL_MAX_H},ih)':force_original_aspect_ratio=decrease,pad={PANEL_MAX_W}:{PANEL_MAX_H}:(ow-iw)/2:(oh-ih)/2",
                 frame_pattern],
                capture_output=True, timeout=120
            )
            if result.returncode != 0:
                return 0, []
        except (subprocess.TimeoutExpired, Exception):
            return 0, []
        
        # Step 2: Convert each extracted PNG frame to .tex
        frame_files = sorted([f for f in os.listdir(tmpdir) if f.endswith(".png")])
        if not frame_files:
            return 0, []
        
        delay_ms = int(1000.0 / fps)  # e.g. 167ms for 6 FPS
        
        for idx, fname in enumerate(frame_files):
            fpath = os.path.join(tmpdir, fname)
            try:
                img = Image.open(fpath)
                rgba = img.convert("RGBA")
                w, h = rgba.size
                # Already resized by ffmpeg, but double-check bounds
                dw, dh = w, h
                if dw > PANEL_MAX_W: dh = dh * PANEL_MAX_W // dw; dw = PANEL_MAX_W
                if dh > PANEL_MAX_H: dw = dw * PANEL_MAX_H // dh; dh = PANEL_MAX_H
                if dw <= 0 or dh <= 0 or dw > 1024 or dh > 1024:
                    continue
                if dw != w or dh != h:
                    rgba = rgba.resize((dw, dh), Image.NEAREST)
                tex_path = f"{output_base}-{idx:03d}.tex"
                write_tex_file(tex_path, rgba.tobytes(), dw, dh)
                delays_ms.append(delay_ms)
                frame_count += 1
            except Exception:
                if delays_ms: delays_ms.append(delays_ms[-1])
                else: delays_ms.append(delay_ms)
                continue
        
        if frame_count == 0:
            return 0, []
        
        # Step 3: Write .anim manifest
        write_anim_file(f"{output_base}.anim", frame_count, delays_ms)
        
        # Step 4: Extract audio as WAV
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", mp4_path,
                 "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
                 wav_path],
                capture_output=True, timeout=60
            )
        except (subprocess.TimeoutExpired, Exception):
            pass  # Audio extraction is optional
        
        return frame_count, delays_ms
    
    finally:
        # Clean up temp directory
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# SWF → FRAME SEQUENCE CONVERSION (Ruffle primary + FFDec fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def _frame_to_tex(img, output_base, out_idx):
    """Resize an RGBA frame to fit the panel, letterbox it, write .tex."""
    w, h = img.size
    dw, dh = w, h
    if dw > PANEL_MAX_W: dh = dh * PANEL_MAX_W // dw; dw = PANEL_MAX_W
    if dh > PANEL_MAX_H: dw = dw * PANEL_MAX_H // dh; dh = PANEL_MAX_H
    if dw <= 0 or dh <= 0 or dw > 1024 or dh > 1024:
        return False
    if dw != w or dh != h:
        img = img.resize((dw, dh), Image.NEAREST)
    canvas = Image.new("RGBA", (PANEL_MAX_W, PANEL_MAX_H), (0, 0, 0, 255))
    canvas.paste(img, ((PANEL_MAX_W - dw) // 2, (PANEL_MAX_H - dh) // 2))
    write_tex_file(f"{output_base}-{out_idx:03d}.tex", canvas.tobytes(),
                   PANEL_MAX_W, PANEL_MAX_H)
    return True


def _convert_png_frames(frame_map, output_base, fps, log, progress,
                        step_base, step_span):
    """Convert {source_index: png_path} → .tex frames + .anim manifest.

    frame_map: ordered list of (source_index, png_path), one per output frame.
    Returns (frame_count, delays_ms).
    """
    _lg = log or (lambda m: print(f"[SWF] {m}"))
    _pr = progress or (lambda p: None)
    delay_ms = max(1, int(round(1000.0 / fps)))
    frame_count = 0
    delays_ms = []
    total = len(frame_map)
    for out_idx, (src_idx, png) in enumerate(frame_map):
        try:
            img = Image.open(png)
            rgba = img.convert("RGBA")
            if _frame_to_tex(rgba, output_base, out_idx):
                frame_count += 1
                delays_ms.append(delay_ms)
        except Exception:
            if delays_ms:
                delays_ms.append(delays_ms[-1])
            else:
                delays_ms.append(delay_ms)
            continue
        if total > 8 and out_idx % max(1, total // 10) == 0:
            pct = step_base + int((out_idx / total) * step_span)
            _pr(pct)
    if frame_count > 0:
        write_anim_file(f"{output_base}.anim", frame_count, delays_ms)
    return frame_count, delays_ms


def _extract_swf_audio(swf_path, wav_path, log):
    """Extract audio from an SWF to a 44100Hz stereo WAV.

    Order: ffmpeg direct demux (fast, handles streaming MP3) →
    FFDec sound export (handles event/DefineSound audio).
    Returns True if a usable WAV was produced.
    """
    _lg = log or (lambda m: print(f"[SWF] {m}"))
    # 1. ffmpeg demuxes the SWF sound stream directly (sub-second)
    if HAS_FFMPEG and wav_path:
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", swf_path,
                 "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
                 wav_path],
                capture_output=True, timeout=120
            )
            if result.returncode == 0 and os.path.isfile(wav_path) \
                    and os.path.getsize(wav_path) > 100 * 1024:
                dur = os.path.getsize(wav_path) / 176400.0
                _lg(f"  Audio extracted via ffmpeg ({dur:.0f}s)")
                return True
        except (subprocess.TimeoutExpired, Exception):
            pass
        # clean up a header-only/failed file so FFDec can retry
        try:
            if os.path.isfile(wav_path):
                os.remove(wav_path)
        except Exception:
            pass

    # 2. FFDec sound export (event sounds etc.)
    if HAS_FFDEC and wav_path:
        import tempfile
        sound_tmpdir = tempfile.mkdtemp(prefix="mspa3ds_swf_snd_")
        try:
            if FFDEC_JAR == "ffdec":
                ffdec_cmd = ["ffdec"]
                ffdec_cwd = None
            else:
                ffdec_cmd = ["java", "-Xmx2g", "-jar", FFDEC_JAR]
                ffdec_cwd = os.path.dirname(FFDEC_JAR)
            try:
                subprocess.run(
                    ffdec_cmd + [
                        "-onerror", "ignore",
                        "-format", "sound:wav",
                        "-resamplewav",
                        "-export", "sound",
                        sound_tmpdir,
                        swf_path
                    ],
                    capture_output=True, timeout=180, cwd=ffdec_cwd
                )
            except (subprocess.TimeoutExpired, Exception):
                pass

            # Pick the LARGEST wav (long flashes often have tiny click sounds
            # alongside the main soundtrack)
            wavs = []
            for fname in os.listdir(sound_tmpdir):
                if fname.lower().endswith(".wav"):
                    fpath = os.path.join(sound_tmpdir, fname)
                    wavs.append((os.path.getsize(fpath), fpath))
            if wavs:
                wavs.sort(reverse=True)
                raw_wav = wavs[0][1]
                if os.path.getsize(raw_wav) > 100 * 1024:
                    if HAS_FFMPEG:
                        try:
                            subprocess.run(
                                ["ffmpeg", "-y", "-i", raw_wav,
                                 "-acodec", "pcm_s16le", "-ar", "44100",
                                 "-ac", "2", wav_path],
                                capture_output=True, timeout=120
                            )
                        except (subprocess.TimeoutExpired, Exception):
                            pass
                    else:
                        shutil.copy2(raw_wav, wav_path)
                    if os.path.isfile(wav_path):
                        _lg(f"  Audio extracted via FFDec")
                        return True
        finally:
            try:
                shutil.rmtree(sound_tmpdir, ignore_errors=True)
            except Exception:
                pass

    _lg("  No audio found (silent SWF)")
    return False


def _run_ruffle_export(swf_path, outdir, indices, log, progress):
    """Run the ruffle exporter, capturing only the given source frame indices.

    Returns True if all requested PNGs were produced.
    """
    _lg = log or (lambda m: print(f"[SWF] {m}"))
    _pr = progress or (lambda p: None)
    unique = sorted(set(indices))
    idx_file = os.path.abspath(os.path.join(outdir, "..", "ruffle_indices.txt"))
    with open(idx_file, "w") as f:
        f.write("\n".join(str(i) for i in unique))

    max_idx = unique[-1]
    # MUST match the exporter's own formula: digits = len(str(totalframes))
    # where totalframes = max_index + 1 (see exporter/src/lib.rs)
    digits = len(str(max_idx + 1))

    cmd = [RUFFLE_EXPORTER] + list(RUFFLE_GRAPHICS_ARGS) + [
        "--frame-indices-file", idx_file,
        "--force-play",
        "--silent",
        swf_path,
        outdir,
    ]

    # Scale timeout with movie length; ruffle renders thousands of frames per
    # second on GPU, but headless/software rendering is much slower.
    timeout_s = max(600, int(max_idx * 0.3))

    env = dict(os.environ)
    if os.name != "nt":
        env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")

    expected = len(unique)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env)
    except Exception as e:
        _lg(f"  Ruffle failed to start: {e}")
        return False

    # Watch the output directory for live progress (files stream to disk)
    deadline = time.time() + timeout_s
    last_count = 0
    while proc.poll() is None:
        if time.time() > deadline:
            proc.kill()
            _lg(f"  Ruffle timed out after {timeout_s}s")
            try: os.remove(idx_file)
            except Exception: pass
            return False
        time.sleep(1.0)
        try:
            count = len([n for n in os.listdir(outdir) if n.endswith(".png")])
            if count > last_count:
                last_count = count
                pct = 10 + int((count / expected) * 45)
                _pr(min(pct, 55))
        except Exception:
            pass

    try: os.remove(idx_file)
    except Exception: pass

    if proc.returncode != 0:
        _lg(f"  Ruffle exited with code {proc.returncode}")
        return False

    # Verify every requested index produced a PNG
    missing = [i for i in unique
               if not os.path.isfile(os.path.join(outdir, f"{i:0{digits}d}.png"))]
    if missing:
        _lg(f"  Ruffle missing {len(missing)}/{expected} frames")
        return False

    return True


def _ffdec_extract_frames_chunked(swf_path, outdir, total_frames, log, progress):
    """FFDec fallback: chunked AVI export + ffmpeg → PNG per source frame.

    A fresh JVM per chunk avoids the progressive slowdown and memory
    accumulation that kills monolithic runs on long flashes.
    Returns {source_index: png_path} or {} on failure.
    """
    _lg = log or (lambda m: print(f"[SWF] {m}"))
    _pr = progress or (lambda p: None)
    if not HAS_FFDEC or not HAS_FFMPEG or total_frames <= 0:
        return {}

    if FFDEC_JAR == "ffdec":
        ffdec_cmd = ["ffdec"]
        ffdec_cwd = None
    else:
        ffdec_cmd = ["java", "-Xmx2g", "-jar", FFDEC_JAR]
        ffdec_cwd = os.path.dirname(FFDEC_JAR)

    CHUNK = 750
    start = 0
    chunk_no = 0
    n_chunks = (total_frames + CHUNK - 1) // CHUNK

    while start < total_frames:
        end = min(start + CHUNK - 1, total_frames - 1)
        chunk_no += 1
        _lg(f"  FFDec chunk {chunk_no}/{n_chunks}: frames {start}-{end}")
        pct = 10 + int(((start / total_frames)) * 40)
        _pr(pct)

        import tempfile
        chunkdir = tempfile.mkdtemp(prefix="mspa3ds_ffdec_")
        try:
            # 1. export this frame range as a PNG-codec AVI
            timeout_s = 120 + (end - start + 1) * 2
            try:
                result = subprocess.run(
                    ffdec_cmd + [
                        "-onerror", "ignore",
                        "-select", f"{start + 1}-{end + 1}",
                        "-format", "frame:avi",
                        "-export", "frame",
                        chunkdir,
                        swf_path
                    ],
                    capture_output=True, timeout=timeout_s, cwd=ffdec_cwd
                )
            except (subprocess.TimeoutExpired, Exception) as e:
                _lg(f"  FFDec chunk timed out: {e}")
                return {}

            avi_path = None
            for fname in os.listdir(chunkdir):
                if fname.lower().endswith(".avi"):
                    avi_path = os.path.join(chunkdir, fname)
                    break
            if not avi_path:
                _lg(f"  FFDec produced no AVI for chunk {chunk_no}")
                return {}

            # 2. extract the chunk's frames with ffmpeg, numbered globally
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", avi_path,
                     "-vsync", "0",
                     "-start_number", str(start),
                     os.path.join(outdir, "f_%06d.png")],
                    capture_output=True, timeout=600
                )
            except (subprocess.TimeoutExpired, Exception) as e:
                _lg(f"  ffmpeg failed on chunk: {e}")
                return {}
        finally:
            shutil.rmtree(chunkdir, ignore_errors=True)

        start = end + 1

    # Map source index → png path
    frame_map = {}
    for fname in os.listdir(outdir):
        if fname.startswith("f_") and fname.endswith(".png"):
            try:
                idx = int(fname[2:8])
                frame_map[idx] = os.path.join(outdir, fname)
            except Exception:
                pass
    _pr(55)
    return frame_map


def convert_swf_to_frames(swf_path, output_base, wav_path, fps=6, log=None, progress=None):
    """
    Convert an SWF file into a frame sequence for 3DS playback.

    Primary pipeline (Ruffle — fast, modern, handles long flashes):
      1. Parse the SWF header (frame rate/count) in pure Python
      2. Compute the exact source-frame indices that sample the movie at
         the target FPS (no intermediate 25fps AVI needed)
      3. ruffle_exporter renders just those frames as PNGs (streamed to
         disk, constant memory, --force-play bypasses preloaders)
      4. Pillow: PNG → .tex (fit + letterbox) + .anim manifest
      5. Audio: ffmpeg demuxes the SWF sound stream directly to WAV
         (FFDec sound export as fallback)

    Fallback pipeline (FFDec, for when Ruffle is unavailable):
      Chunked -select frame-range AVI export (fresh JVM per chunk avoids the
      memory blowup / progressive slowdown of monolithic runs on long
      flashes) → ffmpeg → PNG → same .tex conversion.

    Returns (frame_count, delays_ms) or (0, []) on failure.
    """
    def _log(msg):
        if log:
            log(msg)
        else:
            print(f"[SWF] {msg}")

    def _progress(pct):
        if progress:
            progress(pct)

    import tempfile

    # ── Step 0: parse header ──
    hdr = parse_swf_header(swf_path)
    total_frames = hdr["frame_count"] if hdr else 0
    swf_fps = hdr["frame_rate"] if hdr else 0
    if hdr:
        _log(f"SWF: v{hdr['version']}, {hdr['width']}x{hdr['height']}, "
             f"{swf_fps}fps, {total_frames} frames "
             f"({total_frames / max(swf_fps, 1):.0f}s)")

    indices, _delay = compute_decimated_frames(swf_fps, total_frames, fps)
    expected_out = len(indices)
    _log(f"Target: {fps}fps → {expected_out} frames "
         f"({expected_out / fps:.0f}s)")

    tmpdir = tempfile.mkdtemp(prefix="mspa3ds_swf_")
    try:
        frame_count = 0
        delays_ms = []

        # ── Primary: Ruffle exporter ──
        if HAS_RUFFLE:
            _log(f"Step 1/3: Ruffle rendering {len(set(indices))} frames "
                 f"(of {total_frames} source frames)")
            _progress(10)
            pngdir = os.path.join(tmpdir, "png")
            os.makedirs(pngdir, exist_ok=True)

            if _run_ruffle_export(swf_path, pngdir, indices, _log, _progress):
                unique = sorted(set(indices))
                digits = len(str(unique[-1] + 1))  # matches exporter's formula
                frame_map = [(i, os.path.join(pngdir, f"{i:0{digits}d}.png"))
                             for i in indices]
                _log(f"Step 2/3: Converting {len(frame_map)} PNGs → .tex")
                _progress(55)
                frame_count, delays_ms = _convert_png_frames(
                    frame_map, output_base, fps, _log, _progress, 55, 35)
            else:
                _log("  Ruffle export failed — falling back to FFDec")

        # ── Fallback: FFDec (chunked) ──
        if frame_count == 0 and HAS_FFDEC and HAS_FFMPEG and total_frames > 0:
            _log(f"Step 1/3 (FFDec): chunked frame export")
            _progress(10)
            allpng = os.path.join(tmpdir, "ffdec_png")
            os.makedirs(allpng, exist_ok=True)
            fmap = _ffdec_extract_frames_chunked(
                swf_path, allpng, total_frames, _log, _progress)
            if fmap:
                # decimate the full-rate frames to the target fps
                frame_map = [(i, fmap[i]) for i in indices if i in fmap]
                if len(frame_map) < 2:
                    # indices computed for a different frame set — take evenly
                    avail = sorted(fmap.keys())
                    step = max(1, len(avail) // max(1, expected_out))
                    frame_map = [(i, fmap[i]) for i in avail[::step]]
                _log(f"Step 2/3: Converting {len(frame_map)} PNGs → .tex")
                _progress(55)
                frame_count, delays_ms = _convert_png_frames(
                    frame_map, output_base, fps, _log, _progress, 55, 35)

        if frame_count == 0:
            _log("All conversion methods failed")
            return 0, []

        # ── Step 3: audio ──
        _log(f"Step 3/3: extracting audio")
        _progress(92)
        if wav_path:
            _extract_swf_audio(swf_path, wav_path, _log)

        _progress(100)
        _log(f"Done! {frame_count} frames, {len(delays_ms)} delays")
        return frame_count, delays_ms

    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# YOUTUBE → FRAME SEQUENCE CONVERSION (yt-dlp + ffmpeg)
# ═══════════════════════════════════════════════════════════════════════════════

def _yt_dlp_version():
    """Return the yt-dlp version string (e.g. '2026.08.19'), or ''."""
    global _YT_DLP_VERSION_CACHE
    if _YT_DLP_VERSION_CACHE is not None:
        return _YT_DLP_VERSION_CACHE
    _YT_DLP_VERSION_CACHE = ""
    if not YT_DLP_PATH:
        return _YT_DLP_VERSION_CACHE
    try:
        result = subprocess.run(
            [YT_DLP_PATH, "--version"], capture_output=True, timeout=15
        )
        if result.returncode == 0:
            _YT_DLP_VERSION_CACHE = result.stdout.decode(errors="replace").strip()
    except Exception:
        pass
    return _YT_DLP_VERSION_CACHE


def _yt_dlp_is_stale(version):
    """Heuristic: a yt-dlp release date older than ~1 year is considered stale.
    YouTube changes its API frequently and old versions fail with confusing
    errors (e.g. 'Requested format is not available').
    """
    if not version:
        return False
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", version)
    if not m:
        return False
    try:
        release = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    return (datetime.now() - release).days > 365


# Format selector for yt-dlp downloads.
# NOTE: modern YouTube usually has NO combined (video+audio) format available —
# only separate DASH streams. The old selector ("best[ext=mp4]/best") matched
# nothing on those videos → yt-dlp aborted with:
#   "Requested format is not available. Use --list-formats ..." (issue #10).
# The new selector prefers a combined MP4 ≤720p (old YouTube behavior), then
# merges best video ≤720p + best audio with ffmpeg, then relaxes constraints.
YT_DLP_FORMAT = (
    "best[height<=720][ext=mp4]"
    "/bestvideo[height<=720]+bestaudio"
    "/best[height<=720]"
    "/bestvideo+bestaudio"
    "/best"
)


def convert_youtube_to_frames(video_id, output_base, wav_path, fps=6, log=None, progress=None):
    """
    Download a YouTube video and convert it to a 3DS frame sequence.
    
    Pipeline:
      1. yt-dlp: download video (combined MP4 if available, otherwise merged
         video+audio streams via ffmpeg), ≤720p preferred
      2. ffmpeg: video → PNG sequence at target FPS
      3. Pillow: each PNG → .tex (3DS texture format)
      4. Write .anim manifest
      5. ffmpeg: extract audio as 44100Hz stereo WAV
    
    video_id: YouTube video ID (11 characters) or full URL
    log: callable(msg) for debug/status messages
    progress: callable(percent) for progress updates (0-100)
    
    Returns (frame_count, delays_ms) or (0, []) on failure.
    """
    if not HAS_YT_DLP or not HAS_FFMPEG:
        return 0, []
    
    def _log(msg):
        if log:
            log(msg)
        else:
            print(f"[YT] {msg}")
    
    def _progress(pct):
        if progress:
            progress(pct)
    
    import tempfile
    
    tmpdir = tempfile.mkdtemp(prefix="mspa3ds_yt_")
    
    try:
        # Normalize to full URL if just an ID
        if not video_id.startswith("http"):
            url = f"https://www.youtube.com/watch?v={video_id}"
        else:
            url = video_id
        
        mp4_path = os.path.join(tmpdir, "video.mp4")
        
        # ── Step 1: yt-dlp download ──
        _log(f"Step 1/4: yt-dlp downloading {url}")
        _progress(5)
        
        def _find_downloaded():
            """yt-dlp may name the output differently than -o requested (e.g.
            video.webm when the only combined format is webm, or video.mp4.mp4
            after a merge). Find whatever file it actually produced."""
            if os.path.isfile(mp4_path) and os.path.getsize(mp4_path) > 0:
                return mp4_path
            try:
                candidates = [f for f in os.listdir(tmpdir)
                              if f.startswith("video.") and not f.endswith(".part")]
            except Exception:
                return None
            for f in sorted(candidates):
                fpath = os.path.join(tmpdir, f)
                if os.path.getsize(fpath) > 0:
                    return fpath
            return None
        
        # Download strategies, tried in order:
        #   1. Our modern selector (combined mp4 → merged ≤720p → merged any → combined any)
        #   2. yt-dlp's own default format (no -f) — always valid for the installed
        #      version; rescues us if YouTube changes its format inventory again.
        attempts = [
            ("format ≤720p", ["-f", YT_DLP_FORMAT, "--merge-output-format", "mp4"]),
            ("yt-dlp default", []),
        ]
        
        video_path = None
        last_err = ""
        for label, extra_args in attempts:
            try:
                result = subprocess.run(
                    [YT_DLP_PATH,
                     *extra_args,
                     "-o", mp4_path,
                     "--no-playlist",
                     "--no-warnings",
                     url],
                    capture_output=True, timeout=600
                )
            except subprocess.TimeoutExpired:
                last_err = "timed out after 600s"
                _log(f"  yt-dlp timed out ({label})")
                continue
            except Exception as e:
                _log(f"  yt-dlp exception: {e}")
                return 0, []
            
            video_path = _find_downloaded()
            if result.returncode == 0 and video_path:
                break
            
            last_err = result.stderr.decode(errors="replace")
            _log(f"  yt-dlp failed ({label}): {last_err[:200]}")
            # Clean partial output before retrying with the next strategy
            video_path = None
            try:
                for f in os.listdir(tmpdir):
                    os.remove(os.path.join(tmpdir, f))
            except Exception:
                pass
        
        if not video_path:
            ver = _yt_dlp_version()
            _log(f"  All download attempts failed: {last_err[:200]}")
            if ver:
                _log(f"  yt-dlp version: {ver}")
            _log("  Hint: if this keeps failing, update yt-dlp: pip install -U yt-dlp")
            return 0, []
        
        if video_path != mp4_path:
            mp4_path = video_path  # e.g. video.webm — ffmpeg reads by content, not ext
        
        mp4_size = os.path.getsize(mp4_path)
        _log(f"  Downloaded: {mp4_size//1024}KB")
        _progress(40)
        
        # ── Step 2: ffmpeg → PNG frames ──
        _log(f"Step 2/4: ffmpeg extracting {fps}fps frames")
        _progress(45)
        
        frame_pattern = os.path.join(tmpdir, "frame_%04d.png")
        
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", mp4_path,
                 "-vf", f"fps={fps},scale='min({PANEL_MAX_W},iw)':'min({PANEL_MAX_H},ih)':force_original_aspect_ratio=decrease,pad={PANEL_MAX_W}:{PANEL_MAX_H}:(ow-iw)/2:(oh-ih)/2",
                 frame_pattern],
                capture_output=True, timeout=600
            )
        except (subprocess.TimeoutExpired, Exception) as e:
            _log(f"  ffmpeg failed: {e}")
            return 0, []
        
        _progress(60)
        
        # ── Step 3: PNG → .tex ──
        frame_files = sorted([f for f in os.listdir(tmpdir) if f.startswith("frame_") and f.endswith(".png")])
        if not frame_files:
            _log(f"  No frames extracted!")
            _log(f"  ffmpeg stderr: {result.stderr.decode(errors='replace')[-300:]}")
            return 0, []
        
        total_frames = len(frame_files)
        _log(f"Step 3/4: Converting {total_frames} frames to .tex")
        
        delay_ms = int(1000.0 / fps)
        frame_count = 0
        delays_ms = []
        
        for idx, fname in enumerate(frame_files):
            fpath = os.path.join(tmpdir, fname)
            try:
                img = Image.open(fpath)
                rgba = img.convert("RGBA")
                w, h = rgba.size
                dw, dh = w, h
                if dw > PANEL_MAX_W: dh = dh * PANEL_MAX_W // dw; dw = PANEL_MAX_W
                if dh > PANEL_MAX_H: dw = dw * PANEL_MAX_H // dh; dh = PANEL_MAX_H
                if dw <= 0 or dh <= 0 or dw > 1024 or dh > 1024:
                    continue
                if dw != w or dh != h:
                    rgba = rgba.resize((dw, dh), Image.NEAREST)
                tex_path = f"{output_base}-{idx:03d}.tex"
                write_tex_file(tex_path, rgba.tobytes(), dw, dh)
                delays_ms.append(delay_ms)
                frame_count += 1
            except Exception:
                if delays_ms: delays_ms.append(delays_ms[-1])
                else: delays_ms.append(delay_ms)
                continue
            
            if total_frames > 10 and idx % max(1, total_frames // 10) == 0:
                pct = 60 + int((idx / total_frames) * 30)
                _progress(pct)
        
        if frame_count == 0:
            _log(f"  No frames converted!")
            return 0, []
        
        _log(f"  Converted {frame_count}/{total_frames} frames")
        _progress(92)
        
        # ── Step 4: .anim + audio ──
        _log(f"Step 4/4: Writing .anim + extracting audio")
        write_anim_file(f"{output_base}.anim", frame_count, delays_ms)
        
        if wav_path:
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", mp4_path,
                     "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
                     wav_path],
                    capture_output=True, timeout=60
                )
            except (subprocess.TimeoutExpired, Exception):
                pass
        
        _progress(100)
        _log(f"Done! {frame_count} frames")
        return frame_count, delays_ms
    
    finally:
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# SCRAPER ENGINE (threaded, posts progress to a queue)
# ═══════════════════════════════════════════════════════════════════════════════

class BuildCancelled(Exception):
    pass

class ScraperEngine:
    def __init__(self, progress_queue, cancel_event, comic_slug, comic_offset):
        self.q = progress_queue
        self.cancel = cancel_event
        self.comic_slug = comic_slug
        self.comic_offset = comic_offset
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "MSPA-3DS-Bundler/1.0",
            "Accept": "*/*",
        })

    def _check_cancel(self):
        if self.cancel.is_set():
            raise BuildCancelled()

    def _post(self, msg, progress=None, phase=""):
        self.q.put({"msg": msg, "progress": progress, "phase": phase})

    def fetch_page_html(self, page_num):
        self._check_cancel()
        url = f"{MIRROR_BASE}/{self.comic_slug}/{page_num}"
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            return resp.text if resp.status_code == 200 else None
        except requests.RequestException:
            return None

    def parse_page(self, page_num, html):
        global_page = page_num + self.comic_offset
        soup = BeautifulSoup(html, "html.parser")
        title = extract_title(soup)
        command, next_page = extract_command_and_next(soup, self.comic_slug, self.comic_offset)
        media_urls, is_flash = extract_media_urls(soup, global_page, html, self.comic_slug)
        texts = extract_texts(soup)
        audio_url = f"{FLASH_WAV_BASE}{global_page:06d}.wav" if is_flash else ""
        return {
            "page": global_page, "next_page": next_page,
            "type": title or "PAGE", "command": command,
            "audio_url": audio_url, "media_urls": media_urls, "texts": texts,
            "is_flash": is_flash,
        }

    def download_media(self, url, dest_path):
        self._check_cancel()
        try:
            resp = self.session.get(url, timeout=60, stream=True)
            if resp.status_code != 200: return False
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    self._check_cancel()
                    f.write(chunk)
            return True
        except (requests.RequestException, BuildCancelled):
            raise
        except Exception:
            return False

    def build_bundle(self, start, end, name, output_dir):
        comic_name = COMICS.get(self.comic_slug, {}).get("name", self.comic_slug.title())
        pack_id = name or f"{self.comic_slug}-{start}-{end or 'end'}"
        bundle_dir = os.path.join(output_dir, pack_id)

        # ── Phase 1: Scan ──
        self._post(f"Scanning {comic_name} pages starting from page {start}...", 0, "scan")
        pages = []
        current = start
        max_pages = (end - start + 1) if end else 9999
        consecutive_fails = 0

        while len(pages) < max_pages and consecutive_fails < 3:
            self._check_cancel()
            html = self.fetch_page_html(current)
            if html is None:
                consecutive_fails += 1
                if consecutive_fails >= 3: break
                if end: current += 1; continue
                else: break
            if is_mirror_404(html):
                # Mirror hole: the mirror serves its 404 page with HTTP 200
                # (e.g. Homestuck page 78 is missing while 79+ exist). Skip
                # forward — the story usually continues after the gap. Three
                # holes in a row means we've passed the end of the story.
                consecutive_fails += 1
                if consecutive_fails >= 3: break
                current += 1
                continue
            consecutive_fails = 0
            pages.append(current)
            if end and current >= end: break
            soup = BeautifulSoup(html, "html.parser")
            _, next_page = extract_command_and_next(soup, self.comic_slug, self.comic_offset)
            if next_page <= 0 or next_page <= current: break
            current = next_page
            self._post(f"Scanning... found page {current}", len(pages), "scan")
            time.sleep(REQUEST_DELAY * 0.5)

        if not pages:
            self._post("ERROR: No pages found!", None, "error")
            return None

        self._post(f"Found {len(pages)} pages", None, "scan")

        # ── Phase 2: Scrape (with page splitting) ──
        # Multi-image pages get split into sub-pages.
        # Virtual page number = original_global * 100 + sub_index
        # This means each page has exactly 1 image, and the 3DS navigates
        # between them normally via the "next" field — no code changes needed.
        pages_data = {}   # virtual_page → page data dict
        media_downloads = []

        for i, page_num in enumerate(pages):
            self._check_cancel()
            pct = int((i / len(pages)) * 100)
            self._post(f"Scraping page {page_num + self.comic_offset}...", pct, "scrape")

            html = self.fetch_page_html(page_num)
            if html is None or is_mirror_404(html): continue

            parsed = self.parse_page(page_num, html)
            global_page = parsed["page"]
            n_media = len(parsed["media_urls"])

            # Calculate next virtual page (the page after all sub-pages of the NEXT page)
            next_global = 0
            if parsed["next_page"] > 0:
                next_global = (parsed["next_page"] + self.comic_offset) * 100

            if n_media <= 1:
                # Single image (or no image) — one page
                vpage = global_page * 100  # virtual page number

                local_media = []
                if n_media == 1:
                    ext = _get_ext(parsed["media_urls"][0]) or ".gif"
                    if parsed["is_flash"]:
                        # Use .swf for SWF URLs (FFDec conversion), .mp4 for MP4 URLs (ffmpeg)
                        if ext.lower() != ".swf":
                            ext = ".mp4"  # filegarden MP4 fallback
                    local_path = f"media/{global_page:06d}_0{ext}"
                    local_media.append(local_path)
                    media_downloads.append({
                        "url": parsed["media_urls"][0],
                        "local_path": local_path,
                        "kind": "media",
                        "global_page": global_page,
                        "is_video": ext in (".mpg", ".mpeg", ".mp4"),
                        "is_flash": parsed["is_flash"],
                        "vpage": vpage,
                    })

                local_audio = ""
                if parsed["audio_url"] and not parsed["is_flash"]:
                    # Regular pages: download WAV from source
                    # [S] pages: audio is extracted from MP4 by convert_mp4_to_frames()
                    local_audio = f"media/{global_page:06d}.wav"
                    media_downloads.append({
                        "url": parsed["audio_url"],
                        "local_path": local_audio,
                        "kind": "audio",
                        "global_page": global_page,
                        "is_video": False,
                        "is_flash": False,
                        "vpage": vpage,
                    })

                pages_data[vpage] = {
                    "schema": BUNDLE_SCHEMA,
                    "page": vpage,
                    "display_page": global_page,
                    "next": next_global,
                    "type": parsed["type"],
                    "alt": "",
                    "command": parsed["command"],
                    "audio": local_audio,
                    "media": local_media,
                    "text": parsed["texts"],
                }
            else:
                # Multiple images — split into sub-pages
                for mi, url in enumerate(parsed["media_urls"]):
                    vpage = global_page * 100 + mi
                    # Next sub-page if there are more images, otherwise next real page
                    vpage_next = global_page * 100 + mi + 1 if mi < n_media - 1 else next_global

                    ext = _get_ext(url) or ".gif"
                    local_path = f"media/{global_page:06d}_{mi}{ext}"
                    local_media = [local_path]

                    media_downloads.append({
                        "url": url,
                        "local_path": local_path,
                        "kind": "media",
                        "global_page": global_page,
                        "is_video": ext in (".mpg", ".mpeg", ".mp4"),
                        "is_flash": False,
                        "vpage": vpage,
                    })

                    # Audio only on the last sub-page
                    local_audio = ""
                    if mi == n_media - 1 and parsed["audio_url"]:
                        local_audio = f"media/{global_page:06d}.wav"
                        media_downloads.append({
                            "url": parsed["audio_url"],
                            "local_path": local_audio,
                            "kind": "audio",
                            "global_page": global_page,
                            "is_video": False,
                            "is_flash": False,
                            "vpage": vpage,
                        })

                    pages_data[vpage] = {
                        "schema": BUNDLE_SCHEMA,
                        "page": vpage,
                        "display_page": global_page,
                        "next": vpage_next,
                        "type": parsed["type"],
                        "alt": "",
                        "command": parsed["command"],
                        "audio": local_audio,
                        "media": local_media,
                        "text": parsed["texts"] if mi == 0 else [],  # text only on first sub-page
                    }

            time.sleep(REQUEST_DELAY)

        self._post(f"Scraped {len(pages_data)} pages (from {len(pages)} original)", None, "scrape")

        # ── Phase 3: Download media ──
        os.makedirs(os.path.join(bundle_dir, "pages"), exist_ok=True)
        os.makedirs(os.path.join(bundle_dir, "media"), exist_ok=True)
        downloaded = 0

        for i, item in enumerate(media_downloads):
            self._check_cancel()
            pct = int((i / len(media_downloads)) * 100)
            self._post(f"Downloading {os.path.basename(item['local_path'])}...", pct, "download")

            fs_path = os.path.join(bundle_dir, item["local_path"])

            if item.get("is_flash") and item["kind"] == "media":
                # [S] page: download and extract frame sequence
                output_base = os.path.splitext(fs_path)[0]
                global_page = item.get("global_page", 0)
                wav_path = os.path.join(bundle_dir, f"media/{global_page:06d}.wav")
                vpage = item["vpage"]

                downloaded_ok = self.download_media(item["url"], fs_path)
                # Route by CONTENT, not by URL extension — some mirror flash
                # URLs are extensionless (page 77's AC_RunActiveContent JS
                # embed), and some "video" URLs actually serve SWFs.
                is_swf = downloaded_ok and _file_is_swf(fs_path)

                frame_count = 0
                if downloaded_ok:
                    downloaded += 1
                    if is_swf and (HAS_RUFFLE or HAS_FFDEC):
                        # SWF → Ruffle (primary) / FFDec (fallback) → frames
                        self._post(f"Converting SWF: {os.path.basename(fs_path)}", None, "convert")
                        frame_count, delays = convert_swf_to_frames(
                            fs_path, output_base, wav_path, fps=6,
                            log=lambda m: self._post(f"[SWF] {m}", None, "convert"),
                            progress=lambda p: self._post(f"SWF conversion: {p}%", p, "convert"))
                    elif HAS_FFMPEG:
                        # MP4/WebM → ffmpeg → frames (archive/mirror video)
                        frame_count, delays = convert_mp4_to_frames(
                            fs_path, output_base, wav_path, fps=6)
                    else:
                        self._post(f"Warning: no renderer available, [S] page will have no frames", None, "warn")

                # Last resort: pre-converted MP4 from the archive — also
                # used when the flash URL itself failed to download. (The
                # old code left a DANGLING media reference in the page JSON
                # in that case, which hard-failed the whole page on the 3DS:
                # blue placeholder cube + the previous page's text.)
                if frame_count == 0 and HAS_FFMPEG:
                    mp4_url = f"{FLASH_MP4_BASE}{global_page:06d}.mp4"
                    mp4_path = os.path.splitext(fs_path)[0] + ".mp4"
                    self._post(f"Falling back to archive MP4 for page {global_page}...", None, "convert")
                    try: os.remove(fs_path)
                    except Exception: pass
                    if self.download_media(mp4_url, mp4_path):
                        frame_count, delays = convert_mp4_to_frames(
                            mp4_path, output_base, wav_path, fps=6)
                        try: os.remove(mp4_path)
                        except Exception: pass
                    if frame_count == 0:
                        self._post(f"Warning: no archive MP4 for page {global_page}", None, "warn")

                # Normalize audio (PCM16 WAV; downsample if too big for the
                # 3DS linear heap) — drop it if it can't be made playable
                if os.path.exists(wav_path) and not _normalize_audio_wav(wav_path):
                    try: os.remove(wav_path)
                    except Exception: pass

                if frame_count > 0:
                    # Update page data: change media reference to .gif
                    # so the 3DS treats it as an animation with pre-converted .tex
                    new_rel = os.path.splitext(item["local_path"])[0] + ".gif"
                    if vpage in pages_data:
                        pages_data[vpage]["media"] = [new_rel]
                        if os.path.exists(wav_path):
                            pages_data[vpage]["audio"] = f"media/{global_page:06d}.wav"
                    # Delete the source file — we've extracted everything
                    try: os.remove(fs_path)
                    except: pass
                else:
                    # NEVER leave a dangling media reference: a page JSON
                    # pointing at a nonexistent file hard-fails the page on
                    # the 3DS. Degrade to a text-only page instead.
                    self._post(f"Warning: no media for page {global_page} — page will be text-only", None, "warn")
                    try:
                        if os.path.exists(fs_path): os.remove(fs_path)
                    except Exception: pass
                    if vpage in pages_data and item["local_path"] in pages_data[vpage]["media"]:
                        pages_data[vpage]["media"].remove(item["local_path"])
            else:
                if self.download_media(item["url"], fs_path):
                    downloaded += 1
                else:
                    vpage = item["vpage"]
                    if vpage in pages_data:
                        if item["kind"] == "audio":
                            pages_data[vpage]["audio"] = ""
                        elif item["local_path"] in pages_data[vpage]["media"]:
                            pages_data[vpage]["media"].remove(item["local_path"])

            time.sleep(REQUEST_DELAY * 0.5)

        self._post(f"Downloaded {downloaded}/{len(media_downloads)} files", None, "download")

        # ── Phase 4: Convert images → .tex + .anim ──
        converted = 0
        for i, item in enumerate(media_downloads):
            self._check_cancel()
            pct = int((i / len(media_downloads)) * 100)
            self._post(f"Converting {os.path.basename(item['local_path'])}...", pct, "convert")

            if item["kind"] != "media": continue
            if item.get("is_flash"): continue  # [S] pages already converted to frame sequence
            fs_path = os.path.join(bundle_dir, item["local_path"])
            if not os.path.exists(fs_path): continue
            ext = os.path.splitext(fs_path)[1].lower()
            if ext not in (".gif", ".png", ".jpg", ".jpeg"): continue
            output_base = os.path.splitext(fs_path)[0]
            try:
                frame_count, delays = convert_gif_to_tex(fs_path, output_base, item["is_video"])
                if frame_count > 0:
                    converted += 1
                    # Keep the original GIF as fallback — the 3DS GIF→tex pipeline
                    # is proven; if our pre-converted .tex has issues, it can fall back
            except Exception as e:
                self._post(f"Warning: conversion failed for {fs_path}: {e}", None, "warn")

        self._post(f"Converted {converted} images to .tex", None, "convert")

        # ── Phase 5: Write page JSONs and manifest ──
        self._post("Writing page data...", 95, "package")

        # Fix up the navigation chain:
        #  - mirror holes: if a page's "next" target was skipped (e.g.
        #    Homestuck page 78 doesn't exist on the mirror), retarget it to
        #    the next available page so the reader hops over the gap instead
        #    of hard-failing (blue cube + stale text).
        #  - "prev" links: virtual pages are numbered global_page*100, so
        #    the 3DS BACK button's naive pageNum-1 NEVER hits an existing
        #    page. Write explicit previous-page targets (used by updated
        #    readers; older readers just ignore the extra field).
        _keys = sorted(pages_data.keys())
        _keyset = set(_keys)
        for _vp in _keys:
            _pd = pages_data[_vp]
            _nxt = _pd.get("next") or 0
            if _nxt and _nxt not in _keyset:
                _later = [k for k in _keys if k > _nxt]
                _pd["next"] = _later[0] if _later else 0
            _prevs = [k for k in _keys if k < _vp]
            _pd["prev"] = _prevs[-1] if _prevs else 0

        first_vpage = min(pages_data.keys()) if pages_data else 0
        last_vpage = max(pages_data.keys()) if pages_data else 0
        manifest = {
            "pack_id": pack_id,
            "title": f"{comic_name} (pages {start}\u2013{end})" if end else f"{comic_name} (page {start})",
            "source": f"{MIRROR_BASE}/{self.comic_slug}/{start}",
            "first_page": first_vpage,
            "last_page": last_vpage,
            "page_count": len(pages_data),
            "schema": BUNDLE_SCHEMA,
            "next_pack": "",
            "prev_pack": "",
        }
        with open(os.path.join(bundle_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        for vpage, pdata in pages_data.items():
            page_path = os.path.join(bundle_dir, f"pages/{vpage:06d}.json")
            with open(page_path, "w", encoding="utf-8") as f:
                json.dump(pdata, f, indent=2, ensure_ascii=False)

        total_size = 0
        for root, dirs, files in os.walk(bundle_dir):
            for fname in files:
                total_size += os.path.getsize(os.path.join(root, fname))
        size_mb = total_size / (1024 * 1024)

        self._post(f"Done! {len(pages_data)} pages, {downloaded} media, {converted} converted, {size_mb:.1f} MB", 100, "done")
        return bundle_dir


class MspfaScraperEngine:
    """Scraper engine for MS Paint Fan Adventures (mspfa.com).
    
    Uses the MSPFA JSON API (POST to / with do=story) to fetch the entire
    story at once, then processes pages from the JSON data.
    
    Produces bundles in the EXACT SAME format as ScraperEngine so the
    3DS app reads them without any code changes:
    
        <pack_id>/
        ├── manifest.json
        ├── pages/
        │   ├── <vpage:06d>.json   (same schema as MSPA pages)
        │   └── ...
        └── media/
            ├── <vpage:06d>_<i>.anim
            ├── <vpage:06d>_<i>-000.tex
            └── ...
    
    Virtual page numbering: vpage = story_id * MSPFA_PAGE_MULTIPLIER + mspfa_page
    """
    
    def __init__(self, progress_queue, cancel_event, story_id):
        self.q = progress_queue
        self.cancel = cancel_event
        self.story_id = story_id
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "MSPA-3DS-Bundler/1.0",
            "Accept": "*/*",
        })
    
    def _check_cancel(self):
        if self.cancel.is_set():
            raise BuildCancelled()
    
    def _post(self, msg, progress=None, phase=""):
        self.q.put({"msg": msg, "progress": progress, "phase": phase})
    
    def _vpage(self, mspfa_page):
        """Convert an MSPFA page number to a virtual page number for the bundle."""
        return self.story_id * MSPFA_PAGE_MULTIPLIER + mspfa_page
    
    def download_media(self, url, dest_path):
        self._check_cancel()
        try:
            resp = self.session.get(url, timeout=60, stream=True)
            if resp.status_code != 200:
                return False
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    self._check_cancel()
                    f.write(chunk)
            return True
        except (requests.RequestException, BuildCancelled):
            raise
        except Exception:
            return False
    
    def build_bundle(self, start_page, end_page, name, output_dir):
        """Build a bundle for an MSPFA story from start_page to end_page.
        
        Uses the MSPFA JSON API to fetch the entire story at once,
        then processes only the requested page range.
        If end_page is 0, processes all pages from start_page to the end.
        """
        pack_id = name or f"mspfa-{self.story_id}-{start_page}-{end_page or 'end'}"
        bundle_dir = os.path.join(output_dir, pack_id)
        
        # ── Phase 1: Fetch story data via JSON API ──
        self._post(f"Fetching MSPFA story {self.story_id}...", 0, "scan")
        self._check_cancel()
        
        story_data = mspfa_fetch_story(self.session, self.story_id)
        if story_data is None:
            self._post("ERROR: Could not fetch story data! Check the story ID.", None, "error")
            return None
        
        story_title = story_data.get("n", f"MSPFA Story {self.story_id}")
        story_css = story_data.get("y", "")
        all_pages = story_data.get("p", [])
        total_pages = len(all_pages)
        
        if total_pages == 0:
            self._post("ERROR: Story has no pages!", None, "error")
            return None
        
        self._post(f"Found: {story_title} ({total_pages} pages)", None, "scan")
        
        # Determine the page range to process
        # MSPFA pages are 1-based, array is 0-indexed: page N = all_pages[N-1]
        if not end_page or end_page > total_pages:
            end_page = total_pages
        
        if start_page < 1:
            start_page = 1
        
        page_count = end_page - start_page + 1
        self._post(f"Processing pages {start_page}\u2013{end_page} ({page_count} pages)", None, "scan")
        
        # ── Phase 2: Process pages from JSON data ──
        pages_data = {}
        media_downloads = []
        
        for i in range(start_page - 1, end_page):
            self._check_cancel()
            page_num = i + 1  # 1-based MSPFA page number
            pct = int(((page_num - start_page) / page_count) * 100)
            self._post(f"Processing page {page_num}...", pct, "scrape")
            
            page_data = all_pages[i]
            if page_data is None:
                continue
            
            command = (page_data.get("c", "") or "").strip()
            body = page_data.get("b", "")
            next_pages = page_data.get("n", []) or []
            
            # MSPFA layout vs MSPA (issue #6):
            #   MSPA pages have  h2#title  (the page's OWN title, shown at the
            #   top) and a blue "commands" link at the bottom pointing at the
            #   NEXT page (its text = the next page's title).
            #   MSPFA's "c" field is the command shown at the TOP of the page
            #   on mspfa.com — it plays the role of h2#title (it is the command
            #   that led to this page). The blue next-command at the bottom of
            #   the reader must therefore be the NEXT page's "c", not this
            #   page's. The old code put "c" in the bottom command slot and
            #   left the title as "PAGE" — exactly the reported bug.
            next_command = ""  # no next page → no bottom command
            if next_pages and 0 < next_pages[0] <= total_pages:
                next_data = all_pages[next_pages[0] - 1]
                next_command = ((next_data or {}).get("c", "") or "").strip()
                if not next_command:
                    # mspfa.com always shows an "==&gt;" arrow as the next link
                    next_command = "==&gt;"
            
            # Extract images and text from BBCode body
            media_urls, media_type = mspfa_parse_images(body)
            text_lines = mspfa_parse_text(body)
            
            # Find audio for this page from CSS directives
            audio_url = mspfa_find_audio(story_css, page_num)
            
            vpage = self._vpage(page_num)
            
            # Next virtual page: first entry in the 'n' array
            next_global = 0
            if next_pages and next_pages[0] > 0 and next_pages[0] <= total_pages:
                next_global = self._vpage(next_pages[0])
            
            # Determine extension and flags based on media type
            is_flash = (media_type == 'swf')
            is_video = (media_type == 'video')
            is_youtube = (media_type == 'youtube')
            has_extractable_media = is_flash or is_video or is_youtube
            
            # Build the page. ALL of the page's images go into the SAME page's
            # media array — the 3DS reader cycles through multiple media
            # natively (A button: next media of the page, then next page).
            # The old code split multi-image pages into sub-pages at
            # vpage + mi, which COLLIDED with the next real page's vpage
            # (e.g. page 7's 2nd image overwrote page 8's page JSON) and
            # effectively lost the extra images — issue #6, "There should
            # also be multiple images on this page".
            local_media = []
            for mi, url in enumerate(media_urls):
                if is_youtube:
                    ext = ".mp4"  # YouTube videos download as MP4
                elif is_flash:
                    ext = ".swf"
                elif is_video:
                    ext = _get_ext(url) or ".mp4"
                else:
                    ext = mspfa_media_ext(url)
                local_path = f"media/{vpage:06d}_{mi}{ext}"
                local_media.append(local_path)
                media_downloads.append({
                    "url": url,
                    "local_path": local_path,
                    "kind": "media",
                    "vpage": vpage,
                    "is_video": is_video or is_youtube,
                    "is_flash": is_flash,
                    "is_youtube": is_youtube,
                })
            
            local_audio = ""
            if audio_url and not has_extractable_media:
                # Video/SWF/YouTube pages: audio is extracted during conversion
                local_audio = f"media/{vpage:06d}.wav"
                media_downloads.append({
                    "url": audio_url,
                    "local_path": local_audio,
                    "kind": "audio",
                    "vpage": vpage,
                    "is_video": False,
                    "is_flash": False,
                })
            
            pages_data[vpage] = {
                "schema": BUNDLE_SCHEMA,
                "page": vpage,
                "next": next_global,
                "type": command or "PAGE",  # page's own command = title (like h2#title)
                "alt": "",
                "command": next_command,    # next page's command (blue, bottom)
                "audio": local_audio,
                "media": local_media,
                "text": text_lines,
            }
        
        self._post(f"Processed {len(pages_data)} pages (from {page_count} original)", None, "scrape")
        
        # ── Phase 3: Download media ──
        os.makedirs(os.path.join(bundle_dir, "pages"), exist_ok=True)
        os.makedirs(os.path.join(bundle_dir, "media"), exist_ok=True)
        downloaded = 0
        
        for i, item in enumerate(media_downloads):
            self._check_cancel()
            pct = int((i / len(media_downloads)) * 100)
            self._post(f"Downloading {os.path.basename(item['local_path'])}...", pct, "download")
            
            fs_path = os.path.join(bundle_dir, item["local_path"])
            
            # MSPFA audio is often MP3/OGG, but the 3DS reader can only
            # play PCM 16/8-bit WAVs — convert AND normalize (this also
            # fixes float/24-bit WAV sources and guards against files too
            # big for the 3DS linear heap by downsampling). An audio file
            # that can't be made playable is dropped (audio="") instead of
            # being referenced but silently unplayable.
            if item["kind"] == "audio":
                vpage = item["vpage"]
                src_ext = _get_ext(item["url"]) or ".mp3"
                tmp_path = fs_path + ".tmp" + src_ext
                if self.download_media(item["url"], tmp_path) and \
                        _normalize_audio_wav(tmp_path):
                    downloaded += 1
                    try: os.replace(tmp_path, fs_path)
                    except Exception: pass
                else:
                    self._post(f"Warning: audio for page {vpage} is not playable on 3DS — dropped", None, "warn")
                    try:
                        if os.path.isfile(tmp_path): os.remove(tmp_path)
                    except Exception: pass
                    if vpage in pages_data:
                        pages_data[vpage]["audio"] = ""
            elif item.get("is_youtube"):
                # YouTube video: download with yt-dlp, convert to frames
                video_id = item["url"]
                output_base = os.path.splitext(fs_path)[0]
                wav_path = os.path.splitext(fs_path)[0].rsplit("_", 1)[0] + ".wav"
                
                self._post(f"YouTube: {video_id}", None, "convert")
                frame_count, delays = convert_youtube_to_frames(
                    video_id, output_base, wav_path, fps=6,
                    log=lambda m: self._post(f"[YT] {m}", None, "convert"),
                    progress=lambda p: self._post(f"YouTube conversion: {p}%", p, "convert"))

                # Normalize audio (PCM16 WAV; downsample if too big for the
                # 3DS linear heap) — drop it if it can't be made playable
                if os.path.exists(wav_path) and not _normalize_audio_wav(wav_path):
                    try: os.remove(wav_path)
                    except Exception: pass

                if frame_count > 0:
                    new_rel = os.path.splitext(item["local_path"])[0] + ".gif"
                    vpage = item["vpage"]
                    if vpage in pages_data:
                        pages_data[vpage]["media"] = [new_rel]
                        if os.path.exists(wav_path):
                            # bundle-relative path ("media/NNN.wav"), like the
                            # MSPA engine — wav_path itself may be absolute
                            pages_data[vpage]["audio"] = "media/" + os.path.basename(wav_path)
                    downloaded += 1
                else:
                    self._post(f"Warning: YouTube conversion failed for {video_id}", None, "warn")
                    # never leave a dangling media reference — degrade to a
                    # text page instead of hard-failing on the 3DS
                    vpage = item["vpage"]
                    if vpage in pages_data and item["local_path"] in pages_data[vpage]["media"]:
                        pages_data[vpage]["media"].remove(item["local_path"])
                
                # Remove the placeholder file (we never actually downloaded it as .mp4)
                try: os.remove(fs_path)
                except: pass
                
            elif item.get("is_flash") or item.get("is_video"):
                # SWF or direct video file
                if self.download_media(item["url"], fs_path):
                    downloaded += 1
                    output_base = os.path.splitext(fs_path)[0]
                    wav_path = os.path.splitext(fs_path)[0].rsplit("_", 1)[0] + ".wav"

                    frame_count = 0

                    # Route by CONTENT, not by URL extension — some flash
                    # URLs are extensionless and some "video" URLs are SWFs
                    if item.get("is_flash") and _file_is_swf(fs_path) and \
                            (HAS_RUFFLE or HAS_FFDEC):
                        self._post(f"Converting SWF: {os.path.basename(fs_path)}", None, "convert")
                        frame_count, delays = convert_swf_to_frames(
                            fs_path, output_base, wav_path, fps=6,
                            log=lambda m: self._post(f"[SWF] {m}", None, "convert"),
                            progress=lambda p: self._post(f"SWF conversion: {p}%", p, "convert"))
                    elif HAS_FFMPEG:
                        self._post(f"Converting video: {os.path.basename(fs_path)}", None, "convert")
                        frame_count, delays = convert_mp4_to_frames(
                            fs_path, output_base, wav_path, fps=6)
                    else:
                        self._post(f"Warning: no renderer available, [S] page will have no frames", None, "warn")

                    # Last resort: pre-converted MP4 from the archive (MSPA pages only)
                    if frame_count == 0 and HAS_FFMPEG and str(item.get("global_page", "")).isdigit():
                        global_page = item.get("global_page", 0)
                        mp4_url = f"{FLASH_MP4_BASE}{global_page:06d}.mp4"
                        mp4_path = os.path.splitext(fs_path)[0] + ".mp4"
                        self._post(f"Falling back to archive MP4...", None, "convert")
                        try: os.remove(fs_path)
                        except Exception: pass
                        if self.download_media(mp4_url, mp4_path):
                            frame_count, delays = convert_mp4_to_frames(
                                mp4_path, output_base, wav_path, fps=6)
                            try: os.remove(mp4_path)
                            except Exception: pass

                    # Normalize audio (PCM16 WAV; downsample if too big for
                    # the 3DS linear heap) — drop it if unplayable
                    if os.path.exists(wav_path) and not _normalize_audio_wav(wav_path):
                        try: os.remove(wav_path)
                        except Exception: pass

                    if frame_count > 0:
                        new_rel = os.path.splitext(item["local_path"])[0] + ".gif"
                        vpage = item["vpage"]
                        if vpage in pages_data:
                            pages_data[vpage]["media"] = [new_rel]
                            if os.path.exists(wav_path):
                                # bundle-relative path ("media/NNN.wav"), like the
                                # MSPA engine — wav_path itself may be absolute
                                pages_data[vpage]["audio"] = "media/" + os.path.basename(wav_path)
                        try: os.remove(fs_path)
                        except: pass
                    else:
                        self._post(f"Warning: conversion failed for {os.path.basename(fs_path)}", None, "warn")
                        try: os.remove(fs_path)
                        except: pass
                        # never leave a dangling media reference — degrade
                        # to a text page instead of hard-failing on the 3DS
                        vpage = item["vpage"]
                        if vpage in pages_data and item["local_path"] in pages_data[vpage]["media"]:
                            pages_data[vpage]["media"].remove(item["local_path"])
                else:
                    self._post(f"Warning: could not download {os.path.basename(fs_path)}", None, "warn")
                    # never leave a dangling media reference — degrade to a
                    # text page instead of hard-failing on the 3DS
                    vpage = item["vpage"]
                    if vpage in pages_data and item["local_path"] in pages_data[vpage]["media"]:
                        pages_data[vpage]["media"].remove(item["local_path"])
            else:
                if self.download_media(item["url"], fs_path):
                    downloaded += 1
                else:
                    vpage = item["vpage"]
                    if vpage in pages_data:
                        if item["local_path"] in pages_data[vpage]["media"]:
                            pages_data[vpage]["media"].remove(item["local_path"])
            
            time.sleep(REQUEST_DELAY * 0.5)
        
        self._post(f"Downloaded {downloaded}/{len(media_downloads)} files", None, "download")
        
        # ── Phase 4: Convert images → .tex + .anim ──
        converted = 0
        for i, item in enumerate(media_downloads):
            self._check_cancel()
            pct = int((i / len(media_downloads)) * 100)
            self._post(f"Converting {os.path.basename(item['local_path'])}...", pct, "convert")
            
            if item["kind"] != "media":
                continue
            fs_path = os.path.join(bundle_dir, item["local_path"])
            if not os.path.exists(fs_path):
                continue
            ext = os.path.splitext(fs_path)[1].lower()
            if ext not in (".gif", ".png", ".jpg", ".jpeg"):
                continue
            output_base = os.path.splitext(fs_path)[0]
            try:
                frame_count, delays = convert_gif_to_tex(fs_path, output_base, item["is_video"])
                if frame_count > 0:
                    converted += 1
            except Exception as e:
                self._post(f"Warning: conversion failed for {fs_path}: {e}", None, "warn")
        
        self._post(f"Converted {converted} images to .tex", None, "convert")
        
        # ── Phase 5: Write page JSONs and manifest ──
        self._post("Writing page data...", 95, "package")

        # Add "prev" links (previous AVAILABLE page) so the 3DS BACK button
        # works even when the bundle starts mid-story or pages were skipped
        # (older readers just ignore the extra field).
        _keys = sorted(pages_data.keys())
        for _vp in _keys:
            _prevs = [k for k in _keys if k < _vp]
            pages_data[_vp]["prev"] = _prevs[-1] if _prevs else 0

        first_vpage = min(pages_data.keys()) if pages_data else 0
        last_vpage = max(pages_data.keys()) if pages_data else 0
        
        title = story_title
        manifest = {
            "pack_id": pack_id,
            "title": f"{title} (pages {start_page}\u2013{end_page})" if end_page else f"{title} (all pages)",
            "source": f"{MSPFA_BASE}/?s={self.story_id}&p={start_page}",
            "first_page": first_vpage,
            "last_page": last_vpage,
            "page_count": len(pages_data),
            "schema": BUNDLE_SCHEMA,
            "next_pack": "",
            "prev_pack": "",
        }
        with open(os.path.join(bundle_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        
        for vpage, pdata in pages_data.items():
            page_path = os.path.join(bundle_dir, f"pages/{vpage:06d}.json")
            with open(page_path, "w", encoding="utf-8") as f:
                json.dump(pdata, f, indent=2, ensure_ascii=False)
        
        total_size = 0
        for root, dirs, files in os.walk(bundle_dir):
            for fname in files:
                total_size += os.path.getsize(os.path.join(root, fname))
        size_mb = total_size / (1024 * 1024)
        
        self._post(f"Done! {len(pages_data)} pages, {downloaded} media, {converted} converted, {size_mb:.1f} MB", 100, "done")
        return bundle_dir


# ═══════════════════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════════════════

class MspaBuilderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("MSPA-3DS Bundle Builder")
        self.root.resizable(True, True)
        self.root.minsize(780, 420)

        self.building = False
        self.cancel_event = threading.Event()
        self.progress_queue = queue.Queue()

        # MSPA mirror state
        self._detected_slug = "homestuck"
        self._detected_offset = 1900

        # MSPFA state
        self._mspfa_story_id = None
        self._mspfa_start_page = 1

        self._build_ui()
        self._poll_queue()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        # ── Top: Title bar ──
        title_frame = ttk.Frame(main)
        title_frame.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(title_frame, text="MSPA-3DS Bundle Builder",
                  font=("Segoe UI", 13, "bold")).pack(side=tk.LEFT)
        ttk.Label(title_frame, text="MSPA \u2192 3DS",
                  font=("Segoe UI", 9)).pack(side=tk.RIGHT, pady=(4, 0))

        ttk.Separator(main).pack(fill=tk.X, pady=(0, 4))

        # ── Two-column layout: Left (config) | Right (progress/log) ──
        cols = ttk.Frame(main)
        cols.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(cols)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=False, padx=(0, 6))

        right = ttk.Frame(cols)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── LEFT COLUMN ──────────────────────────────────────────────

        # Source Tabs
        self.source_notebook = ttk.Notebook(left)
        self.source_notebook.pack(fill=tk.X, pady=(0, 4))

        # Tab 1: MSPA Mirror
        mspa_tab = ttk.Frame(self.source_notebook, padding=6)
        self.source_notebook.add(mspa_tab, text="  MSPA Mirror  ")

        url_row = ttk.Frame(mspa_tab)
        url_row.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(url_row, text="URL/slug:").pack(side=tk.LEFT)
        self.url_var = tk.StringVar(value="homestuck")
        url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        url_entry.bind("<Return>", lambda e: self._detect_comic())
        ttk.Button(url_row, text="Detect", command=self._detect_comic).pack(side=tk.RIGHT, padx=(4, 0))

        self.comic_info_var = tk.StringVar(value="Homestuck (offset: +1900)")
        ttk.Label(mspa_tab, textvariable=self.comic_info_var,
                  font=("Segoe UI", 8, "italic")).pack(anchor=tk.W)

        # Tab 2: MSPFA
        mspfa_tab = ttk.Frame(self.source_notebook, padding=6)
        self.source_notebook.add(mspfa_tab, text="  MSPFA  ")

        mspfa_row = ttk.Frame(mspfa_tab)
        mspfa_row.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(mspfa_row, text="Story ID:").pack(side=tk.LEFT)
        self.mspfa_url_var = tk.StringVar(value="")
        mspfa_entry = ttk.Entry(mspfa_row, textvariable=self.mspfa_url_var)
        mspfa_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        mspfa_entry.bind("<Return>", lambda e: self._detect_mspfa())
        ttk.Button(mspfa_row, text="Detect", command=self._detect_mspfa).pack(side=tk.RIGHT, padx=(4, 0))

        self.mspfa_info_var = tk.StringVar(value="Enter an MSPFA story ID or URL")
        ttk.Label(mspfa_tab, textvariable=self.mspfa_info_var,
                  font=("Segoe UI", 8, "italic")).pack(anchor=tk.W)

        # Page Range + Pack Name (compact, one row each)
        range_frame = ttk.LabelFrame(left, text="Settings", padding=6)
        range_frame.pack(fill=tk.X, pady=(0, 4))

        row1 = ttk.Frame(range_frame)
        row1.pack(fill=tk.X, pady=1)
        ttk.Label(row1, text="From:").pack(side=tk.LEFT)
        self.start_var = tk.StringVar(value="1")
        ttk.Entry(row1, textvariable=self.start_var, width=6).pack(side=tk.LEFT, padx=(2, 0))
        ttk.Label(row1, text="  To:").pack(side=tk.LEFT)
        self.end_var = tk.StringVar(value="21")
        ttk.Entry(row1, textvariable=self.end_var, width=6).pack(side=tk.LEFT, padx=(2, 0))
        ttk.Label(row1, text="(blank=end)", font=("Segoe UI", 7)).pack(side=tk.LEFT, padx=(4, 0))

        row2 = ttk.Frame(range_frame)
        row2.pack(fill=tk.X, pady=1)
        ttk.Label(row2, text="Pack name:").pack(side=tk.LEFT)
        self.name_var = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.name_var, width=16).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(row2, text="(auto if blank)", font=("Segoe UI", 7)).pack(side=tk.LEFT, padx=(4, 0))

        row3 = ttk.Frame(range_frame)
        row3.pack(fill=tk.X, pady=1)
        ttk.Label(row3, text="Output:").pack(side=tk.LEFT)
        self.output_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "MSPA-3DS-bundles"))
        ttk.Entry(row3, textvariable=self.output_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        ttk.Button(row3, text="...", command=self._browse_output, width=3).pack(side=tk.RIGHT)

        # Tools Status (compact, inline)
        tools_frame = ttk.LabelFrame(left, text="Tools", padding=4)
        tools_frame.pack(fill=tk.X, pady=(0, 4))

        ffmpeg_status = "\u2705" if HAS_FFMPEG else "\u274C"
        ruffle_status = "\u2705" if HAS_RUFFLE else "\u274C"
        ffdec_status = "\u2705" if HAS_FFDEC else "\u274C"
        ytdlp_status = "\u2705" if HAS_YT_DLP else "\u274C"
        yt_version = _yt_dlp_version() if HAS_YT_DLP else ""
        yt_label = f"yt-dlp {ytdlp_status}" if not yt_version else f"yt-dlp {yt_version} {ytdlp_status}"

        tools_row = ttk.Frame(tools_frame)
        tools_row.pack(fill=tk.X)
        ttk.Label(tools_row, text=f"Ruffle {ruffle_status}", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(tools_row, text=f"ffmpeg {ffmpeg_status}", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(tools_row, text=f"FFDec {ffdec_status}", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(tools_row, text=yt_label, font=("Segoe UI", 8)).pack(side=tk.LEFT)

        if not HAS_YT_DLP:
            ttk.Label(tools_frame,
                      text="yt-dlp needed for YouTube videos. pip install yt-dlp",
                      font=("Segoe UI", 7)).pack(anchor=tk.W, pady=(2, 0))
        elif _yt_dlp_is_stale(yt_version):
            # YouTube changes its API several times a year; stale yt-dlp is the
            # #1 cause of "Requested format is not available" failures (issue #10)
            ttk.Label(tools_frame,
                      text=f"yt-dlp {yt_version} is outdated \u2014 YouTube videos may fail. Update: pip install -U yt-dlp",
                      font=("Segoe UI", 7), foreground="#b45309").pack(anchor=tk.W, pady=(2, 0))

        if not HAS_RUFFLE:
            ruffle_hint = ("Ruffle renders flashes \u224850x faster than FFDec. " +
                           ("Set RUFFLE_AUTO_BUILD=1 to build it with cargo."
                            if HAS_FFDEC else
                            "Set RUFFLE_AUTO_BUILD=1 (needs Rust from rustup.rs) to build it."))
            ttk.Label(tools_frame, text=ruffle_hint,
                      font=("Segoe UI", 7), wraplength=240).pack(anchor=tk.W, pady=(2, 0))

        # Build / Cancel buttons
        btn_frame = ttk.Frame(left)
        btn_frame.pack(fill=tk.X, pady=(4, 0))
        self.build_btn = ttk.Button(btn_frame, text="\u25B6 Build", command=self._start_build)
        self.build_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        self.cancel_btn = ttk.Button(btn_frame, text="\u2716 Cancel", command=self._cancel_build, state="disabled")
        self.cancel_btn.pack(side=tk.RIGHT, padx=(6, 0), ipady=4)

        # ── RIGHT COLUMN ─────────────────────────────────────────────

        prog_frame = ttk.LabelFrame(right, text="Progress", padding=6)
        prog_frame.pack(fill=tk.BOTH, expand=True)

        status_row = ttk.Frame(prog_frame)
        status_row.pack(fill=tk.X)

        self.phase_label = ttk.Label(status_row, text="Ready", font=("Segoe UI", 9, "bold"))
        self.phase_label.pack(side=tk.LEFT)

        self.progress_bar = ttk.Progressbar(status_row, mode="determinate", length=200)
        self.progress_bar.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(8, 0))

        self.status_label = ttk.Label(prog_frame, text="Select a source and click Build.",
                                       font=("Segoe UI", 8), wraplength=500)
        self.status_label.pack(anchor=tk.W, pady=(2, 2))

        self.log_text = tk.Text(prog_frame, height=10, font=("Consolas", 8),
                                bg="#1e1e1e", fg="#cccccc", insertbackground="#cccccc",
                                state="disabled", wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        scrollbar = ttk.Scrollbar(self.log_text, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _detect_comic(self):
        """Parse the URL/slug field and update the detected comic info."""
        url = self.url_var.get().strip()
        slug, offset, start_page = parse_comic_url(url)
        
        if slug is None:
            self.comic_info_var.set("Unknown comic \u2014 enter a valid URL or slug")
            self._detected_slug = None
            self._detected_offset = 0
            return
        
        info = COMICS.get(slug)
        name = info["name"] if info else slug.title()
        offset_text = f"(offset: +{offset})" if offset else "(no offset)"
        self.comic_info_var.set(f"{name} {offset_text}")
        self._detected_slug = slug
        self._detected_offset = offset
        
        # Auto-fill start page from URL if it was specified
        if url and "/" in url and not url.rstrip("/").endswith(slug):
            self.start_var.set(str(start_page))
    
    def _detect_mspfa(self):
        """Parse the MSPFA URL/ID field, validate it via the API, and update the detected story info."""
        url = self.mspfa_url_var.get().strip()
        story_id, start_page = parse_mspfa_url(url)
        
        if story_id is None:
            self.mspfa_info_var.set("Not a valid MSPFA URL or story ID")
            self._mspfa_story_id = None
            return
        
        self.mspfa_info_var.set(f"Checking story #{story_id}...")
        self._mspfa_story_id = story_id
        self._mspfa_start_page = start_page
        
        # Auto-fill start page
        self.start_var.set(str(start_page))
        
        # Validate via the API in a background thread so the GUI doesn't freeze
        def check():
            import requests as req
            session = req.Session()
            session.headers.update({"User-Agent": "MSPA-3DS-Bundler/1.0", "Accept": "*/*"})
            story = mspfa_fetch_story(session, story_id)
            if story:
                name = story.get("n", "Unknown")
                pages = len(story.get("p", []))
                self.mspfa_info_var.set(f"Found: {name} ({pages} pages)")
                # Auto-fill end page with total
                self.end_var.set(str(pages))
            else:
                self.mspfa_info_var.set(f"Story #{story_id} not found or has no pages")
                self._mspfa_story_id = None
        
        threading.Thread(target=check, daemon=True).start()

    def _browse_output(self):
        path = filedialog.askdirectory(initialdir=self.output_var.get(), title="Select Output Folder")
        if path:
            self.output_var.set(path)

    def _log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")

    def _get_active_source(self):
        """Return the currently selected source tab: 'mspa' or 'mspfa'."""
        idx = self.source_notebook.index(self.source_notebook.select())
        return "mspa" if idx == 0 else "mspfa"

    def _start_build(self):
        if self.building:
            return
        
        source = self._get_active_source()
        
        try:
            start = int(self.start_var.get())
            end_raw = self.end_var.get().strip()
            end = int(end_raw) if end_raw else 0
            if end and end < start:
                messagebox.showerror("Invalid Range", "End page must be >= start page.")
                return
            name = self.name_var.get().strip() or None
        except ValueError:
            messagebox.showerror("Invalid Input", "Please enter valid page numbers.")
            return

        output_dir = self.output_var.get().strip()
        if not output_dir:
            messagebox.showerror("No Output", "Please select an output directory.")
            return
        os.makedirs(output_dir, exist_ok=True)

        if source == "mspa":
            self._start_build_mspa(start, end, name, output_dir)
        else:
            self._start_build_mspfa(start, end, name, output_dir)
    
    def _start_build_mspa(self, start, end, name, output_dir):
        """Start building from the MSPA mirror."""
        self._detect_comic()
        
        comic_slug = self._detected_slug
        if not comic_slug:
            messagebox.showerror("No Comic", 
                "Could not detect a comic from the URL/slug field.\n\n"
                "Enter a known slug (e.g. homestuck, jailbreak, problemsleuth) or a full URL.")
            return
        
        comic_name = COMICS.get(comic_slug, {}).get("name", comic_slug.title())
        
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state="disabled")
        self._log(f"Source: MSPA Mirror")
        self._log(f"Comic: {comic_name} ({comic_slug})")
        self._log(f"Building: pages {start}\u2013{end or 'end'}")
        self._log(f"Output: {output_dir}")
        self._log("")

        self.building = True
        self.cancel_event.clear()
        self.build_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress_bar["value"] = 0
        self.phase_label.config(text="Starting...")
        self.status_label.config(text="Building bundle...")

        def run():
            try:
                engine = ScraperEngine(self.progress_queue, self.cancel_event,
                                       comic_slug, self._detected_offset)
                engine.build_bundle(start, end, name, output_dir)
            except BuildCancelled:
                self.progress_queue.put({"msg": "Build cancelled.", "progress": None, "phase": "cancelled"})
            except Exception as e:
                self.progress_queue.put({"msg": f"ERROR: {e}", "progress": None, "phase": "error"})

        threading.Thread(target=run, daemon=True).start()
    
    def _start_build_mspfa(self, start, end, name, output_dir):
        """Start building from MSPFA."""
        self._detect_mspfa()
        
        story_id = self._mspfa_story_id
        if story_id is None:
            messagebox.showerror("No MSPFA Story",
                "Could not detect an MSPFA story.\n\n"
                "Enter a story ID (e.g. 27317) or a full URL\n"
                "(e.g. https://mspfa.com/?s=27317&p=1)")
            return
        
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state="disabled")
        self._log(f"Source: MSPFA")
        self._log(f"Story ID: {story_id}")
        self._log(f"Building: pages {start}\u2013{end or 'end'}")
        self._log(f"Output: {output_dir}")
        self._log("")

        self.building = True
        self.cancel_event.clear()
        self.build_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress_bar["value"] = 0
        self.phase_label.config(text="Starting...")
        self.status_label.config(text="Building MSPFA bundle...")

        def run():
            try:
                engine = MspfaScraperEngine(self.progress_queue, self.cancel_event, story_id)
                engine.build_bundle(start, end, name, output_dir)
            except BuildCancelled:
                self.progress_queue.put({"msg": "Build cancelled.", "progress": None, "phase": "cancelled"})
            except Exception as e:
                self.progress_queue.put({"msg": f"ERROR: {e}", "progress": None, "phase": "error"})

        threading.Thread(target=run, daemon=True).start()

    def _cancel_build(self):
        if self.building:
            self.cancel_event.set()
            self._log("Cancelling...")

    def _poll_queue(self):
        try:
            while True:
                item = self.progress_queue.get_nowait()
                msg = item.get("msg", "")
                progress = item.get("progress")
                phase = item.get("phase", "")

                if msg:
                    self.status_label.config(text=msg)
                    self._log(msg)
                if progress is not None:
                    self.progress_bar["value"] = progress

                phase_names = {
                    "scan": "\U0001F50D Scanning",
                    "scrape": "\U0001F4DD Scraping",
                    "download": "\U0001F4E5 Downloading",
                    "convert": "\U0001F3A8 Converting to .tex",
                    "package": "\U0001F4E6 Packaging",
                    "done": "\u2705 Done!",
                    "error": "\u274C Error",
                    "cancelled": "\u274C Cancelled",
                    "warn": "\u26A0\uFE0F Warning",
                }
                if phase in phase_names:
                    self.phase_label.config(text=phase_names[phase])
                if phase in ("done", "error", "cancelled"):
                    self.building = False
                    self.build_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)


def main():
    root = tk.Tk()
    root.geometry("880x460")
    app = MspaBuilderApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
