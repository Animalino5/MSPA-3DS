# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the MSPA-3DS Bundle Builder (Windows .exe)
#
# Build (see BUILD-WINDOWS-EXE.md for the full Docker/Wine recipe):
#
#     pyinstaller --clean --noconfirm mspa3ds-builder.spec
#
# Produces dist/MSPA-3DS-Builder.exe — a single self-contained file with
# Python + tkinter + requests + beautifulsoup4 + Pillow and (if present
# next to build_gui.py when you build) the ruffle-exporter folder and the
# optional ffdec / yt-dlp.exe tools.
#
# NOTE for the ruffle exporter: on Windows the binary must be
# ruffle-exporter/ruffle_exporter.exe (the Linux ELF binary of the same
# name is ignored). Build it with build_ruffle_windows.sh (from Linux) or
# `cargo build --release -p exporter` (on a Windows machine) — both use
# the exact patched sources embedded in build_gui.py.

import os

datas = []

def _add(src, dest=None):
    """Bundle a folder/file if it exists next to build_gui.py."""
    if os.path.isdir(src):
        datas.append((src, dest or src))
    elif os.path.isfile(src):
        datas.append((src, dest or '.'))

_add('ruffle-exporter')   # ruffle_exporter(.exe) — see build_ruffle_windows.sh
_add('ffdec')             # optional; requires Java on the target machine
_add('yt-dlp.exe')        # optional; download from yt-dlp GitHub releases

a = Analysis(
    ['build_gui.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MSPA-3DS-Builder',
    debug=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,            # windowed (tkinter GUI — no console pop-up)
    disable_windowed_traceback=False,
    icon=None,                # set to 'icon.ico' if you have one
)
