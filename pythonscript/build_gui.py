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
MSPFA_BASE = "https://mspfa.com"
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
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    search_dirs = [
        script_dir,
        os.path.join(script_dir, "ffdec"),
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
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    target_dir = os.path.join(script_dir, "ffdec")
    
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

def is_media_url_allowed(url):
    try:
        path = urlparse(url).path.lower()
        _, ext = os.path.splitext(path)
        return ext in ALLOWED_EXTENSIONS
    except Exception:
        return False

def extract_media_urls(soup, global_page, html, comic_slug):
    if looks_like_flash(html):
        # [S] page: try to find SWF URL in the HTML first
        swf_urls = find_swf_urls(html)
        if swf_urls and HAS_FFDEC:
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
      - [img]URL[/img]                    → image (GIF/PNG/JPEG)
      - [flash]URL[/flash]                → SWF animation
      - [flash=WxH]URL[/flash]            → SWF with dimensions
      - <iframe src="...youtube/embed/ID"> → YouTube video
      - <video src="URL">                 → direct video (MP4/WebM)
      - <video><source src="URL"></video>  → direct video with source tag
    
    Returns (urls, media_type) where media_type is one of:
      'image'  — static image, use convert_gif_to_tex
      'swf'    — Flash animation, use convert_swf_to_frames (FFDec)
      'video'  — direct video file, use convert_mp4_to_frames (ffmpeg)
      'youtube' — YouTube video, use convert_youtube_to_frames (yt-dlp)
    """
    # Check for [flash] tags (SWF) — takes priority
    for m in re.finditer(r'\[flash(?:=\d*?x\d*?)?\](.+?)\[/flash\]', body, re.IGNORECASE):
        url = m.group(1).strip()
        if url:
            return [url], 'swf'
    
    # Check for <iframe> YouTube embeds
    # MSPFA users embed YouTube via: <iframe src="https://www.youtube.com/embed/VIDEO_ID">
    yt_match = re.search(
        r'<iframe[^>]+src=["\'](?:https?://)?(?:www\.)?youtube\.com/embed/([\w-]{11})',
        body, re.IGNORECASE
    )
    if not yt_match:
        # Also check youtu.be short URLs
        yt_match = re.search(
            r'<iframe[^>]+src=["\'](?:https?://)?youtu\.be/([\w-]{11})',
            body, re.IGNORECASE
        )
    if yt_match:
        video_id = yt_match.group(1)
        return [video_id], 'youtube'
    
    # Check for <video> tags with direct video URLs
    # Pattern: <video src="URL"> or <video><source src="URL">
    video_match = re.search(
        r'<video[^>]*>.*?<source[^>]+src=["\']([^"\']+)["\']',
        body, re.IGNORECASE | re.DOTALL
    )
    if not video_match:
        video_match = re.search(
            r'<video[^>]+src=["\']([^"\']+)["\']',
            body, re.IGNORECASE
        )
    if video_match:
        url = video_match.group(1).strip()
        if url:
            return [url], 'video'
    
    # Default: [img] tags
    urls = []
    for m in re.finditer(r'\[img(?:=\d*?x\d*?)?\](.+?)\[/img\]', body, re.IGNORECASE):
        url = m.group(1).strip()
        if url and is_media_url_allowed(url):
            urls.append(url)
    
    return urls, 'image'


def mspfa_parse_text(body):
    """Extract plain text from MSPFA BBCode body text.
    
    Strips all BBCode tags and returns a list of non-empty lines.
    """
    text = body
    # Remove [img]...[/img] entirely (images aren't text)
    text = re.sub(r'\[img\].+?\[/img\]', '', text, flags=re.IGNORECASE)
    # Remove [flash]...[/flash] entirely
    text = re.sub(r'\[flash\].+?\[/flash\]', '', text, flags=re.IGNORECASE)
    # Remove [url=...]...[/url] — keep the link text only
    text = re.sub(r'\[url=[^\]]*\](.+?)\[/url\]', r'\1', text, flags=re.IGNORECASE)
    # Remove all other BBCode tags
    text = re.sub(r'\[/?(?:b|i|u|s|size=\d*?|color=[^]]*?|spoiler|alt|user|background=[^]]*?|font=[^]]*?|left|center|right|justify)\]', '', text, flags=re.IGNORECASE)
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
# SWF → FRAME SEQUENCE CONVERSION (FFDec + ffmpeg)
# ═══════════════════════════════════════════════════════════════════════════════

def convert_swf_to_frames(swf_path, output_base, wav_path, fps=6, log=None, progress=None):
    """
    Convert an SWF file into a frame sequence for 3DS playback.
    
    Pipeline:
      1. FFDec: SWF → AVI (frames) + WAV (sound)
      2. ffmpeg: AVI → PNG sequence at target FPS
      3. Pillow: each PNG → .tex (3DS texture format)
      4. Write .anim manifest
      5. ffmpeg: resample WAV to 44100Hz stereo for 3DS
    
    log: callable(msg) for debug/status messages
    progress: callable(percent) for progress updates (0-100)
    
    Returns (frame_count, delays_ms) or (0, []) on failure.
    """
    if not HAS_FFDEC or not HAS_FFMPEG:
        return 0, []
    
    def _log(msg):
        if log:
            log(msg)
        else:
            print(f"[SWF] {msg}")
    
    def _progress(pct):
        if progress:
            progress(pct)
    
    import tempfile
    
    tmpdir = tempfile.mkdtemp(prefix="mspa3ds_swf_")
    sound_tmpdir = tempfile.mkdtemp(prefix="mspa3ds_swf_snd_")
    frame_count = 0
    delays_ms = []
    
    try:
        # Build the FFDec command
        if FFDEC_JAR == "ffdec":
            ffdec_cmd = ["ffdec"]
            ffdec_cwd = None
        else:
            ffdec_cmd = ["java", "-jar", FFDEC_JAR]
            ffdec_cwd = os.path.dirname(FFDEC_JAR)
        
        swf_name = os.path.basename(swf_path)
        swf_size = os.path.getsize(swf_path)
        
        # ── Step 1: FFDec → AVI (this is the slowest step) ──
        _log(f"Step 1/5: FFDec extracting frames from {swf_name} ({swf_size//1024}KB)")
        _progress(5)
        
        try:
            result = subprocess.run(
                ffdec_cmd + [
                    "-onerror", "ignore",
                    "-format", "frame:avi",
                    "-export", "frame",
                    tmpdir,
                    swf_path
                ],
                capture_output=True, timeout=600, cwd=ffdec_cwd
            )
            _log(f"  FFDec finished (rc={result.returncode})")
            if result.stderr:
                stderr_text = result.stderr.decode(errors="replace")[:300]
                if stderr_text.strip():
                    _log(f"  FFDec stderr: {stderr_text}")
        except (subprocess.TimeoutExpired, Exception) as e:
            _log(f"  FFDec FAILED: {e}")
            return 0, []
        
        _progress(30)
        
        # Find the AVI file
        avi_path = None
        for fname in os.listdir(tmpdir):
            if fname.lower().endswith(".avi"):
                avi_path = os.path.join(tmpdir, fname)
                break
        
        if not avi_path or not os.path.isfile(avi_path):
            _log(f"  No AVI produced! Files: {os.listdir(tmpdir)}")
            return 0, []
        
        avi_size = os.path.getsize(avi_path)
        _log(f"  AVI: {os.path.basename(avi_path)} ({avi_size//1024}KB)")
        
        # ── Step 2: FFDec → WAV (sound extraction) ──
        _log(f"Step 2/5: FFDec extracting audio")
        _progress(35)
        
        raw_wav_path = None
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
                capture_output=True, timeout=120, cwd=ffdec_cwd
            )
            for fname in os.listdir(sound_tmpdir):
                if fname.lower().endswith(".wav"):
                    raw_wav_path = os.path.join(sound_tmpdir, fname)
                    break
            if raw_wav_path:
                _log(f"  Audio: {os.path.basename(raw_wav_path)}")
            else:
                _log(f"  No audio found (silent SWF)")
        except (subprocess.TimeoutExpired, Exception):
            _log(f"  Audio extraction failed (optional)")
        
        _progress(45)
        
        # ── Step 3: ffmpeg → PNG frames at target FPS ──
        _log(f"Step 3/5: ffmpeg extracting {fps}fps frames from AVI")
        _progress(50)
        
        frame_pattern = os.path.join(tmpdir, "frame_%04d.png")
        
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", avi_path,
                 "-vf", f"fps={fps},scale='min({PANEL_MAX_W},iw)':'min({PANEL_MAX_H},ih)':force_original_aspect_ratio=decrease,pad={PANEL_MAX_W}:{PANEL_MAX_H}:(ow-iw)/2:(oh-ih)/2",
                 frame_pattern],
                capture_output=True, timeout=600
            )
        except (subprocess.TimeoutExpired, Exception) as e:
            _log(f"  ffmpeg FAILED: {e}")
            return 0, []
        
        _progress(60)
        
        # ── Step 4: Convert each PNG → .tex ──
        frame_files = sorted([f for f in os.listdir(tmpdir) if f.startswith("frame_") and f.endswith(".png")])
        if not frame_files:
            _log(f"  No PNG frames extracted!")
            _log(f"  ffmpeg stderr: {result.stderr.decode(errors='replace')[-300:]}")
            return 0, []
        
        total_frames = len(frame_files)
        _log(f"Step 4/5: Converting {total_frames} frames to .tex")
        
        delay_ms = int(1000.0 / fps)
        
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
            
            # Report progress every 10% of frames
            if total_frames > 10 and idx % max(1, total_frames // 10) == 0:
                pct = 60 + int((idx / total_frames) * 30)
                _progress(pct)
        
        if frame_count == 0:
            _log(f"  No frames converted!")
            return 0, []
        
        _log(f"  Converted {frame_count}/{total_frames} frames")
        _progress(92)
        
        # ── Step 5: Write .anim + resample audio ──
        _log(f"Step 5/5: Writing .anim manifest")
        write_anim_file(f"{output_base}.anim", frame_count, delays_ms)
        
        if raw_wav_path and wav_path:
            _log(f"  Resampling audio to 44100Hz stereo")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", raw_wav_path,
                     "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
                     wav_path],
                    capture_output=True, timeout=60
                )
            except (subprocess.TimeoutExpired, Exception):
                pass
        
        _progress(100)
        _log(f"Done! {frame_count} frames, {len(delays_ms)} delays")
        return frame_count, delays_ms
    
    finally:
        # Clean up temp directories
        for d in [tmpdir, sound_tmpdir]:
            try:
                for f in os.listdir(d):
                    os.remove(os.path.join(d, f))
                os.rmdir(d)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# YOUTUBE → FRAME SEQUENCE CONVERSION (yt-dlp + ffmpeg)
# ═══════════════════════════════════════════════════════════════════════════════

def convert_youtube_to_frames(video_id, output_base, wav_path, fps=6, log=None, progress=None):
    """
    Download a YouTube video and convert it to a 3DS frame sequence.
    
    Pipeline:
      1. yt-dlp: download video as MP4 (best quality up to 720p)
      2. ffmpeg: MP4 → PNG sequence at target FPS
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
        
        try:
            result = subprocess.run(
                [YT_DLP_PATH,
                 "-f", "best[height<=720][ext=mp4]/best[height<=720]/best",
                 "-o", mp4_path,
                 "--no-playlist",
                 "--no-warnings",
                 url],
                capture_output=True, timeout=300
            )
            if result.returncode != 0:
                _log(f"  yt-dlp failed: {result.stderr.decode(errors='replace')[:200]}")
                return 0, []
        except (subprocess.TimeoutExpired, Exception) as e:
            _log(f"  yt-dlp exception: {e}")
            return 0, []
        
        if not os.path.isfile(mp4_path):
            _log(f"  No MP4 downloaded!")
            return 0, []
        
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
                is_swf = item["url"].lower().endswith(".swf")
                if self.download_media(item["url"], fs_path):
                    downloaded += 1
                    output_base = os.path.splitext(fs_path)[0]
                    global_page = item.get("global_page", 0)
                    wav_path = os.path.join(bundle_dir, f"media/{global_page:06d}.wav")
                    
                    frame_count = 0
                    
                    if is_swf and HAS_FFDEC:
                        # SWF → FFDec → AVI + WAV → ffmpeg → frames
                        self._post(f"Converting SWF: {os.path.basename(fs_path)}", None, "convert")
                        frame_count, delays = convert_swf_to_frames(
                            fs_path, output_base, wav_path, fps=6,
                            log=lambda m: self._post(f"[SWF] {m}", None, "convert"),
                            progress=lambda p: self._post(f"SWF conversion: {p}%", p, "convert"))
                    elif HAS_FFMPEG:
                        # MP4 → ffmpeg → frames (filegarden fallback)
                        frame_count, delays = convert_mp4_to_frames(
                            fs_path, output_base, wav_path, fps=6)
                    else:
                        self._post(f"Warning: no FFDec/ffmpeg, [S] page will have no frames", None, "warn")
                    
                    if frame_count > 0:
                        # Update page data: change media reference to .gif
                        # so the 3DS treats it as an animation with pre-converted .tex
                        new_rel = os.path.splitext(item["local_path"])[0] + ".gif"
                        vpage = item["vpage"]
                        if vpage in pages_data:
                            pages_data[vpage]["media"] = [new_rel]
                            if os.path.exists(wav_path):
                                pages_data[vpage]["audio"] = f"media/{global_page:06d}.wav"
                        # Delete the source file — we've extracted everything
                        try: os.remove(fs_path)
                        except: pass
                    else:
                        self._post(f"Warning: frame extraction failed for {os.path.basename(fs_path)}", None, "warn")
                        try: os.remove(fs_path)
                        except: pass
                else:
                    self._post(f"Warning: could not download {os.path.basename(fs_path)}", None, "warn")
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
            
            command = page_data.get("c", "")
            body = page_data.get("b", "")
            next_pages = page_data.get("n", [])
            
            # Extract images and text from BBCode body
            media_urls, media_type = mspfa_parse_images(body)
            text_lines = mspfa_parse_text(body)
            
            # Find audio for this page from CSS directives
            audio_url = mspfa_find_audio(story_css, page_num)
            
            vpage = self._vpage(page_num)
            n_media = len(media_urls)
            
            # Next virtual page: first entry in the 'n' array
            next_global = 0
            if next_pages and next_pages[0] > 0 and next_pages[0] <= total_pages:
                next_global = self._vpage(next_pages[0])
            
            # Determine extension and flags based on media type
            is_flash = (media_type == 'swf')
            is_video = (media_type == 'video')
            is_youtube = (media_type == 'youtube')
            has_extractable_media = is_flash or is_video or is_youtube
            
            if n_media <= 1:
                # Single image (or no image) — one page
                local_media = []
                if n_media == 1:
                    url = media_urls[0]
                    if is_youtube:
                        ext = ".mp4"  # YouTube videos download as MP4
                    elif is_flash:
                        ext = ".swf"
                    elif is_video:
                        ext = _get_ext(url) or ".mp4"
                    else:
                        ext = _get_ext(url) or ".gif"
                    local_path = f"media/{vpage:06d}_0{ext}"
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
                    "type": command if command.startswith("[S]") else "PAGE",
                    "alt": "",
                    "command": command,
                    "audio": local_audio,
                    "media": local_media,
                    "text": text_lines,
                }
            else:
                # Multiple images — split into sub-pages (same as MSPA scraper)
                for mi, url in enumerate(media_urls):
                    sub_vpage = vpage + mi
                    sub_next = vpage + mi + 1 if mi < n_media - 1 else next_global
                    
                    ext = _get_ext(url) or ".gif"
                    local_path = f"media/{vpage:06d}_{mi}{ext}"
                    local_media = [local_path]
                    
                    media_downloads.append({
                        "url": url,
                        "local_path": local_path,
                        "kind": "media",
                        "vpage": sub_vpage,
                        "is_video": ext in (".mpg", ".mpeg", ".mp4"),
                    })
                    
                    local_audio = ""
                    if mi == n_media - 1 and audio_url:
                        local_audio = f"media/{vpage:06d}.wav"
                        media_downloads.append({
                            "url": audio_url,
                            "local_path": local_audio,
                            "kind": "audio",
                            "vpage": sub_vpage,
                            "is_video": False,
                        })
                    
                    pages_data[sub_vpage] = {
                        "schema": BUNDLE_SCHEMA,
                        "page": sub_vpage,
                        "next": sub_next,
                        "type": command if command.startswith("[S]") else "PAGE",
                        "alt": "",
                        "command": command,
                        "audio": local_audio,
                        "media": local_media,
                        "text": text_lines if mi == 0 else [],
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
            
            # MSPFA audio is often .mp3 but the 3DS needs .wav
            if item["kind"] == "audio":
                src_ext = _get_ext(item["url"]) or ".mp3"
                tmp_path = fs_path + ".tmp" + src_ext
                if self.download_media(item["url"], tmp_path):
                    downloaded += 1
                    if src_ext.lower() != ".wav" and HAS_FFMPEG:
                        try:
                            result = subprocess.run(
                                ["ffmpeg", "-y", "-i", tmp_path,
                                 "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
                                 fs_path],
                                capture_output=True, timeout=60
                            )
                            if result.returncode == 0:
                                try: os.remove(tmp_path)
                                except: pass
                            else:
                                try: os.rename(tmp_path, fs_path)
                                except: pass
                                self._post(f"Warning: audio conversion failed, keeping original format", None, "warn")
                        except (subprocess.TimeoutExpired, Exception):
                            try: os.rename(tmp_path, fs_path)
                            except: pass
                    else:
                        try: os.rename(tmp_path, fs_path)
                        except: pass
                else:
                    vpage = item["vpage"]
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
                
                if frame_count > 0:
                    new_rel = os.path.splitext(item["local_path"])[0] + ".gif"
                    vpage = item["vpage"]
                    if vpage in pages_data:
                        pages_data[vpage]["media"] = [new_rel]
                        if os.path.exists(wav_path):
                            pages_data[vpage]["audio"] = wav_path.split("/", 1)[-1] if "/" in wav_path else wav_path
                    downloaded += 1
                else:
                    self._post(f"Warning: YouTube conversion failed for {video_id}", None, "warn")
                
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
                    
                    if item.get("is_flash") and HAS_FFDEC:
                        self._post(f"Converting SWF: {os.path.basename(fs_path)}", None, "convert")
                        frame_count, delays = convert_swf_to_frames(
                            fs_path, output_base, wav_path, fps=6,
                            log=lambda m: self._post(f"[SWF] {m}", None, "convert"),
                            progress=lambda p: self._post(f"SWF conversion: {p}%", p, "convert"))
                    elif HAS_FFMPEG:
                        self._post(f"Converting video: {os.path.basename(fs_path)}", None, "convert")
                        frame_count, delays = convert_mp4_to_frames(
                            fs_path, output_base, wav_path, fps=6)
                    
                    if frame_count > 0:
                        new_rel = os.path.splitext(item["local_path"])[0] + ".gif"
                        vpage = item["vpage"]
                        if vpage in pages_data:
                            pages_data[vpage]["media"] = [new_rel]
                            if os.path.exists(wav_path):
                                pages_data[vpage]["audio"] = wav_path.split("/", 1)[-1] if "/" in wav_path else wav_path
                        try: os.remove(fs_path)
                        except: pass
                    else:
                        self._post(f"Warning: conversion failed for {os.path.basename(fs_path)}", None, "warn")
                        try: os.remove(fs_path)
                        except: pass
                else:
                    self._post(f"Warning: could not download {os.path.basename(fs_path)}", None, "warn")
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
        ffdec_status = "\u2705" if HAS_FFDEC else "\u274C"
        ytdlp_status = "\u2705" if HAS_YT_DLP else "\u274C"

        tools_row = ttk.Frame(tools_frame)
        tools_row.pack(fill=tk.X)
        ttk.Label(tools_row, text=f"ffmpeg {ffmpeg_status}", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(tools_row, text=f"FFDec {ffdec_status}", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(tools_row, text=f"yt-dlp {ytdlp_status}", font=("Segoe UI", 8)).pack(side=tk.LEFT)

        if not HAS_YT_DLP:
            ttk.Label(tools_frame,
                      text="yt-dlp needed for YouTube videos. pip install yt-dlp",
                      font=("Segoe UI", 7)).pack(anchor=tk.W, pady=(2, 0))

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
