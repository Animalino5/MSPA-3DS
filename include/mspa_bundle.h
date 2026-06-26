#pragma once
#include <3ds.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* ── Bundle (folder pack) reader ─────────────────────────────────────────
 * Reads pack folders from sdmc:/3ds/MSPA-3DS/packs/<pack_id>/
 * Each folder contains:
 *   manifest.json
 *   pages/NNNNNN.json
 *   media/NNNNNN_N.gif (etc.)
 *
 * Files are read directly from the SD card — no decompression needed.
 */

typedef struct {
    char folderPath[256];        /* sdmc:/3ds/MSPA-3DS/packs/<pack_id> */
    char packId[64];
    char title[128];
    int  firstPage;
    int  lastPage;
    int  pageCount;
    char nextPack[64];
    char prevPack[64];
} MspaBundle;

/* Scan sdmc:/3ds/MSPA-3DS/packs/ for subdirectories containing
 * manifest.json. Opens each one, reads manifest, fills out[bundleIdx].
 * Returns number of packs found (0 if dir missing or empty). */
int mspa_bundle_scan_packs(MspaBundle *out, int maxPacks);

/* Open a specific pack folder and parse its manifest.json.
 * Returns true on success. */
bool mspa_bundle_open(MspaBundle *bundle, const char *folderPath);

/* Close a bundle (clears the struct). */
void mspa_bundle_close(MspaBundle *bundle);

/* Check if a file exists inside the bundle folder.
 * relativePath is like "pages/001901.json" or "media/001901_0.gif".
 * Returns true if the file exists. */
bool mspa_bundle_file_exists(MspaBundle *bundle, const char *relativePath);

/* Build the full path to a page JSON inside the bundle and verify it exists.
 * pathOut must be at least 256 bytes.
 * Returns true if the JSON file exists. */
bool mspa_bundle_get_page_json_path(MspaBundle *bundle, int globalPage,
                                     char *pathOut, size_t pathLen);

/* Build the full path to a media file inside the bundle and verify it exists.
 * relativePath is like "media/001901_0.gif".
 * pathOut must be at least 320 bytes.
 * Returns true if the media file exists. */
bool mspa_bundle_get_media_path(MspaBundle *bundle, const char *relativePath,
                                 char *pathOut, size_t pathLen);

/* Convenience: get the path to a page's JSON inside the bundle folder.
 * cachePathOut must be at least 256 bytes.
 * Returns true if the JSON file exists. */
bool mspa_bundle_ensure_page_json(MspaBundle *bundle, int globalPage,
                                   char *cachePathOut, size_t cachePathLen);

/* Convenience: ensure a media file is accessible at destPath.
 * With folder bundles, this copies from the pack folder to destPath
 * if destPath differs from the source. If destPath is NULL or empty,
 * just verifies the source exists. Returns true if the file is accessible. */
bool mspa_bundle_ensure_media(MspaBundle *bundle, const char *relativePath,
                               const char *destPath);
