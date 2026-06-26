#include <3ds.h>
#include <citro2d.h>
#include <malloc.h>
#include <stdlib.h> 
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <sys/stat.h>
#include <dirent.h>
#include <time.h>

#include "mspa_page.h"
#include "mspa_image.h"
#include "mspa_audio.h"
#include "mspa_bundle.h"

/* ═══════════════════════════════════════════════════════════════════════
 * APP STATE
 * ═══════════════════════════════════════════════════════════════════════ */

typedef enum {
    STATE_PACK_SELECT,
    STATE_LOADING,
    STATE_READING
} AppState;

typedef enum {
    LOAD_IDLE,
    LOAD_PAGE_JSON,       /* read page JSON from bundle folder */
    LOAD_MEDIA_READ,      /* read media file directly from bundle folder */
    LOAD_GIF_READ,        /* read raw GIF bytes from SD */
    LOAD_GIF_CONVERT_FRAME,/* one GIF frame per game-loop tick */
    LOAD_GIF_DELETE,      /* delete raw GIF from SD */
    LOAD_AUDIO_READ,      /* read audio file directly from bundle folder */
    LOAD_AUDIO_OPEN,      /* open WAV with ndsp */
    LOAD_TEX_LOAD,        /* load .tex into GPU */
    LOAD_COMMIT,
    LOAD_FAIL
} LoadStage;

typedef struct {
    bool active;
    int targetPage;
    LoadStage stage;
    MspaPage page;
    MspaImage image;
    MspaImage tempImage;
    char mediaPath[224];       /* full path to media file in bundle folder */
    char audioPath[224];       /* full path to audio file in bundle folder */
    char texPath[224];
    char animPath[224];
    char mediaRelPath[224];    /* full base path in bundle (e.g. "<bundle>/media/001901_0") */
    bool hasAudio;
    int targetMediaIndex;
    MspaAudio audio;
    MspaAnimManifest meta;
    uint32_t *delaysMs;
    uint32_t frameCount;
    uint32_t currentFrame;
    u64 nextFrameAtMs;
    uint8_t *gifBytes;
    size_t gifBytesSize;
    MspaGifConverter converter;
} LoadJob;

typedef struct {
    bool active;
    char basePath[320];        /* full bundle media base path (e.g. "<bundle>/media/001901_0") */
    char animPath[320];
    MspaAnimManifest meta;
    uint32_t *delaysMs;
    uint32_t frameCount;
    uint32_t currentFrame;
    u64 nextFrameAtMs;
} PanelAnimation;

/* ── Globals ──────────────────────────────────────────────────────────── */

static C3D_RenderTarget *topTarget    = NULL;
static C3D_RenderTarget *botTarget    = NULL;
static MspaImage         curImage     = {0};
static MspaAudio         curAudio     = {0};
static MspaImage         placeholder  = {0};
static MspaPage          curPage      = {0};
static AppState          state        = STATE_PACK_SELECT;
static bool              needsRedraw  = true;
static bool              pageLoadedOk = false;
static int               pageNum      = 0;
static int               curMediaIndex = 0;
static LoadJob           loadJob      = {0};
static PanelAnimation    curAnim      = {0};
static int               textScrollY  = 0;
static int               resumePage   = 0;

/* Pack selection */
#define MAX_PACKS 32
static MspaBundle        packs[MAX_PACKS];
static int               packCount    = 0;
static int               selectedPack = 0;
static MspaBundle       *activeBundle = NULL;  /* currently open bundle */

/* ═══════════════════════════════════════════════════════════════════════
 * HELPERS
 * ═══════════════════════════════════════════════════════════════════════ */

static const char *load_stage_name(LoadStage s) {
    switch (s) {
    case LOAD_PAGE_JSON:        return "load page";
    case LOAD_MEDIA_READ:      return "read media";
    case LOAD_GIF_READ:         return "read gif";
    case LOAD_GIF_CONVERT_FRAME:return "convert frames";
    case LOAD_GIF_DELETE:       return "delete gif";
    case LOAD_AUDIO_READ:      return "read audio";
    case LOAD_AUDIO_OPEN:       return "open audio";
    case LOAD_TEX_LOAD:         return "load tex";
    case LOAD_COMMIT:           return "commit";
    case LOAD_FAIL:             return "fail";
    default:                    return "idle";
    }
}

static bool file_exists(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    fclose(f);
    return true;
}

static void ensure_sd_dirs(void) {
    mkdir("sdmc:/3ds", 0777);
    mkdir("sdmc:/3ds/MSPA-3DS", 0777);
    mkdir("sdmc:/3ds/MSPA-3DS/packs", 0777);
}

/* ═══════════════════════════════════════════════════════════════════════
 * PANEL ANIMATION (unchanged)
 * ═══════════════════════════════════════════════════════════════════════ */

static void panel_anim_free(void) {
    free(curAnim.delaysMs);
    curAnim.delaysMs = NULL;
    curAnim.active = false;
    curAnim.frameCount = 0;
    curAnim.currentFrame = 0;
    curAnim.nextFrameAtMs = 0;
    curAnim.basePath[0] = '\0';
    curAnim.animPath[0] = '\0';
}

static void panel_anim_apply_frame(uint32_t frame) {
    if (!curAnim.active || curAnim.frameCount == 0) return;
    if (frame >= curAnim.frameCount) return;

    /* Build the .tex path directly inside the bundle folder.
     * curAnim.basePath is like "sdmc:/3ds/MSPA-3DS/packs/<pack>/media/001901_0" */
    char framePath[320];
    snprintf(framePath, sizeof(framePath), "%s-%03u.tex", curAnim.basePath, (unsigned)frame);

    MspaImage newImg = {0};
    if (!mspa_panel_load_tex_file(framePath, &newImg)) return;

    mspa_image_free(&curImage);
    curImage = newImg;
    curImage.image.tex    = &curImage.tex;
    curImage.image.subtex = &curImage.subtex;
    curAnim.currentFrame  = frame;
}

static void panel_anim_start_for_page(const char *bundleMediaBase, int frameCount,
                                       const MspaAnimManifest *meta, const uint32_t *delays) {
    panel_anim_free();
    if (!bundleMediaBase || frameCount <= 0 || !meta || !delays) return;

    curAnim.active = true;
    strncpy(curAnim.basePath, bundleMediaBase, sizeof(curAnim.basePath) - 1);
    curAnim.meta       = *meta;
    curAnim.delaysMs   = (uint32_t *)delays;
    curAnim.frameCount = frameCount;
    curAnim.currentFrame = 0;

    panel_anim_apply_frame(0);

    uint32_t firstDelay = curAnim.delaysMs[0] ? curAnim.delaysMs[0] : 100u;
    curAnim.nextFrameAtMs = osGetTime() + firstDelay;
}

static void panel_anim_tick(void) {
    if (!curAnim.active || state != STATE_READING || loadJob.active) return;
    if (curAnim.frameCount <= 1 || !curImage.valid) return;

    u64 now = osGetTime();
    if (now < curAnim.nextFrameAtMs) return;

    uint32_t nextFrame = curAnim.currentFrame + 1;
    if (nextFrame >= curAnim.frameCount) nextFrame = 0;

    curAnim.currentFrame = nextFrame;
    panel_anim_apply_frame(nextFrame);
    needsRedraw = true;

    uint32_t delay = curAnim.delaysMs[nextFrame] ? curAnim.delaysMs[nextFrame] : 50u;
    curAnim.nextFrameAtMs = now + delay;
}

/* ═══════════════════════════════════════════════════════════════════════
 * FILE HELPERS
 * ═══════════════════════════════════════════════════════════════════════ */

static bool read_entire_file(const char *path, uint8_t **bufOut, size_t *sizeOut) {
    if (bufOut) *bufOut = NULL;
    if (sizeOut) *sizeOut = 0;
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return false; }
    long len = ftell(f);
    if (len < 0) { fclose(f); return false; }
    rewind(f);
    uint8_t *buf = (uint8_t *)malloc((size_t)len);
    if (!buf) { fclose(f); return false; }
    size_t got = fread(buf, 1, (size_t)len, f);
    fclose(f);
    if (got != (size_t)len) { free(buf); return false; }
    if (bufOut) *bufOut = buf;
    if (sizeOut) *sizeOut = got;
    return true;
}

static bool resize_rgba_nearest(const uint8_t *src, int sw, int sh,
                                uint8_t **dstOut, int *dwOut, int *dhOut) {
    if (!src || sw <= 0 || sh <= 0 || !dstOut || !dwOut || !dhOut) return false;
    int dw = sw;
    int dh = sh;
    if (dw > MSPA_PANEL_MAX_W) { dh = dh * MSPA_PANEL_MAX_W / dw; dw = MSPA_PANEL_MAX_W; }
    if (dh > MSPA_PANEL_MAX_H) { dw = dw * MSPA_PANEL_MAX_H / dh; dh = MSPA_PANEL_MAX_H; }
    if (dw <= 0 || dh <= 0) return false;
    uint8_t *dst = (uint8_t *)malloc((size_t)dw * (size_t)dh * 4u);
    if (!dst) return false;
    for (int y = 0; y < dh; y++) {
        int sy = (y * sh) / dh;
        for (int x = 0; x < dw; x++) {
            int sx = (x * sw) / dw;
            memcpy(&dst[((size_t)y * dw + x) * 4u], &src[((size_t)sy * sw + sx) * 4u], 4u);
        }
    }
    *dstOut = dst;
    *dwOut = dw;
    *dhOut = dh;
    return true;
}

/* ═══════════════════════════════════════════════════════════════════════
 * LOAD JOB
 * ═══════════════════════════════════════════════════════════════════════ */

static void free_job_buffers(void) {
    free(loadJob.gifBytes); loadJob.gifBytes = NULL; loadJob.gifBytesSize = 0;
    mspa_gif_converter_close(&loadJob.converter);
    mspa_image_free(&loadJob.tempImage);
    mspa_audio_close(&loadJob.audio);
}

static void load_job_reset(void) {
    mspa_page_free(&loadJob.page);
    mspa_image_free(&loadJob.image);
    mspa_audio_close(&loadJob.audio);
    free_job_buffers();
    memset(&loadJob, 0, sizeof(loadJob));
}

static void begin_page_load(int n, int mediaIndex) {
    if (loadJob.active) return;
    if (!activeBundle) return;

    pageNum = n;

    panel_anim_free();
    mspa_audio_close(&curAudio);
    mspa_image_free(&curImage);

    load_job_reset();
    loadJob.active = true;
    loadJob.targetPage = n;
    loadJob.targetMediaIndex = mediaIndex;
    loadJob.stage = LOAD_PAGE_JSON;
    state = STATE_LOADING;
    pageLoadedOk = false;
    textScrollY = 0;
    needsRedraw = true;
}

static void commit_loaded_page(void) {
    mspa_page_free(&curPage);
    mspa_image_free(&curImage);

    curPage = loadJob.page;
    memset(&loadJob.page, 0, sizeof(loadJob.page));

    if (curPage.page == 0)
        curPage.page = loadJob.targetPage;
    pageNum = curPage.page;

    curMediaIndex = loadJob.targetMediaIndex;
    if (loadJob.hasAudio) {
        curAudio = loadJob.audio;
        memset(&loadJob.audio, 0, sizeof(loadJob.audio));
    } else {
        mspa_audio_close(&curAudio);
    }

    /* Transfer image from loadJob */
    curImage = loadJob.image;
    curImage.image.tex    = &curImage.tex;
    curImage.image.subtex = &curImage.subtex;
    memset(&loadJob.image, 0, sizeof(loadJob.image));

    /* Start animation if .anim manifest exists in the bundle. */
    if (loadJob.animPath[0] && loadJob.mediaRelPath[0]) {
        MspaAnimManifest animMeta = {0};
        uint32_t *animDelays = NULL;
        if (mspa_panel_load_anim_manifest(loadJob.animPath, &animMeta, &animDelays)) {
            if (animMeta.frameCount > 1 && animDelays) {
                panel_anim_start_for_page(loadJob.mediaRelPath,
                                          (int)animMeta.frameCount,
                                          &animMeta, animDelays);
                /* animDelays now owned by curAnim — don't free */
            } else {
                free(animDelays);
            }
        }
    }

    /* Save resume point */
    mkdir("sdmc:/mspa", 0777);
    FILE *rf = fopen("sdmc:/mspa/resume.txt", "w");
    if (rf) { fprintf(rf, "%d", pageNum); fclose(rf); }
    resumePage = pageNum;

    pageLoadedOk = true;
    state = STATE_READING;
    needsRedraw = true;
}

static void fail_loaded_page(void) {
    load_job_reset();
    panel_anim_free();
    mspa_audio_close(&curAudio);
    state = (curPage.page > 0) ? STATE_READING : STATE_PACK_SELECT;
    pageLoadedOk = false;
    needsRedraw = true;
}

static void advance_load_job(void) {
    if (!loadJob.active) return;

    switch (loadJob.stage) {
    case LOAD_PAGE_JSON: {
        if (!activeBundle) { loadJob.stage = LOAD_FAIL; break; }

        /* Get path to page JSON inside the bundle folder */
        char jsonPath[256];
        if (!mspa_bundle_ensure_page_json(activeBundle, loadJob.targetPage,
                                          jsonPath, sizeof(jsonPath))) {
            loadJob.stage = LOAD_FAIL;
            break;
        }

        /* Load the JSON directly (no extraction needed — it's a plain file) */
        char *json = NULL;
        {
            FILE *f = fopen(jsonPath, "rb");
            if (!f) { loadJob.stage = LOAD_FAIL; break; }
            fseek(f, 0, SEEK_END);
            long len = ftell(f);
            rewind(f);
            json = (char *)malloc((size_t)len + 1);
            if (!json) { fclose(f); loadJob.stage = LOAD_FAIL; break; }
            json[fread(json, 1, (size_t)len, f)] = '\0';
            fclose(f);
        }

        /* Parse using the existing JSON parser from mspa_page.c */
        {
            extern bool parse_page_json(const char *json, MspaPage *out);
            if (!parse_page_json(json, &loadJob.page)) {
                free(json);
                loadJob.stage = LOAD_FAIL;
                break;
            }
        }
        free(json);

        if (loadJob.page.page == 0)
            loadJob.page.page = loadJob.targetPage;

        if (loadJob.page.media && loadJob.page.mediaCount > 0) {
            size_t mi = (loadJob.targetMediaIndex < 0) ? 0 : (size_t)loadJob.targetMediaIndex;
            if (mi >= loadJob.page.mediaCount) mi = 0;

            /* The media field in bundle JSON is a relative path like "media/001901_0.gif" */
            const char *mediaRef = loadJob.page.media[mi];
            loadJob.hasAudio = (loadJob.page.audio && loadJob.page.audio[0]);

            /* Build paths directly inside the bundle folder.
             * No caching — read .tex/.anim directly from the pack.
             * mediaRef = "media/001901_0.gif"
             * base = "media/001901_0" (strip extension)
             * texPath = "<bundle>/media/001901_0-000.tex"
             * animPath = "<bundle>/media/001901_0.anim"
             * mediaBasePath = "<bundle>/media/001901_0" (for animation frame loading)
             */
            char baseRelPath[256];
            snprintf(baseRelPath, sizeof(baseRelPath), "%s", mediaRef);
            char *dot = strrchr(baseRelPath, '.');
            if (dot) *dot = '\0';

            /* Full bundle-relative base path for .tex and .anim */
            char bundleMediaBase[320];
            snprintf(bundleMediaBase, sizeof(bundleMediaBase),
                     "%s/%s", activeBundle->folderPath, baseRelPath);

            /* Frame 0 .tex path */
            snprintf(loadJob.texPath, sizeof(loadJob.texPath),
                     "%s-000.tex", bundleMediaBase);

            /* .anim manifest path */
            snprintf(loadJob.animPath, sizeof(loadJob.animPath),
                     "%s.anim", bundleMediaBase);

            /* Store the media base path for animation system */
            snprintf(loadJob.mediaRelPath, sizeof(loadJob.mediaRelPath),
                     "%s", bundleMediaBase);

            /* Full path to original media (GIF fallback) */
            snprintf(loadJob.mediaPath, sizeof(loadJob.mediaPath),
                     "%s/%s", activeBundle->folderPath, mediaRef);

            if (loadJob.hasAudio) {
                snprintf(loadJob.audioPath, sizeof(loadJob.audioPath),
                         "%s/%s", activeBundle->folderPath, loadJob.page.audio);
            } else {
                loadJob.audioPath[0] = '\0';
            }

            if (file_exists(loadJob.texPath)) {
                /* Pre-converted .tex exists in bundle — load directly */
                loadJob.stage = LOAD_TEX_LOAD;
            } else if (file_exists(loadJob.mediaPath)) {
                /* No .tex but GIF exists in bundle — decode it on-device */
                loadJob.stage = LOAD_GIF_READ;
            } else {
                loadJob.stage = LOAD_FAIL;
            }
        } else {
            loadJob.stage = LOAD_COMMIT;
        }
        break;
    }

    case LOAD_MEDIA_READ: {
        /* No longer used — we read directly from the bundle folder.
         * Kept as a passthrough in case any legacy path lands here. */
        loadJob.stage = LOAD_FAIL;
        break;
    }

    case LOAD_GIF_READ: {
        /* Read GIF directly from bundle folder for on-device decoding.
         * mediaPath is already the full path inside the bundle. */
        if (!read_entire_file(loadJob.mediaPath, &loadJob.gifBytes, &loadJob.gifBytesSize)) {
            loadJob.stage = LOAD_FAIL;
            break;
        }
        /* Write decoded .tex/.anim back into the bundle folder
         * so they're available for future visits. basePath is
         * the bundle media base (e.g. "<bundle>/media/001901_0") */
        if (!mspa_gif_converter_open(&loadJob.converter,
                                     loadJob.gifBytes, loadJob.gifBytesSize,
                                     loadJob.mediaRelPath, loadJob.animPath)) {
            loadJob.stage = LOAD_FAIL;
            break;
        }
        loadJob.stage = LOAD_GIF_CONVERT_FRAME;
        break;
    }

    case LOAD_GIF_CONVERT_FRAME:
        mspa_gif_converter_step(&loadJob.converter);
        if (loadJob.converter.failed) {
            loadJob.stage = LOAD_FAIL;
        } else if (loadJob.converter.done) {
            mspa_gif_converter_close(&loadJob.converter);
            free(loadJob.gifBytes);
            loadJob.gifBytes = NULL;
            loadJob.gifBytesSize = 0;
            loadJob.stage = LOAD_GIF_DELETE;
        }
        break;

    case LOAD_GIF_DELETE:
        /* Don't delete the original GIF from the bundle — it's part of the pack.
         * The on-device decoded .tex files are saved alongside it in the bundle. */
        loadJob.stage = LOAD_TEX_LOAD;
        break;

    case LOAD_AUDIO_READ: {
        /* Read audio directly from bundle folder.
         * audioPath is already the full path inside the bundle. */
        if (loadJob.hasAudio && loadJob.audioPath[0]) {
            loadJob.stage = LOAD_AUDIO_OPEN;
        } else {
            loadJob.stage = LOAD_COMMIT;
        }
        break;
    }

    case LOAD_AUDIO_OPEN:
        if (loadJob.hasAudio) {
            if (!mspa_audio_open(&loadJob.audio, loadJob.audioPath)) {
                mspa_audio_close(&loadJob.audio);
                loadJob.hasAudio = false;
            }
        }
        loadJob.stage = LOAD_COMMIT;
        break;

    case LOAD_TEX_LOAD:
        if (!mspa_panel_load_tex_file(loadJob.texPath, &loadJob.image)) {
            /* .tex load failed — try falling back to GIF decode */
            if (file_exists(loadJob.mediaPath)) {
                loadJob.stage = LOAD_GIF_READ;
            } else {
                loadJob.stage = LOAD_FAIL;
            }
            break;
        }
        loadJob.stage = loadJob.hasAudio ? LOAD_AUDIO_READ : LOAD_COMMIT;
        break;

    case LOAD_COMMIT:
        commit_loaded_page();
        load_job_reset();
        break;

    case LOAD_FAIL:
    default:
        fail_loaded_page();
        break;
    }

    needsRedraw = true;
}

/* ═══════════════════════════════════════════════════════════════════════
 * RENDERING (top + bottom screens — kept as-is)
 * ═══════════════════════════════════════════════════════════════════════ */

static void render_top(void) {
    C3D_FrameBegin(C3D_FRAME_SYNCDRAW);

    C2D_TargetClear(topTarget, C2D_Color32(0xC0, 0xC0, 0xC0, 0xFF));
    C2D_SceneBegin(topTarget);

    const MspaImage *img = curImage.valid ? &curImage : &placeholder;
    if (img->valid) {
        float x = (400.0f - (float)img->width)  * 0.5f;
        float y = (240.0f - (float)img->height) * 0.5f;
        if (x < 0.0f) x = 0.0f;
        if (y < 0.0f) y = 0.0f;
        C2D_DrawImageAt(img->image, x, y, 0.5f, NULL, 1.0f, 1.0f);
    }
}

#define BOT_W        320
#define BOT_H        240
#define SIDEBAR_W     20
#define BOT_PAD_X     10
#define BOT_PAD_TOP    6
#define BOT_LINE_H    18
#define COL_BG        C2D_Color32(0xE8,0xE8,0xE8,0xFF)
#define COL_SIDEBAR   C2D_Color32(0xD0,0xD0,0xD0,0xFF)
#define COL_WHITE     C2D_Color32(0xFF,0xFF,0xFF,0xFF)
#define COL_BLACK     C2D_Color32(0x00,0x00,0x00,0xFF)
#define COL_DIV       C2D_Color32(0xAA,0xAA,0xAA,0xFF)
#define COL_BLUE      C2D_Color32(0x00,0x00,0xCC,0xFF)
#define COL_GREY      C2D_Color32(0x77,0x77,0x77,0xFF)

#define TEXT_X_START  (SIDEBAR_W + BOT_PAD_X)
#define TEXT_X_END    (BOT_W - SIDEBAR_W - BOT_PAD_X)
#define TEXT_AVAIL_W  (TEXT_X_END - TEXT_X_START)
#define TITLE_Y       BOT_PAD_TOP
#define DIV_Y         (TITLE_Y + 18)
#define BODY_Y_START  (DIV_Y + 4)
#define BODY_Y_END    (BOT_H - 4)

static C2D_TextBuf textBuf = NULL;

static void bot_init(void) { textBuf = C2D_TextBufNew(4096); }
static void bot_free(void) { if (textBuf) { C2D_TextBufDelete(textBuf); textBuf = NULL; } }

static float draw_text(const char *str, float x, float y, float z, float sz, u32 color) {
    if (!str || !*str || !textBuf) return 0.0f;
    C2D_TextBufClear(textBuf);
    C2D_Text t;
    C2D_TextParse(&t, textBuf, str);
    C2D_TextOptimize(&t);
    float w = 0.0f, h = 0.0f;
    C2D_TextGetDimensions(&t, sz, sz, &w, &h);
    C2D_DrawText(&t, C2D_WithColor | C2D_AtBaseline, x, y + h, z, sz, sz, color);
    return w;
}

static float measure_text_width(const char *text, float sz) {
    if (!text || !*text || !textBuf) return 0.0f;
    C2D_TextBufClear(textBuf);
    C2D_Text t;
    C2D_TextParse(&t, textBuf, text);
    C2D_TextOptimize(&t);
    float w = 0.0f, h = 0.0f;
    C2D_TextGetDimensions(&t, sz, sz, &w, &h);
    return w;
}

static float draw_wrapped_text(const char *text, float x, float startY,
                               float maxPx, float sz, u32 color) {
    if (!text || !*text || !textBuf) return startY;
    float lineH = (sz >= 0.58f) ? 18.0f : (sz >= 0.50f ? 16.0f : 14.0f);
    float y = startY;
    char lineBuf[256];
    const char *p = text;
    while (*p) {
        while (*p == '\r') p++;
        if (!*p) break;
        if (*p == '\n') { p++; y += lineH; continue; }
        const char *lineStart = p;
        const char *lastSpace = NULL;
        const char *scan      = p;
        while (*scan && *scan != '\n') {
            size_t len = (size_t)(scan - lineStart) + 1;
            if (len >= sizeof(lineBuf)) { scan++; break; }
            memcpy(lineBuf, lineStart, len);
            lineBuf[len] = '\0';
            float w = measure_text_width(lineBuf, sz);
            if (w > maxPx) {
                if (lastSpace && lastSpace > lineStart) {
                    scan = lastSpace;
                }
                break;
            }
            if (*scan == ' ') lastSpace = scan;
            scan++;
        }
        size_t drawLen = (size_t)(scan - lineStart);
        while (drawLen > 0 && lineStart[drawLen - 1] == ' ') drawLen--;
        if (drawLen > 0 && drawLen < sizeof(lineBuf)) {
            memcpy(lineBuf, lineStart, drawLen);
            lineBuf[drawLen] = '\0';
            draw_text(lineBuf, x, y, 0.3f, sz, color);
        }
        y += lineH;
        p = scan;
        if (*p == ' ') p++;
        if (*p == '\n') { p++; }
    }
    return y;
}

static void render_bottom(void) {
    C2D_SceneBegin(botTarget);
    C2D_TargetClear(botTarget, COL_BG);

    C2D_DrawRectSolid(0,               0, 0.1f, SIDEBAR_W,     BOT_H, COL_SIDEBAR);
    C2D_DrawRectSolid(BOT_W - SIDEBAR_W, 0, 0.1f, SIDEBAR_W,   BOT_H, COL_SIDEBAR);
    C2D_DrawRectSolid(SIDEBAR_W, 0, 0.1f, BOT_W - SIDEBAR_W * 2, BOT_H, COL_WHITE);

    const float TITLE_SZ = 0.60f;
    const float BODY_SZ  = 0.45f;
    const float CMD_SZ   = 0.52f;
    const int   LINE_H   = 18;

    switch (state) {
    case STATE_PACK_SELECT: {
        draw_text("MSPA-3DS", TEXT_X_START, TITLE_Y, 0.3f, TITLE_SZ, COL_BLACK);
        float y = BODY_Y_START;
        if (packCount == 0) {
            draw_text("No packs found!", TEXT_X_START, y, 0.3f, BODY_SZ, COL_GREY);
            y += LINE_H * 2;
            draw_text("Place pack folders in:", TEXT_X_START, y, 0.3f, BODY_SZ, COL_GREY);
            y += LINE_H;
            draw_text("sdmc:/3ds/MSPA-3DS/packs/<name>/", TEXT_X_START, y, 0.3f, BODY_SZ, COL_GREY);
        } else {
            draw_text("SELECT A PACK:", TEXT_X_START, y, 0.3f, BODY_SZ, COL_BLACK);
            y += LINE_H;
            for (int i = 0; i < packCount && y < BODY_Y_END; i++) {
                u32 col = (i == selectedPack) ? COL_BLUE : COL_BLACK;
                const char *sel = (i == selectedPack) ? "> " : "  ";
                char tmp[192];
                snprintf(tmp, sizeof(tmp), "%s%s", sel, packs[i].title[0] ? packs[i].title : packs[i].packId);
                draw_text(tmp, TEXT_X_START, y, 0.3f, BODY_SZ, col);
                y += LINE_H;
            }
            y += LINE_H;
            char info[128];
            if (packs[selectedPack].pageCount > 0) {
                snprintf(info, sizeof(info), "Pages: %d (%d-%d)",
                         packs[selectedPack].pageCount,
                         packs[selectedPack].firstPage,
                         packs[selectedPack].lastPage);
                draw_text(info, TEXT_X_START, y, 0.3f, BODY_SZ, COL_GREY);
            }
        }
        y = BODY_Y_END - LINE_H * 2;
        draw_text("> A: Select  UP/DN: Choose", TEXT_X_START, y, 0.3f, BODY_SZ, COL_GREY);
        y += LINE_H;
        draw_text("> START: Quit", TEXT_X_START, y, 0.3f, BODY_SZ, COL_GREY);
        break;
    }

    case STATE_LOADING: {
        draw_text("LOADING", TEXT_X_START, TITLE_Y, 0.3f, TITLE_SZ, COL_BLACK);
        char tmp[64];
        snprintf(tmp, sizeof(tmp), "Page %06d", loadJob.targetPage);
        draw_text(tmp, TEXT_X_START, BODY_Y_START + LINE_H * 1, 0.3f, BODY_SZ, COL_BLACK);
        if (loadJob.stage == LOAD_GIF_CONVERT_FRAME && loadJob.converter.frameCount > 0) {
            snprintf(tmp, sizeof(tmp), "frame %lu / %lu",
                     loadJob.converter.frameDone + 1,
                     loadJob.converter.frameCount);
        } else {
            snprintf(tmp, sizeof(tmp), "Stage: %s", load_stage_name(loadJob.stage));
        }
        draw_text(tmp, TEXT_X_START, BODY_Y_START + LINE_H * 2, 0.3f, BODY_SZ, COL_GREY);
        draw_text("Please wait...", TEXT_X_START, BODY_Y_START + LINE_H * 4, 0.3f, BODY_SZ, COL_BLACK);
        break;
    }

    case STATE_READING: {
        const char *typeStr = (curPage.type && curPage.type[0]) ? curPage.type : "PAGE";
        float currentY = TITLE_Y;
        currentY = draw_wrapped_text(typeStr, TEXT_X_START, currentY, (float)TEXT_AVAIL_W, TITLE_SZ, COL_BLACK);
        currentY += 8.0f;
        C2D_Flush();
        int clipTop = (int)currentY;
        C3D_SetScissor(GPU_SCISSOR_NORMAL, clipTop, SIDEBAR_W, BODY_Y_END, BOT_W - SIDEBAR_W);
        float drawY = currentY - textScrollY;
        for (size_t i = 0; i < curPage.textCount; i++) {
            if (!curPage.text[i] || !curPage.text[i][0]) continue;
            drawY = draw_wrapped_text(curPage.text[i], TEXT_X_START, drawY, (float)TEXT_AVAIL_W, BODY_SZ, COL_BLACK);
            drawY += 4.0f;
        }
        if (curPage.command && curPage.command[0]) {
            drawY += 2.0f;
            drawY = draw_wrapped_text(curPage.command, TEXT_X_START, drawY, (float)TEXT_AVAIL_W, CMD_SZ, COL_BLUE);
        }
        C2D_Flush();
        C3D_SetScissor(GPU_SCISSOR_DISABLE, 0, 0, 0, 0);
        break;
    }
    }

    needsRedraw = false;
    C3D_FrameEnd(0);
}

/* ═══════════════════════════════════════════════════════════════════════
 * MAIN
 * ═══════════════════════════════════════════════════════════════════════ */

int main(void) {
    gfxInitDefault();
    romfsInit();

    ensure_sd_dirs();

    C3D_Init(C3D_DEFAULT_CMDBUF_SIZE);
    C2D_Init(C2D_DEFAULT_MAX_OBJECTS);
    C2D_Prepare();
    topTarget = C2D_CreateScreenTarget(GFX_TOP, GFX_LEFT);
    botTarget = C2D_CreateScreenTarget(GFX_BOTTOM, GFX_LEFT);
    bot_init();
    mspa_image_make_placeholder(&placeholder);

    /* Scan for packs on SD card */
    packCount = mspa_bundle_scan_packs(packs, MAX_PACKS);

    /* Load resume page */
    {
        FILE *f = fopen("sdmc:/mspa/resume.txt", "r");
        if (f) {
            if (fscanf(f, "%d", &resumePage) != 1) resumePage = 0;
            fclose(f);
        }
    }

    while (aptMainLoop()) {
        hidScanInput();
        u32 kDown = hidKeysDown();
        u32 kHeld = hidKeysHeld();

        if (kDown & KEY_START)
            break;

        if (!loadJob.active) {
            switch (state) {
            case STATE_PACK_SELECT:
                if (packCount > 0) {
                    if (kDown & KEY_UP) {
                        selectedPack = (selectedPack - 1 + packCount) % packCount;
                        needsRedraw = true;
                    }
                    if (kDown & KEY_DOWN) {
                        selectedPack = (selectedPack + 1) % packCount;
                        needsRedraw = true;
                    }
                    if (kDown & KEY_A) {
                        /* Open the selected bundle */
                        activeBundle = &packs[selectedPack];
                        int startPage = activeBundle->firstPage;
                        if (startPage <= 0) startPage = 1;
                        begin_page_load(startPage, 0);
                    }
                }
                break;

            case STATE_READING:
                if (kHeld & KEY_UP) {
                    textScrollY -= 4;
                    if (textScrollY < 0) textScrollY = 0;
                    needsRedraw = true;
                }
                if (kHeld & KEY_DOWN) {
                    textScrollY += 4;
                    needsRedraw = true;
                }

                if (kDown & KEY_A) {
                    if (curPage.mediaCount > 1 && curMediaIndex + 1 < (int)curPage.mediaCount) {
                        begin_page_load(pageNum, curMediaIndex + 1);
                    } else {
                        int nextPage = curPage.next;
                        if (nextPage <= pageNum)
                            nextPage = pageNum + 1;
                        begin_page_load(nextPage, 0);
                    }
                }
                if (kDown & KEY_B) {
                    if (curPage.mediaCount > 1 && curMediaIndex > 0) {
                        begin_page_load(pageNum, curMediaIndex - 1);
                    } else if (pageNum > 1) {
                        begin_page_load(pageNum - 1, 0);
                    }
                }
                if (kDown & KEY_X) {
                    mspa_audio_close(&curAudio);
                    panel_anim_free();
                    load_job_reset();
                    if (activeBundle) {
                        mspa_bundle_close(activeBundle);
                        activeBundle = NULL;
                    }
                    /* Re-scan packs (in case user added new ones) */
                    for (int i = 0; i < packCount; i++)
                        mspa_bundle_close(&packs[i]);
                    packCount = mspa_bundle_scan_packs(packs, MAX_PACKS);
                    selectedPack = 0;
                    state = STATE_PACK_SELECT;
                    needsRedraw = true;
                }
                break;

            case STATE_LOADING:
                break;
            }
        }

        if (loadJob.active) {
            advance_load_job();
        } else {
            if (curAudio.active) mspa_audio_tick(&curAudio);
            panel_anim_tick();
        }

        render_top();
        render_bottom();
    }

    mspa_image_free(&curImage);
    mspa_image_free(&placeholder);
    mspa_page_free(&curPage);
    panel_anim_free();
    mspa_audio_close(&curAudio);
    load_job_reset();
    if (activeBundle) mspa_bundle_close(activeBundle);
    for (int i = 0; i < packCount; i++) mspa_bundle_close(&packs[i]);
    bot_free();
    C2D_Fini();
    C3D_Fini();
    romfsExit();
    gfxExit();
    return 0;
}
