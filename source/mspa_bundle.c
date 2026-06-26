/*
 * mspa_bundle.c — Folder bundle reader for MSPA-3DS
 *
 * Reads pack folders from sdmc:/3ds/MSPA-3DS/packs/<pack_id>/
 * Each folder contains:
 *   manifest.json
 *   pages/NNNNNN.json
 *   media/NNNNNN_N.gif (etc.)
 *
 * Since files are already on the SD as plain files, no decompression
 * is needed — we just build paths and check existence.
 */

#include "mspa_bundle.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <dirent.h>

#define PACKS_DIR  "sdmc:/3ds/MSPA-3DS/packs"

/* Helper: extract a string value from JSON by key (inline) */
#define auto_str_val(json, key, dest, destLen) do { \
    const char *_k = "\"" key "\""; \
    const char *_p = strstr(json, _k); \
    if (_p) { \
        _p = strchr(_p + strlen(_k), '"'); \
        if (_p) { \
            _p++; \
            const char *_e = strchr(_p, '"'); \
            if (_e) { \
                size_t _len = (size_t)(_e - _p); \
                if (_len >= destLen) _len = destLen - 1; \
                memcpy(dest, _p, _len); \
                dest[_len] = '\0'; \
            } \
        } \
    } \
} while(0)

/* ── Directory helpers ──────────────────────────────────────────────── */

static bool dir_exists(const char *path) {
    struct stat st;
    return (stat(path, &st) == 0 && S_ISDIR(st.st_mode));
}

static bool file_exists(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    fclose(f);
    return true;
}

/* ═══════════════════════════════════════════════════════════════════════
 * PUBLIC API
 * ═══════════════════════════════════════════════════════════════════════ */

int mspa_bundle_scan_packs(MspaBundle *out, int maxPacks) {
    mkdir("sdmc:/3ds", 0777);
    mkdir("sdmc:/3ds/MSPA-3DS", 0777);
    mkdir(PACKS_DIR, 0777);

    DIR *dir = opendir(PACKS_DIR);
    if (!dir) return 0;

    int count = 0;
    struct dirent *ent;
    while ((ent = readdir(dir)) != NULL && count < maxPacks) {
        const char *name = ent->d_name;
        /* Skip "." and ".." */
        if (name[0] == '.' && (name[1] == '\0' || (name[1] == '.' && name[2] == '\0')))
            continue;

        /* Check if it's a directory with a manifest.json */
        char manifestPath[256];
        snprintf(manifestPath, sizeof(manifestPath), "%s/%s/manifest.json", PACKS_DIR, name);

        FILE *f = fopen(manifestPath, "rb");
        if (!f) continue;  /* Not a valid pack folder */
        fclose(f);

        char folderPath[256];
        snprintf(folderPath, sizeof(folderPath), "%s/%s", PACKS_DIR, name);

        if (mspa_bundle_open(&out[count], folderPath)) {
            count++;
        }
    }

    closedir(dir);
    return count;
}

bool mspa_bundle_open(MspaBundle *bundle, const char *folderPath) {
    memset(bundle, 0, sizeof(*bundle));
    snprintf(bundle->folderPath, sizeof(bundle->folderPath), "%s", folderPath);

    /* Extract the folder name as a fallback packId */
    const char *lastSlash = strrchr(folderPath, '/');
    if (lastSlash) {
        snprintf(bundle->packId, sizeof(bundle->packId), "%s", lastSlash + 1);
    } else {
        snprintf(bundle->packId, sizeof(bundle->packId), "%s", folderPath);
    }

    /* Read manifest.json */
    char manifestPath[256];
    snprintf(manifestPath, sizeof(manifestPath), "%s/manifest.json", folderPath);

    FILE *f = fopen(manifestPath, "rb");
    if (!f) return false;

    fseek(f, 0, SEEK_END);
    long len = ftell(f);
    rewind(f);

    if (len <= 0 || len > 65536) {
        fclose(f);
        return false;
    }

    char *json = (char *)malloc((size_t)len + 1);
    if (!json) {
        fclose(f);
        return false;
    }

    size_t got = fread(json, 1, (size_t)len, f);
    fclose(f);
    json[got] = '\0';

    /* Parse JSON fields (simple strstr-based, matching mspa_page.c style) */
    auto_str_val(json, "pack_id", bundle->packId, sizeof(bundle->packId));
    auto_str_val(json, "title", bundle->title, sizeof(bundle->title));
    auto_str_val(json, "next_pack", bundle->nextPack, sizeof(bundle->nextPack));
    auto_str_val(json, "prev_pack", bundle->prevPack, sizeof(bundle->prevPack));

    /* Parse integers */
    const char *p;
    p = strstr(json, "\"first_page\"");
    if (p) { p = strchr(p, ':'); if (p) bundle->firstPage = atoi(p + 1); }
    p = strstr(json, "\"last_page\"");
    if (p) { p = strchr(p, ':'); if (p) bundle->lastPage = atoi(p + 1); }
    p = strstr(json, "\"page_count\"");
    if (p) { p = strchr(p, ':'); if (p) bundle->pageCount = atoi(p + 1); }

    free(json);
    return true;
}

void mspa_bundle_close(MspaBundle *bundle) {
    if (!bundle) return;
    /* Nothing to free — no dynamic allocations anymore */
    memset(bundle, 0, sizeof(*bundle));
}

bool mspa_bundle_file_exists(MspaBundle *bundle, const char *relativePath) {
    if (!bundle || !relativePath) return false;
    char fullPath[320];
    snprintf(fullPath, sizeof(fullPath), "%s/%s", bundle->folderPath, relativePath);
    FILE *f = fopen(fullPath, "rb");
    if (!f) return false;
    fclose(f);
    return true;
}

bool mspa_bundle_get_page_json_path(MspaBundle *bundle, int globalPage,
                                     char *pathOut, size_t pathLen) {
    if (!bundle || !pathOut) return false;
    snprintf(pathOut, pathLen, "%s/pages/%06d.json", bundle->folderPath, globalPage);

    /* Check if the file exists */
    FILE *f = fopen(pathOut, "rb");
    if (!f) return false;
    fclose(f);
    return true;
}

bool mspa_bundle_get_media_path(MspaBundle *bundle, const char *relativePath,
                                 char *pathOut, size_t pathLen) {
    if (!bundle || !relativePath || !pathOut) return false;
    snprintf(pathOut, pathLen, "%s/%s", bundle->folderPath, relativePath);

    /* Check if the file exists */
    FILE *f = fopen(pathOut, "rb");
    if (!f) return false;
    fclose(f);
    return true;
}

bool mspa_bundle_ensure_page_json(MspaBundle *bundle, int globalPage,
                                   char *cachePathOut, size_t cachePathLen) {
    /*
     * With folder-based bundles, the page JSON is already on the SD card
     * inside the pack folder. We just build the path and verify it exists.
     * The cachePathOut is set to the direct path inside the bundle folder.
     */
    if (!bundle || !cachePathOut) return false;

    snprintf(cachePathOut, cachePathLen,
             "%s/pages/%06d.json", bundle->folderPath, globalPage);

    FILE *f = fopen(cachePathOut, "rb");
    if (!f) return false;
    fclose(f);
    return true;
}

/* ── Internal: copy a file from src to dst, creating parent dirs ─────── */

static bool copy_file(const char *srcPath, const char *dstPath) {
    FILE *src = fopen(srcPath, "rb");
    if (!src) return false;

    /* Create parent directories for dstPath */
    {
        char tmp[256];
        snprintf(tmp, sizeof(tmp), "%s", dstPath);
        for (char *p = tmp + 1; *p; p++) {
            if (*p == '/') {
                *p = '\0';
                mkdir(tmp, 0777);
                *p = '/';
            }
        }
    }

    FILE *dst = fopen(dstPath, "wb");
    if (!dst) { fclose(src); return false; }

    uint8_t buf[8192];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), src)) > 0) {
        fwrite(buf, 1, n, dst);
    }

    fclose(src);
    fclose(dst);
    return true;
}

bool mspa_bundle_ensure_media(MspaBundle *bundle, const char *relativePath,
                               const char *destPath) {
    /*
     * With folder-based bundles, the media file is already on the SD card
     * inside the pack folder. If destPath differs from the source, we copy
     * it. If destPath is NULL or the same, we just verify existence.
     *
     * In practice, we now build the full path directly in main.c and don't
     * need to "extract" anything — the files are already there.
     */
    if (!bundle || !relativePath) return false;

    char srcPath[320];
    snprintf(srcPath, sizeof(srcPath), "%s/%s", bundle->folderPath, relativePath);

    /* If no destPath given, just verify the source exists */
    if (!destPath || destPath[0] == '\0') {
        FILE *f = fopen(srcPath, "rb");
        if (!f) return false;
        fclose(f);
        return true;
    }

    /* If destPath already exists, done */
    FILE *f = fopen(destPath, "rb");
    if (f) { fclose(f); return true; }

    /* Copy from bundle folder to destPath */
    FILE *src = fopen(srcPath, "rb");
    if (!src) return false;

    /* Create parent directories for destPath */
    {
        char tmp[256];
        snprintf(tmp, sizeof(tmp), "%s", destPath);
        for (char *p = tmp + 1; *p; p++) {
            if (*p == '/') {
                *p = '\0';
                mkdir(tmp, 0777);
                *p = '/';
            }
        }
    }

    FILE *dst = fopen(destPath, "wb");
    if (!dst) { fclose(src); return false; }

    uint8_t buf[8192];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), src)) > 0) {
        fwrite(buf, 1, n, dst);
    }

    fclose(src);
    fclose(dst);
    return true;
}
