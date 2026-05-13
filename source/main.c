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

#define SOC_ALIGN      0x1000
#define SOC_BUFFERSIZE 0x100000

typedef enum {
    STATE_MENU,
    STATE_LOADING,
    STATE_READING
} AppState;

typedef enum {
    LOAD_IDLE,
    LOAD_PAGE_JSON,
    LOAD_GIF_DOWNLOAD,
    LOAD_GIF_READ,
    LOAD_GIF_CONVERT_FRAME,  /* one GIF frame per game-loop tick */
    LOAD_GIF_DELETE,
    LOAD_TEX_LOAD,
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
    char basePath[192];
    char gifPath[224];
    char texPath[224]; 
    char animPath[224];
    MspaAnimManifest meta;
    uint32_t *delaysMs;
    uint32_t frameCount;
    uint32_t currentFrame;
    u64 nextFrameAtMs;
    uint8_t *gifBytes;
    size_t gifBytesSize;
    MspaGifConverter converter;  /* streamed frame-by-frame converter */
} LoadJob;

typedef struct {
    bool active;
    char basePath[192];
    char animPath[224];
    MspaAnimManifest meta;
    uint32_t *delaysMs;
    uint32_t frameCount;
    uint32_t currentFrame;
    u64 nextFrameAtMs;
} PanelAnimation;

static u32              *socBuf       = NULL;
static C3D_RenderTarget *topTarget    = NULL;
static C3D_RenderTarget *botTarget    = NULL;
static MspaImage         curImage     = {0};
static MspaImage         placeholder  = {0};
static MspaPage          curPage      = {0};
static AppState          state        = STATE_MENU;
static bool              needsRedraw  = true;
static bool              pageLoadedOk = false;
static int               pageNum      = 1901;
static int               startPage    = 1901;
static LoadJob           loadJob      = {0};
static PanelAnimation    curAnim      = {0};
static int               textScrollY  = 0;
static bool              resumeAvailable = false;
static int               resumePage   = 0;

static const char *load_stage_name(LoadStage s) {
    switch (s) {
    case LOAD_PAGE_JSON:        return "load page";
    case LOAD_GIF_DOWNLOAD:     return "download gif";
    case LOAD_GIF_READ:         return "read gif";
    case LOAD_GIF_CONVERT_FRAME:return "convert frames";
    case LOAD_GIF_DELETE:       return "delete gif";
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
    mkdir("sdmc:/3ds/MSPA-3DS/pages", 0777);
    mkdir("sdmc:/3ds/MSPA-3DS/panels", 0777);
}

static bool find_latest_cached_page(int *pageOut) {
    if (pageOut) *pageOut = 0;
    DIR *dir = opendir("sdmc:/3ds/MSPA-3DS/pages");
    if (!dir) return false;

    int bestPage = 0;
    time_t bestMtime = 0;
    struct dirent *ent;

    while ((ent = readdir(dir)) != NULL) {
        const char *name = ent->d_name;
        size_t len = strlen(name);
        if (len != 10) continue; /* 000000.json */
        if (strcmp(name + 6, ".json") != 0) continue;
        for (int i = 0; i < 6; i++) {
            if (name[i] < '0' || name[i] > '9') goto next_entry;
        }
        {
            char path[160];
            snprintf(path, sizeof(path), "sdmc:/3ds/MSPA-3DS/pages/%s", name);
            struct stat st;
            if (stat(path, &st) != 0) goto next_entry;
            int p = atoi(name);
            if (p > 0 && (bestPage == 0 || st.st_mtime > bestMtime || (st.st_mtime == bestMtime && p > bestPage))) {
                bestPage = p;
                bestMtime = st.st_mtime;
            }
        }
        next_entry: ;
    }

    closedir(dir);
    if (bestPage <= 0) return false;
    if (pageOut) *pageOut = bestPage;
    return true;
}

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

    /* build path for this frame's tex file */
    char framePath[256];
    mspa_panel_frame_tex_path(curAnim.basePath, frame, framePath, sizeof(framePath));

    /* load new frame first, then free old one to avoid a gap */
    MspaImage newImg = {0};
    if (!mspa_panel_load_tex_file(framePath, &newImg)) return;

    mspa_image_free(&curImage);
    curImage = newImg;
    curImage.image.tex    = &curImage.tex;
    curImage.image.subtex = &curImage.subtex;
    curAnim.currentFrame  = frame;
}

static void panel_anim_start_for_page(const MspaPage *pg, int page) {
    panel_anim_free();
    if (!pg || !pg->media || !pg->media[0]) return;

    char base[192], animPath[224];
    mspa_panel_path_from_url(pg->media, page, 1, 1, base, sizeof(base));
    snprintf(animPath, sizeof(animPath), "%s.anim", base);

    if (!file_exists(animPath)) return;

    MspaAnimManifest meta = {0};
    uint32_t *delays = NULL;
    if (!mspa_panel_load_anim_manifest(animPath, &meta, &delays)) {
        free(delays);
        return;
    }

    if (meta.frameCount == 0 || !delays) {
        free(delays);
        return;
    }

    curAnim.active = true;
    strncpy(curAnim.basePath, base, sizeof(curAnim.basePath) - 1);
    strncpy(curAnim.animPath, animPath, sizeof(curAnim.animPath) - 1);
    curAnim.meta       = meta;
    curAnim.delaysMs   = delays;
    curAnim.frameCount = meta.frameCount;
    curAnim.currentFrame = 0;

    /* load frame 0 — replaces whatever static image is currently shown */
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

static void free_job_buffers(void) {
    free(loadJob.gifBytes); loadJob.gifBytes = NULL; loadJob.gifBytesSize = 0;
    mspa_gif_converter_close(&loadJob.converter);
    mspa_image_free(&loadJob.tempImage);
}

static void load_job_reset(void) {
    mspa_page_free(&loadJob.page);
    mspa_image_free(&loadJob.image);
    free_job_buffers();
    memset(&loadJob, 0, sizeof(loadJob));
}

static void build_panel_paths(const char *mediaUrl, int page, char *base, size_t baseLen,
                              char *gifPath, size_t gifLen,
                              char *texPath, size_t texLen,
                              char *animPath, size_t animLen) {
    mspa_panel_path_from_url(mediaUrl, page, 1, 1, base, baseLen);
    snprintf(gifPath,  gifLen,  "%s.gif",  base);
    /* texPath = frame-0 tex; used for single-frame pages AND as the cache-exists check */
    mspa_panel_frame_tex_path(base, 0, texPath, texLen);
    snprintf(animPath, animLen, "%s.anim", base);
}

static void begin_page_load(int n) {
    if (loadJob.active) return;
    if (n < startPage) n = startPage;
    pageNum = n;

    panel_anim_free();
    mspa_image_free(&curImage);

    load_job_reset();
    loadJob.active = true;
    loadJob.targetPage = n;
    loadJob.stage = LOAD_PAGE_JSON;
    state = STATE_LOADING;
    pageLoadedOk = false;
    textScrollY = 0;
    needsRedraw = true;
}

static void commit_loaded_page(void) {
    mspa_page_free(&curPage);
    mspa_image_free(&curImage);

    /* Transfer ownership of page data */
    curPage = loadJob.page;
    memset(&loadJob.page, 0, sizeof(loadJob.page));

    /* Fix page number BEFORE starting animation so path building is correct */
    if (curPage.page == 0)
        curPage.page = loadJob.targetPage;
    pageNum = curPage.page;

    /* Transfer image ownership — zero loadJob.image WITHOUT freeing the GPU
       texture, since curImage now owns it. */
    curImage = loadJob.image;
    curImage.image.tex    = &curImage.tex;
    curImage.image.subtex = &curImage.subtex;
    memset(&loadJob.image, 0, sizeof(loadJob.image)); /* ownership transferred */

    /* Start animation AFTER pageNum is correct */
    panel_anim_start_for_page(&curPage, pageNum);

    resumeAvailable = find_latest_cached_page(&resumePage);
    pageLoadedOk = true;
    state = STATE_READING;
    needsRedraw = true;
}

static void fail_loaded_page(void) {
    load_job_reset();
    panel_anim_free();
    state = (curPage.page > 0) ? STATE_READING : STATE_MENU;
    pageLoadedOk = false;
    needsRedraw = true;
}

static void advance_load_job(void) {
    if (!loadJob.active) return;

    switch (loadJob.stage) {
    case LOAD_PAGE_JSON: {
        if (!mspa_page_load(loadJob.targetPage, &loadJob.page)) {
            loadJob.stage = LOAD_FAIL;
            break;
        }
        if (loadJob.page.page == 0)
            loadJob.page.page = loadJob.targetPage;
        mspa_page_save_sd(loadJob.targetPage, &loadJob.page);
        resumeAvailable = find_latest_cached_page(&resumePage);

        if (loadJob.page.media && loadJob.page.media[0]) {
            build_panel_paths(loadJob.page.media, loadJob.targetPage,
                              loadJob.basePath, sizeof(loadJob.basePath),
                              loadJob.gifPath, sizeof(loadJob.gifPath),
                              loadJob.texPath, sizeof(loadJob.texPath),
                              loadJob.animPath, sizeof(loadJob.animPath));
            if (file_exists(loadJob.texPath)) {
                loadJob.stage = LOAD_TEX_LOAD;
            } else if (file_exists(loadJob.gifPath)) {
                loadJob.stage = LOAD_GIF_READ;
            } else {
                loadJob.stage = LOAD_GIF_DOWNLOAD;
            }
        } else {
            loadJob.stage = LOAD_COMMIT;
        }
        break;
    }

    case LOAD_GIF_DOWNLOAD:
        if (!mspa_panel_download_gif(loadJob.page.media, loadJob.gifPath, NULL)) {
            loadJob.stage = LOAD_FAIL;
            break;
        }
        loadJob.stage = LOAD_GIF_READ;
        break;

    case LOAD_GIF_READ:
        if (!read_entire_file(loadJob.gifPath, &loadJob.gifBytes, &loadJob.gifBytesSize)) {
            loadJob.stage = LOAD_FAIL;
            break;
        }
        /* open the streaming converter — gifBytes stays alive until DELETE */
        if (!mspa_gif_converter_open(&loadJob.converter,
                                     loadJob.gifBytes, loadJob.gifBytesSize,
                                     loadJob.basePath, loadJob.animPath)) {
            loadJob.stage = LOAD_FAIL;
            break;
        }
        loadJob.stage = LOAD_GIF_CONVERT_FRAME;
        break;

    case LOAD_GIF_CONVERT_FRAME:
        /* convert exactly one frame per game-loop tick */
        mspa_gif_converter_step(&loadJob.converter);
        if (loadJob.converter.failed) {
            loadJob.stage = LOAD_FAIL;
        } else if (loadJob.converter.done) {
            /* close converter and free the gif bytes — we're done with them */
            mspa_gif_converter_close(&loadJob.converter);
            free(loadJob.gifBytes);
            loadJob.gifBytes = NULL;
            loadJob.gifBytesSize = 0;
            loadJob.stage = LOAD_GIF_DELETE;
        }
        /* otherwise stay in LOAD_GIF_CONVERT_FRAME for next tick */
        break;

    case LOAD_GIF_DELETE:
        remove(loadJob.gifPath);
        loadJob.stage = LOAD_TEX_LOAD;
        break;

    case LOAD_TEX_LOAD:
        if (!mspa_panel_load_tex_file(loadJob.texPath, &loadJob.image)) {
            /* stale or corrupt cache: drop tex + anim manifest and rebuild */
            remove(loadJob.texPath);
            remove(loadJob.animPath);
            if (file_exists(loadJob.gifPath)) {
                loadJob.stage = LOAD_GIF_READ;
            } else {
                loadJob.stage = LOAD_GIF_DOWNLOAD;
            }
            break;
        }
        loadJob.stage = LOAD_COMMIT;
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

static void render_top(void) {
    C3D_FrameBegin(C3D_FRAME_SYNCDRAW);

    /* Top screen: grey background, panel centered */
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

/* ── bottom-screen constants ──────────────────────────────────────── */
#define BOT_W        320
#define BOT_H        240
#define SIDEBAR_W     20
#define BOT_PAD_X     10
#define BOT_PAD_TOP    6
#define BOT_LINE_H    18   /* px per text line                        */
#define BOT_TITLE_SZ  0.80f
#define BOT_BODY_SZ   0.50f
#define BOT_CMD_SZ    0.72f

/* colours */
#define COL_BG        C2D_Color32(0xE8,0xE8,0xE8,0xFF)
#define COL_SIDEBAR   C2D_Color32(0xD0,0xD0,0xD0,0xFF)
#define COL_WHITE     C2D_Color32(0xFF,0xFF,0xFF,0xFF)
#define COL_BLACK     C2D_Color32(0x00,0x00,0x00,0xFF)
#define COL_DIV       C2D_Color32(0xAA,0xAA,0xAA,0xFF)
#define COL_BLUE      C2D_Color32(0x00,0x00,0xCC,0xFF)
#define COL_GREY      C2D_Color32(0x77,0x77,0x77,0xFF)

/* text area x range (inside card) */
#define TEXT_X_START  (SIDEBAR_W + BOT_PAD_X)
#define TEXT_X_END    (BOT_W - SIDEBAR_W - BOT_PAD_X)
#define TEXT_AVAIL_W  (TEXT_X_END - TEXT_X_START)

/* title bar bottom y, divider y */
#define TITLE_Y       BOT_PAD_TOP
#define DIV_Y         (TITLE_Y + 18)
#define BODY_Y_START  (DIV_Y + 4)
#define BODY_Y_END    (BOT_H - 4)

static C2D_TextBuf textBuf = NULL;

static void bot_init(void) {
    textBuf = C2D_TextBufNew(4096);
}
static void bot_free(void) {
    if (textBuf) { C2D_TextBufDelete(textBuf); textBuf = NULL; }
}

/* Draw a string clipped within [clipX, clipX+clipW] at pixel (x,y).
   Returns rendered width. */
static float draw_text(const char *str, float x, float y, float z,
                       float sz, u32 color) {
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

/*
 * measure_text_width: measure pixel width of a NUL-terminated string.
 */
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

/*
 * draw_wrapped_text:
 *   Draws `text` word-wrapped inside `maxPx` pixels starting at (x, y).
 *   Returns the Y position after the last line drawn.
 *   Never mutates the source string.
 */
static float draw_wrapped_text(const char *text, float x, float startY,
                               float maxPx, float sz, u32 color) {
    if (!text || !*text || !textBuf) return startY;

    /* line height derived from font size */
    float lineH = (sz >= 0.58f) ? 18.0f : (sz >= 0.50f ? 16.0f : 14.0f);
    float y = startY;

    /* scratch buffer for a single line */
    char lineBuf[256];

    const char *p = text;
    while (*p) {
        /* skip lone \r */
        while (*p == '\r') p++;
        if (!*p) break;

        /* explicit newline → blank line */
        if (*p == '\n') { p++; y += lineH; continue; }

        /* find the longest prefix of [p…] that fits in maxPx */
        const char *lineStart = p;
        const char *lastSpace = NULL;   /* last space inside the fit range */
        const char *scan      = p;

        while (*scan && *scan != '\n') {
            /* copy [lineStart..scan] into lineBuf for measurement */
            size_t len = (size_t)(scan - lineStart) + 1;
            if (len >= sizeof(lineBuf)) { scan++; break; } /* safety */
            memcpy(lineBuf, lineStart, len);
            lineBuf[len] = '\0';

            float w = measure_text_width(lineBuf, sz);
            if (w > maxPx) {
                /* this char doesn't fit — break before it */
                if (lastSpace && lastSpace > lineStart) {
                    scan = lastSpace; /* break at last space */
                }
                /* else break right here (word longer than line) */
                break;
            }
            if (*scan == ' ') lastSpace = scan;
            scan++;
        }

        /* draw [lineStart .. scan) */
        size_t drawLen = (size_t)(scan - lineStart);
        /* trim trailing spaces */
        while (drawLen > 0 && lineStart[drawLen - 1] == ' ') drawLen--;

        if (drawLen > 0 && drawLen < sizeof(lineBuf)) {
            memcpy(lineBuf, lineStart, drawLen);
            lineBuf[drawLen] = '\0';
            draw_text(lineBuf, x, y, 0.3f, sz, color);
        }
        y += lineH;

        /* advance p past the chunk we just drew */
        p = scan;
        /* skip the breaking space (but not a newline — handled top of loop) */
        if (*p == ' ') p++;
        /* skip explicit newline */
        if (*p == '\n') { p++; }
    }

    return y;
}

static void render_bottom(void) {
    C2D_SceneBegin(botTarget);
    C2D_TargetClear(botTarget, COL_BG);

    /* sidebars */
    C2D_DrawRectSolid(0,               0, 0.1f, SIDEBAR_W,     BOT_H, COL_SIDEBAR);
    C2D_DrawRectSolid(BOT_W - SIDEBAR_W, 0, 0.1f, SIDEBAR_W,   BOT_H, COL_SIDEBAR);

    /* white card */
    C2D_DrawRectSolid(SIDEBAR_W, 0, 0.1f, BOT_W - SIDEBAR_W * 2, BOT_H, COL_WHITE);

    const float TITLE_SZ = 0.60f;
    const float BODY_SZ  = 0.45f;
    const float CMD_SZ   = 0.52f;
    const int   LINE_H   = 18;

    switch (state) {
    case STATE_MENU:
        draw_text("MSPA-3DS", TEXT_X_START, TITLE_Y, 0.3f, TITLE_SZ, COL_BLACK);
        draw_text("SELECT A COMIC:", TEXT_X_START, BODY_Y_START + LINE_H * 1, 0.3f, BODY_SZ, COL_BLACK);
        draw_text("> A: HOMESTUCK", TEXT_X_START, BODY_Y_START + LINE_H * 3, 0.3f, BODY_SZ, COL_BLACK);
        if (resumeAvailable)
            draw_text("> B: CONTINUE", TEXT_X_START, BODY_Y_START + LINE_H * 4, 0.3f, BODY_SZ, COL_BLACK);
        else
            draw_text("> B: NO SAVE", TEXT_X_START, BODY_Y_START + LINE_H * 4, 0.3f, BODY_SZ, COL_GREY);
	draw_text("> X: TELEPORT TO PAGE", TEXT_X_START, BODY_Y_START + LINE_H * 5, 0.3f, BODY_SZ, COL_BLACK);
        draw_text("> START: Quit", TEXT_X_START, BODY_Y_START + LINE_H * 7, 0.3f, BODY_SZ, COL_GREY);
        break;

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
    // 1. Draw the Header (Not scrolled)
    currentY = draw_wrapped_text(typeStr, TEXT_X_START, currentY, (float)TEXT_AVAIL_W, TITLE_SZ, COL_BLACK);

    currentY += 8.0f;
    
    C2D_Flush(); 
    
    // Standard Citra/3DS Hardware Scissor logic for the bottom screen:
    // We want to clip Y from BODY_Y_START to BODY_Y_END
    // and X from SIDEBAR_W to (BOT_W - SIDEBAR_W)
    // Because of the screen rotation, we map it like this:
    int scissorX = BODY_Y_START;
    int scissorY = SIDEBAR_W;
    int scissorW = BODY_Y_END - BODY_Y_START;
    int scissorH = BOT_W - (SIDEBAR_W * 2);
    int clipTop = (int)currentY;
    int clipBottom = BODY_Y_END;

    C3D_SetScissor(GPU_SCISSOR_NORMAL, clipTop, SIDEBAR_W, clipBottom, BOT_W - SIDEBAR_W);

    // 3. Draw text with the Y offset (subtracted by textScrollY)
    float drawY = currentY - textScrollY;

    for (size_t i = 0; i < curPage.textCount; i++) {
        if (!curPage.text[i] || !curPage.text[i][0]) continue;
        drawY = draw_wrapped_text(curPage.text[i], TEXT_X_START, drawY, (float)TEXT_AVAIL_W, BODY_SZ, COL_BLACK);
        drawY += 4.0f;
        // REMOVE the 'if (y > BODY_Y_END) break' so it calculates all lines
    }

    if (curPage.command && curPage.command[0]) {
        drawY += 2.0f;
        drawY = draw_wrapped_text(curPage.command, TEXT_X_START, drawY, (float)TEXT_AVAIL_W, CMD_SZ, COL_BLUE);
    }

    // 4. Reset Scissor
    C2D_Flush();
    C3D_SetScissor(GPU_SCISSOR_DISABLE, 0, 0, 0, 0);

    break;
}
    }

    needsRedraw = false;
    C3D_FrameEnd(0);
}

int read_resume_page() {
    FILE* f = fopen("sdmc:/mspa/resume.txt", "r");
    if (!f) return -1;

    int p = -1;
    if (fscanf(f, "%d", &p) != 1) {
        p = -1;
    }

    fclose(f);
    return p;
}

void save_resume_page(int page) {
    // Create the directory just in case it doesn't exist
    mkdir("sdmc:/mspa", 0777); 

    FILE* f = fopen("sdmc:/mspa/resume.txt", "w");
    if (f) {
        fprintf(f, "%d", page);
        fclose(f);
        
        // Update the globals so the menu reacts immediately
        resumePage = page;
        resumeAvailable = true;
    }
}


int main(void) {
    gfxInitDefault();
    romfsInit();

    ensure_sd_dirs();

    socBuf = (u32 *)memalign(SOC_ALIGN, SOC_BUFFERSIZE);
    if (!socBuf || R_FAILED(socInit(socBuf, SOC_BUFFERSIZE))) {
        printf("socInit failed\n");
        goto done_gfx;
    }
    if (R_FAILED(httpcInit(0))) {
        printf("httpcInit failed\n");
        goto done_soc;
    }

    C3D_Init(C3D_DEFAULT_CMDBUF_SIZE);
    C2D_Init(C2D_DEFAULT_MAX_OBJECTS);
    C2D_Prepare();
    topTarget = C2D_CreateScreenTarget(GFX_TOP, GFX_LEFT);
    botTarget = C2D_CreateScreenTarget(GFX_BOTTOM, GFX_LEFT);
    bot_init();
    mspa_image_make_placeholder(&placeholder);

    resumeAvailable = read_resume_page();
	if (resumeAvailable) {
    		resumePage = read_resume_page(); 
	} else {
    		resumePage = startPage;
     }

    while (aptMainLoop()) {
        hidScanInput();
        u32 kDown = hidKeysDown();
        u32 kHeld = hidKeysHeld(); 

        if (kDown & KEY_START)
            break;

        if (!loadJob.active) {
            switch (state) {
            case STATE_MENU:
                if (kDown & KEY_A) {
                    begin_page_load(startPage);
                }
                if (resumeAvailable && (kDown & KEY_B)) {
                    begin_page_load(resumePage);
                }

		if (kDown & KEY_X) {
        static SwkbdState swkbd;
        char inputBuf[10]; // Buffer for the page number string
        
        // Initialize the keyboard as a Numpad
        swkbdInit(&swkbd, SWKBD_TYPE_NUMPAD, 1, 7); 
        swkbdSetHintText(&swkbd, "Enter page number...");
        
        // Launch the keyboard (this is a blocking call)
        SwkbdButton button = swkbdInputText(&swkbd, inputBuf, sizeof(inputBuf));
        
        // If the user didn't press 'Cancel'
        if (button != SWKBD_BUTTON_NONE) {
            int targetPage = atoi(inputBuf); // Convert string to integer
            if (targetPage > 0) {
                begin_page_load(targetPage);
            }
        }
        needsRedraw = true; // Tell the engine to redraw the menu after the KB closes
    }

                break;

            case STATE_READING:
                // Scrolling logic
                if (kHeld & KEY_UP) {
                    textScrollY -= 4;
                    if (textScrollY < 0) textScrollY = 0;
                    needsRedraw = true;
                }
                if (kHeld & KEY_DOWN) {
                    textScrollY += 4;
                    needsRedraw = true;
                }  

                // Navigation logic
                if (kDown & KEY_A) {
                    int nextPage = curPage.next;
                    if (nextPage <= pageNum)
                        nextPage = pageNum + 1;
                    begin_page_load(nextPage);
		    save_resume_page(pageNum);
                }
                if ((kDown & KEY_B) && pageNum > startPage) {
                    begin_page_load(pageNum - 1);
                }
                if (kDown & KEY_X) {
                    panel_anim_free();
                    state = STATE_MENU;
                    needsRedraw = true;
                }
                break; // This closes STATE_READING

            case STATE_LOADING:
                break;
            } // This closes the switch(state)
        } // This closes the if(!loadJob.active)

        if (loadJob.active) {
            advance_load_job();
        } else {
            panel_anim_tick();
        }

        render_top();
        render_bottom();
    }

    mspa_image_free(&curImage);
    mspa_image_free(&placeholder);
    mspa_page_free(&curPage);
    panel_anim_free();
    load_job_reset();
    bot_free();
    C2D_Fini();
    C3D_Fini();
    httpcExit();

done_soc:
    socExit();
    if (socBuf) { free(socBuf); socBuf = NULL; }

done_gfx:
    romfsExit();
    gfxExit();
    return 0;
}