# MSPA-3DS

A Homestuck (and other MSPA comics) reader for the Nintendo 3DS.

## What is this?

A homebrew app that lets you read MS Paint Adventures webcomics on your 3DS. It comes with a companion PC tool that downloads and pre-converts comic pages into a format the 3DS can display instantly! no internet connection needed on the console itself.

**Fully Supported Comics:**
- Jailbreak
- Problem Sleuth

**Incomplete Comics:**
- Homestuck (the ´[S]´ page video archive is incomplete so far, only featuring 3 pages. Any help would be appreciated!)

More can be added — the reader accepts any comic hosted on `mspa.chadthundercock.com`, and MSPFA support is planned.

## How does it work?

### On your PC: The Bundle Builder

Run the included Python GUI tool to download comic pages and package them into **bundles**:

1. Enter a comic URL or slug (e.g. `homestuck`, `jailbreak`, or a full URL)
2. Set the page range and pack name
3. Click **Build Bundle**
4. Copy the generated folder to your 3DS SD card

The builder handles everything:
- Downloads pages and images from the MSPA mirror
- Converts GIFs and images to 3DS-native `.tex` textures for instant loading
- Extracts `[S]` (Flash/animated) pages as frame sequences at 6 FPS with WAV audio
- Splits multi-image pages into separate navigable pages

### On your 3DS: The Reader

- Browse your installed packs from the main menu
- Navigate with **A** (next page), **B** (previous page), **X** (back to menu)
- Animated GIFs play automatically as frame sequences
- `[S]` pages play as animations with audio
- Scroll text on the bottom screen with **D-pad up/down**

## Installation

### 3DS App

1. Build the `.3dsx` using devkitARM and citro2d/citro3d
2. Copy `MSPA-3DS.3dsx` to `/3ds/` on your SD card

### Building from source

Requirements:
- [devkitARM](https://devkitpro.org/wiki/Getting_Started) (part of devkitPro)
- citro2d, citro3d, ctrulib
- libnsgif (included in `lib/libnsgif/`)

```bash
cd MSPA-3DS
make
```

The output is `MSPA-3DS.3dsx`.

### Bundle Builder (PC)

Requirements:
- Python 3.8+
- `pip install requests beautifulsoup4 Pillow`
- **ffmpeg** (required for `[S]` page conversion — install via your package manager)

Run it:
```bash
python build_gui.py
```

Or build a standalone executable:
```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "MSPA-3DS-Builder" build_gui.py
```

## Transferring bundles to your 3DS

After building a pack, copy its folder to:
```
sdmc:/3ds/MSPA-3DS/packs/<pack-name>/
```

The folder structure looks like:
```
packs/
  homestuck-1-100/
    manifest.json
    pages/
      190100.json
      190200.json
      ...
    media/
      001901_0-000.tex
      001901_0.anim
      001901.wav
      001902_0.gif
      001902_0-000.tex
      ...
```

## File format notes

| File | Description |
|------|-------------|
| `.tex` | 3DS GPU texture (format 0x80 = untiled RGBA, format 0x00 = GPU-tiled) |
| `.anim` | Animation manifest: frame count + per-frame delays (ms) |
| `.wav` | PCM audio for `[S]` pages |
| `.gif` | Original GIF (fallback for on-device conversion) |
| `.mpg` | Legacy video format (no longer used — frame sequences replace these) |

## Controls

| Button | Action |
|--------|--------|
| A | Next page (or next image on multi-image pages) |
| B | Previous page |
| X | Return to pack selection |
| D-pad Up/Down | Scroll text |

## Limitations

- `[S]` pages are converted to 6 FPS frame sequences. smooth but not full video quality
- Audio for `[S]` pages is extracted from the video file; some pages may have audio sync issues
- No internet connectivity from the 3DS. all content must be pre-built on PC
- Very long animations may use significant SD card space

## License

This project is open source. The Homestuck webcomic and all MSPA/MSPFA content belong to their respective creators.
