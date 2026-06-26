#!/usr/bin/env python3
"""
MSPA-3DS Bundle Builder — GUI Edition
======================================
A tkinter GUI wrapper for the MSPA-3DS scraper/packager.
Can be frozen into a standalone .exe with PyInstaller:

    pip install pyinstaller
    pyinstaller --onefile --windowed --name "MSPA-3DS-Builder" build_gui.py

Requires: pip install requests beautifulsoup4 Pillow
Optional: ffmpeg (for [S] page video conversion)
"""

import os
import sys
import time
import json
import struct
import re
import shutil
import subprocess
import threading
import queue
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

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

MIRROR_BASE = "https://mspa.chadthundercock.com"
FLASH_MP4_BASE = "http://file.garden/aQ9-6gw2fD_KMuI9/mspa-3ds/"
FLASH_WAV_BASE = "http://file.garden/aQ9-6gw2fD_KMuI9/mspa-3ds/"
BUNDLE_SCHEMA = 4
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
COMICS = {
    "jailbreak":       {"name": "Jailbreak",              "offset": 0},
    "bard-quest":      {"name": "Bard Quest",             "offset": 0},
    "problemsleuth":   {"name": "Problem Sleuth",         "offset": 0},
    "beta":            {"name": "Homestuck Beta",          "offset": 0},
    "homestuck":       {"name": "Homestuck",              "offset": 1900},
}

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

# Detect external tools
HAS_FFMPEG = shutil.which("ffmpeg") is not None


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
        if "homestuck.com" in src or "storage.homestuck.com" in src:
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

def is_media_url_allowed(url):
    try:
        path = urlparse(url).path.lower()
        _, ext = os.path.splitext(path)
        return ext in ALLOWED_EXTENSIONS
    except Exception:
        return False

def extract_media_urls(soup, global_page, html, comic_slug):
    if looks_like_flash(html):
        # [S] pages: download pre-converted MP4 from archive
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
            if html is None: continue

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
                        ext = ".mp4"  # [S] pages use pre-converted MP4s
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
                # [S] page: download MP4 from file.garden, extract frame sequence with ffmpeg
                if self.download_media(item["url"], fs_path):
                    downloaded += 1
                    if HAS_FFMPEG:
                        # Extract frames at 6 FPS → .tex + .anim + .wav
                        output_base = os.path.splitext(fs_path)[0]
                        # Audio path: same page number, .wav extension
                        global_page = item.get("global_page", 0)
                        wav_path = os.path.join(bundle_dir, f"media/{global_page:06d}.wav")
                        
                        frame_count, delays = convert_mp4_to_frames(
                            fs_path, output_base, wav_path, fps=6)
                        
                        if frame_count > 0:
                            # Update page data: change media reference to .gif
                            # so the 3DS treats it as an animation with pre-converted .tex
                            new_rel = os.path.splitext(item["local_path"])[0] + ".gif"
                            vpage = item["vpage"]
                            if vpage in pages_data:
                                pages_data[vpage]["media"] = [new_rel]
                                # Also set audio to the extracted WAV
                                if os.path.exists(wav_path):
                                    pages_data[vpage]["audio"] = f"media/{global_page:06d}.wav"
                            # Delete the MP4 — we've extracted everything we need
                            try: os.remove(fs_path)
                            except: pass
                            converted_count = frame_count  # track for logging
                        else:
                            self._post(f"Warning: frame extraction failed for {os.path.basename(fs_path)}", None, "warn")
                    else:
                        self._post(f"Warning: no ffmpeg, [S] page will have no frames", None, "warn")
                        # Can't do anything useful without ffmpeg, remove the MP4
                        try: os.remove(fs_path)
                        except: pass
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
        first_vpage = min(pages_data.keys()) if pages_data else 0
        last_vpage = max(pages_data.keys()) if pages_data else 0

        comic_name = COMICS.get(self.comic_slug, {}).get("name", self.comic_slug.title())
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


# ═══════════════════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════════════════

class MspaBuilderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("MSPA-3DS Bundle Builder")
        self.root.resizable(True, True)
        self.root.minsize(540, 680)

        self.building = False
        self.cancel_event = threading.Event()
        self.progress_queue = queue.Queue()

        # Default detected comic (Homestuck)
        self._detected_slug = "homestuck"
        self._detected_offset = 1900

        self._build_ui()
        self._poll_queue()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=15)
        main.pack(fill=tk.BOTH, expand=True)

        # Title
        title_frame = ttk.Frame(main)
        title_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(title_frame, text="MSPA-3DS Bundle Builder",
                  font=("Segoe UI", 16, "bold")).pack(side=tk.LEFT)
        ttk.Label(title_frame, text="MSPA \u2192 3DS",
                  font=("Segoe UI", 10)).pack(side=tk.RIGHT, pady=(8, 0))

        ttk.Separator(main).pack(fill=tk.X, pady=(0, 8))

        # ── Comic Source ──
        src_frame = ttk.LabelFrame(main, text="Comic Source", padding=10)
        src_frame.pack(fill=tk.X, pady=(0, 6))

        url_row = ttk.Frame(src_frame)
        url_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(url_row, text="URL or comic slug:").pack(side=tk.LEFT)
        self.url_var = tk.StringVar(value="homestuck")
        url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        url_entry.bind("<Return>", lambda e: self._detect_comic())
        ttk.Button(url_row, text="Detect", command=self._detect_comic).pack(side=tk.RIGHT, padx=(6, 0))

        # Detected comic info
        self.comic_info_var = tk.StringVar(value="Homestuck (offset: +1900)")
        ttk.Label(src_frame, textvariable=self.comic_info_var,
                  font=("Segoe UI", 9, "italic")).pack(anchor=tk.W)

        ttk.Label(src_frame, text="Examples: homestuck, jailbreak, problemsleuth, bard-quest, beta",
                  font=("Segoe UI", 8)).pack(anchor=tk.W, pady=(2, 0))
        ttk.Label(src_frame, text="Or paste a full URL: https://mspa.chadthundercock.com/jailbreak/1",
                  font=("Segoe UI", 8)).pack(anchor=tk.W)

        # ── Page Range ──
        act_frame = ttk.LabelFrame(main, text="Page Range", padding=10)
        act_frame.pack(fill=tk.X, pady=(0, 6))

        row1 = ttk.Frame(act_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="From page:").pack(side=tk.LEFT)
        self.start_var = tk.StringVar(value="1")
        ttk.Entry(row1, textvariable=self.start_var, width=8).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(row1, text="To page:").pack(side=tk.LEFT, padx=(12, 0))
        self.end_var = tk.StringVar(value="21")
        ttk.Entry(row1, textvariable=self.end_var, width=8).pack(side=tk.LEFT, padx=(4, 0))
        self.page_num_hint = ttk.Label(row1, text="(comic page numbers, 1-based)",
                  font=("Segoe UI", 8))
        self.page_num_hint.pack(side=tk.LEFT, padx=(8, 0))

        row2 = ttk.Frame(act_frame)
        row2.pack(fill=tk.X, pady=(6, 2))
        ttk.Label(row2, text="Pack name:").pack(side=tk.LEFT)
        self.name_var = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.name_var, width=18).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(row2, text="(optional, auto-generated if empty)",
                  font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(4, 0))

        # ── Tools Status ──
        tools_frame = ttk.LabelFrame(main, text="External Tools", padding=10)
        tools_frame.pack(fill=tk.X, pady=(0, 6))

        tools_inner = ttk.Frame(tools_frame)
        tools_inner.pack(fill=tk.X)

        ffmpeg_status = "\u2705 Detected" if HAS_FFMPEG else "\u274C Not found"

        ttk.Label(tools_inner, text=f"ffmpeg: {ffmpeg_status}", font=("Segoe UI", 9)).pack(anchor=tk.W)
        if not HAS_FFMPEG:
            ttk.Label(tools_inner, text="ffmpeg is needed to convert [S] page videos for 3DS.",
                      font=("Segoe UI", 8)).pack(anchor=tk.W)
            ttk.Label(tools_inner, text="Install with: sudo apt install ffmpeg (or brew install ffmpeg)",
                      font=("Segoe UI", 8)).pack(anchor=tk.W)
        else:
            ttk.Label(tools_inner, text="[S] page video conversion is available.",
                      font=("Segoe UI", 8)).pack(anchor=tk.W)

        # ── Output Directory ──
        out_frame = ttk.LabelFrame(main, text="Output", padding=10)
        out_frame.pack(fill=tk.X, pady=(0, 6))

        out_row = ttk.Frame(out_frame)
        out_row.pack(fill=tk.X)
        self.output_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "MSPA-3DS-bundles"))
        ttk.Entry(out_row, textvariable=self.output_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(out_row, text="Browse...", command=self._browse_output).pack(side=tk.RIGHT)

        # ── Build Button ──
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=(6, 6))
        self.build_btn = ttk.Button(btn_frame, text="\u25B6  Build Bundle", command=self._start_build)
        self.build_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)
        self.cancel_btn = ttk.Button(btn_frame, text="\u2716  Cancel", command=self._cancel_build, state="disabled")
        self.cancel_btn.pack(side=tk.RIGHT, padx=(8, 0), ipady=6)

        # ── Progress ──
        prog_frame = ttk.LabelFrame(main, text="Progress", padding=10)
        prog_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 0))

        self.phase_label = ttk.Label(prog_frame, text="Ready", font=("Segoe UI", 9, "bold"))
        self.phase_label.pack(anchor=tk.W)

        self.progress_bar = ttk.Progressbar(prog_frame, mode="determinate", length=400)
        self.progress_bar.pack(fill=tk.X, pady=(4, 4))

        self.status_label = ttk.Label(prog_frame, text="Select an act and click Build Bundle.",
                                       font=("Segoe UI", 9), wraplength=460)
        self.status_label.pack(anchor=tk.W)

        self.log_text = tk.Text(prog_frame, height=8, font=("Consolas", 8),
                                bg="#1e1e1e", fg="#cccccc", insertbackground="#cccccc",
                                state="disabled", wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        scrollbar = ttk.Scrollbar(self.log_text, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _detect_comic(self):
        """Parse the URL/slug field and update the detected comic info."""
        url = self.url_var.get().strip()
        slug, offset, start_page = parse_comic_url(url)
        
        if slug is None:
            self.comic_info_var.set("Unknown comic — enter a valid URL or slug")
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
    
    def _browse_output(self):
        path = filedialog.askdirectory(initialdir=self.output_var.get(), title="Select Output Folder")
        if path:
            self.output_var.set(path)

    def _log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")

    def _start_build(self):
        if self.building:
            return
        
        # Re-detect comic from URL field (in case user changed it without pressing Detect)
        self._detect_comic()
        
        comic_slug = self._detected_slug
        if not comic_slug:
            messagebox.showerror("No Comic", 
                "Could not detect a comic from the URL/slug field.\n\n"
                "Enter a known slug (e.g. homestuck, jailbreak, problemsleuth) or a full URL.")
            return
        
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

        comic_name = COMICS.get(comic_slug, {}).get("name", comic_slug.title())
        
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state="disabled")
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
    root.geometry("560x720")
    app = MspaBuilderApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
