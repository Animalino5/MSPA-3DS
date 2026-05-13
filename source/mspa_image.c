#include "mspa_image.h"
#include "mspa_http.h"

#include <3ds/services/httpc.h>
#include <libnsgif.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <malloc.h>
#include <sys/stat.h>
#include <errno.h>

#define PANEL_DIR  "sdmc:/3ds/MSPA-3DS/panels"
#define LOG_PATH   "sdmc:/3ds/MSPA-3DS/debug.log"
#define RAW_MAGIC   0x53524134u  /* "SRA4" — raw compressed image bytes */
#define TEX_MAGIC   0x58455435u  /* "TEX5" */
#define MAX_IMAGE_BYTES (48u * 1024u * 1024u)
#define ANIM_MAGIC 0x53485432u  /* "SHT2" */

static void ensure_dirs(void) {
    mkdir("sdmc:/3ds", 0777);
    mkdir("sdmc:/3ds/MSPA-3DS", 0777);
    mkdir(PANEL_DIR, 0777);
}

static void log_msg(const char *tag, const char *msg) {
    ensure_dirs();
    FILE *f = fopen(LOG_PATH, "a");
    if (!f) return;
    fprintf(f, "[%s] %s\n", tag, msg);
    fclose(f);
}

static void log_err(const char *url, Result rc, const char *step) {
    ensure_dirs();
    FILE *f = fopen(LOG_PATH, "a");
    if (!f) return;
    fprintf(f, "[ERR] step=%s rc=%08lX url=%s\n", step, rc, url ? url : "?");
    fclose(f);
}

static int next_pow2(int v) {
    int p = 64;
    while (p < v) p <<= 1;
    return p;
}

static void *gif_bitmap_create(int width, int height) {
    if (width <= 0 || height <= 0) return NULL;
    if ((unsigned long long)width * (unsigned long long)height > (MAX_IMAGE_BYTES / 4u))
        return NULL;
    return calloc((size_t)width * (size_t)height, 4);
}

static void gif_bitmap_destroy(void *bitmap) {
    free(bitmap);
}

static unsigned char *gif_bitmap_get_buffer(void *bitmap) {
    return (unsigned char *)bitmap;
}

static void gif_bitmap_set_opaque(void *bitmap, bool opaque) {
    (void)bitmap; (void)opaque;
}

static bool gif_bitmap_test_opaque(void *bitmap) {
    (void)bitmap;
    return false;
}

static void gif_bitmap_modified(void *bitmap) {
    (void)bitmap;
}

static bool read_entire_file(const char *path, uint8_t **bufOut, size_t *sizeOut) {
    if (bufOut) *bufOut = NULL;
    if (sizeOut) *sizeOut = 0;

    FILE *f = fopen(path, "rb");
    if (!f) return false;

    if (fseek(f, 0, SEEK_END) != 0) {
        fclose(f);
        return false;
    }
    long len = ftell(f);
    if (len < 0) {
        fclose(f);
        return false;
    }
    rewind(f);

    uint8_t *buf = (uint8_t *)malloc((size_t)len);
    if (!buf) {
        fclose(f);
        return false;
    }

    size_t got = fread(buf, 1, (size_t)len, f);
    fclose(f);
    if (got != (size_t)len) {
        free(buf);
        return false;
    }

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

static void frame_tex_path(const char *texBasePath, uint32_t frameIndex, char *out, size_t outLen) {
    if (!out || outLen == 0) return;
    if (!texBasePath || !texBasePath[0]) {
        snprintf(out, outLen, "frame-%03u.tex", (unsigned)frameIndex);
        return;
    }
    snprintf(out, outLen, "%s-%03u.tex", texBasePath, (unsigned)frameIndex);
}

void mspa_panel_frame_tex_path(const char *texBasePath, uint32_t frameIndex, char *out, size_t outLen) {
    frame_tex_path(texBasePath, frameIndex, out, outLen);
}

static bool write_anim_manifest(const char *animPath, const MspaAnimManifest *meta, const uint32_t *delaysMs) {
    if (!animPath || !meta || !delaysMs || meta->frameCount == 0) return false;
    FILE *f = fopen(animPath, "wb");
    if (!f) return false;
    uint32_t magic   = ANIM_MAGIC;
    uint32_t version = 2;  /* version 2 = per-frame tex files */
    bool ok = fwrite(&magic,   4, 1, f) == 1 &&
              fwrite(&version, 4, 1, f) == 1 &&
              fwrite(meta, sizeof(*meta), 1, f) == 1 &&
              fwrite(delaysMs, 4, meta->frameCount, f) == meta->frameCount;
    fclose(f);
    if (!ok) remove(animPath);
    return ok;
}

bool mspa_panel_save_anim_manifest(const char *animPath, const MspaAnimManifest *meta, const uint32_t *delaysMs) {
    return write_anim_manifest(animPath, meta, delaysMs);
}

bool mspa_panel_load_anim_manifest(const char *animPath, MspaAnimManifest *metaOut, uint32_t **delaysMsOut) {
    if (delaysMsOut) *delaysMsOut = NULL;
    if (metaOut) memset(metaOut, 0, sizeof(*metaOut));
    if (!animPath || !metaOut || !delaysMsOut) return false;
    FILE *f = fopen(animPath, "rb");
    if (!f) return false;
    uint32_t magic = 0, version = 0;
    if (fread(&magic, 4, 1, f) != 1 || magic != ANIM_MAGIC ||
        fread(&version, 4, 1, f) != 1) {
        fclose(f);
        return false;
    }
    /* version 1 = old spritesheet format (no longer supported, force rebuild) */
    if (version != 2) {
        fclose(f);
        return false;
    }
    MspaAnimManifest meta = {0};
    if (fread(&meta, sizeof(meta), 1, f) != 1 ||
        meta.frameCount == 0 || meta.frameCount > 4096) {
        fclose(f);
        return false;
    }
    uint32_t *delays = (uint32_t *)calloc(meta.frameCount, sizeof(uint32_t));
    if (!delays) { fclose(f); return false; }
    if (fread(delays, 4, meta.frameCount, f) != meta.frameCount) {
        free(delays);
        fclose(f);
        return false;
    }
    fclose(f);
    *metaOut = meta;
    *delaysMsOut = delays;
    return true;
}

static bool convert_and_save_one_frame(const uint8_t *rgba, int w, int h, const char *texPath) {
    uint8_t *scaled = NULL;
    int dw = 0, dh = 0;
    if (!resize_rgba_nearest(rgba, w, h, &scaled, &dw, &dh)) return false;
    MspaImage temp = {0};
    bool ok = mspa_panel_rgba_to_tex_file(scaled, dw, dh, texPath, &temp);
    mspa_image_free(&temp);
    free(scaled);
    return ok;
}

static bool upload_rgba(MspaImage *out, int w, int h, const uint8_t *rgba) {
    if (!out || !rgba || w <= 0 || h <= 0) return false;
    mspa_image_free(out);

    if (w > 1024 || h > 1024) return false;

    int tw = next_pow2(w);
    int th = next_pow2(h);
    size_t texBytes = (size_t)tw * (size_t)th * 4u;

    if (!C3D_TexInit(&out->tex, (u16)tw, (u16)th, GPU_RGBA8)) {
        log_msg("TEX", "C3D_TexInit failed");
        return false;
    }

    C3D_TexSetFilter(&out->tex, GPU_LINEAR, GPU_LINEAR);
    C3D_TexSetWrap(&out->tex, GPU_CLAMP_TO_EDGE, GPU_CLAMP_TO_EDGE);
    out->tex.border = 0x00000000u;

    uint8_t *src = (uint8_t *)linearAlloc(texBytes);
    if (!src) {
        C3D_TexDelete(&out->tex);
        memset(out, 0, sizeof(*out));
        return false;
    }
    memset(src, 0, texBytes);

    /* libnsgif gives us R,G,B,A bytes.
       GX_TRANSFER_FMT_RGBA8 on the 3DS reads memory as A,B,G,R
       (GPU stores 0xRRGGBBAA big-endian → little-endian bytes = A,B,G,R).
       So we must swizzle: dst[0]=A, dst[1]=B, dst[2]=G, dst[3]=R */
    for (int y = 0; y < h; y++) {
        const uint8_t *row_in  = rgba + (size_t)y * (size_t)w * 4u;
        uint8_t       *row_out = src  + (size_t)y * (size_t)tw * 4u;
        for (int x = 0; x < w; x++) {
            row_out[x*4+0] = row_in[x*4+3]; /* A */
            row_out[x*4+1] = row_in[x*4+2]; /* B */
            row_out[x*4+2] = row_in[x*4+1]; /* G */
            row_out[x*4+3] = row_in[x*4+0]; /* R */
        }
    }

    GSPGPU_FlushDataCache(src, (u32)texBytes);

    Result ret = GX_DisplayTransfer(
        (u32 *)src, GX_BUFFER_DIM((u32)tw, (u32)th),
        (u32 *)out->tex.data, GX_BUFFER_DIM((u32)tw, (u32)th),
        GX_TRANSFER_FLIP_VERT(0) |
        GX_TRANSFER_OUT_TILED(1) |
        GX_TRANSFER_RAW_COPY(0) |
        GX_TRANSFER_IN_FORMAT(GX_TRANSFER_FMT_RGBA8) |
        GX_TRANSFER_OUT_FORMAT(GX_TRANSFER_FMT_RGBA8) |
        GX_TRANSFER_SCALING(GX_TRANSFER_SCALE_NO)
    );

    gspWaitForPPF();
    C3D_TexFlush(&out->tex);
    linearFree(src);

    if (R_FAILED(ret)) {
        C3D_TexDelete(&out->tex);
        memset(out, 0, sizeof(*out));
        return false;
    }

    out->subtex.left   = 0.0f;
    out->subtex.top    = 1.0f;
    out->subtex.right  = (float)w / (float)tw;
    out->subtex.bottom = 1.0f - ((float)h / (float)th);
    out->subtex.width  = (u16)w;
    out->subtex.height = (u16)h;
    out->image.tex     = &out->tex;
    out->image.subtex  = &out->subtex;
    out->width  = w;
    out->height = h;
    out->valid  = true;
    return true;
}

static bool http_fetch_raw(const char *url, uint8_t **bufOut, size_t *sizeOut) {
    Result ret = 0;
    httpcContext ctx;
    char *newurl = NULL;
    u32 statuscode = 0, readsize = 0, size = 0;
    uint8_t *buf = NULL, *lastbuf = NULL;
    char cur[1024];

    if (!url) return false;
    snprintf(cur, sizeof(cur), "%s", url);

    if (bufOut) *bufOut = NULL;
    if (sizeOut) *sizeOut = 0;

    do {
        ret = httpcOpenContext(&ctx, HTTPC_METHOD_GET, cur, 1);
        if (R_FAILED(ret)) {
            log_err(cur, ret, "OpenContext");
            if (newurl) free(newurl);
            return false;
        }
        mspa_http_apply_request_headers(&ctx, "http://mspa.chadthundercock.com/");

        ret = httpcBeginRequest(&ctx);
        if (R_FAILED(ret)) {
            log_err(cur, ret, "BeginRequest");
            httpcCloseContext(&ctx);
            if (newurl) free(newurl);
            return false;
        }

        ret = httpcGetResponseStatusCode(&ctx, &statuscode);
        if (R_FAILED(ret)) {
            log_err(cur, ret, "GetStatus");
            httpcCloseContext(&ctx);
            if (newurl) free(newurl);
            return false;
        }

        if ((statuscode >= 301 && statuscode <= 303) || (statuscode >= 307 && statuscode <= 308)) {
            if (!newurl) newurl = (char *)malloc(0x1000);
            if (!newurl) { httpcCloseContext(&ctx); return false; }
            ret = httpcGetResponseHeader(&ctx, "Location", newurl, 0x1000);
            if (R_FAILED(ret)) { httpcCloseContext(&ctx); free(newurl); return false; }
            char resolved[1024];
            if (!mspa_http_resolve_location(cur, newurl, resolved, sizeof(resolved))) {
                httpcCloseContext(&ctx); free(newurl); return false;
            }
            snprintf(cur, sizeof(cur), "%s", resolved);
            httpcCloseContext(&ctx);
        }
    } while ((statuscode >= 301 && statuscode <= 303) || (statuscode >= 307 && statuscode <= 308));

    if (statuscode != 200) {
        httpcCloseContext(&ctx);
        if (newurl) free(newurl);
        return false;
    }

    buf = (uint8_t *)malloc(0x4000);
    if (!buf) { httpcCloseContext(&ctx); if (newurl) free(newurl); return false; }

    do {
        ret = httpcDownloadData(&ctx, buf + size, 0x4000, &readsize);
        size += readsize;
        if (ret == (s32)HTTPC_RESULTCODE_DOWNLOADPENDING) {
            lastbuf = buf;
            buf = (uint8_t *)realloc(buf, size + 0x4000);
            if (!buf) { httpcCloseContext(&ctx); free(lastbuf); if (newurl) free(newurl); return false; }
        }
    } while (ret == (s32)HTTPC_RESULTCODE_DOWNLOADPENDING);

    httpcCloseContext(&ctx);
    if (newurl) free(newurl);
    if (ret != 0) { free(buf); return false; }

    *bufOut = buf;
    *sizeOut = size;
    return size > 0;
}

bool mspa_panel_decode_gif_bytes(const uint8_t *gifData, size_t gifSize,
                                 int *w, int *h, uint8_t **rgbaOut) {
    if (!gifData || gifSize == 0 || !w || !h || !rgbaOut) return false;

    gif_bitmap_callback_vt bitmap_callbacks = {
        gif_bitmap_create,
        gif_bitmap_destroy,
        gif_bitmap_get_buffer,
        gif_bitmap_set_opaque,
        gif_bitmap_test_opaque,
        gif_bitmap_modified
    };

    gif_animation gif;
    memset(&gif, 0, sizeof(gif));
    gif_create(&gif, &bitmap_callbacks);

    gif_result code;
    do {
        code = gif_initialise(&gif, (size_t)gifSize, (uint8_t *)gifData);
        if (code != GIF_OK && code != GIF_WORKING) {
            log_msg("GIF", "gif_initialise failed");
            gif_finalise(&gif);
            return false;
        }
    } while (code != GIF_OK);

    code = gif_decode_frame(&gif, 0);
    if (code != GIF_OK) {
        log_msg("GIF", "gif_decode_frame failed");
        gif_finalise(&gif);
        return false;
    }

    if (gif.width <= 0 || gif.height <= 0 || !gif.frame_image) {
        gif_finalise(&gif);
        return false;
    }

    size_t bytes = (size_t)gif.width * (size_t)gif.height * 4u;
    uint8_t *copy = (uint8_t *)malloc(bytes);
    if (!copy) {
        gif_finalise(&gif);
        return false;
    }
    memcpy(copy, gif.frame_image, bytes);

    *w = gif.width;
    *h = gif.height;
    *rgbaOut = copy;
    gif_finalise(&gif);
    return true;
}

void mspa_panel_path_from_url(const char *url, int page, int imageIndex, int frame,
                              char *out, size_t outLen) {
    const char *lastSlash = url ? strrchr(url, '/') : NULL;
    char slug[64] = "panel";
    if (lastSlash && strlen(lastSlash) > 1) {
        strncpy(slug, lastSlash + 1, sizeof(slug) - 1);
        slug[sizeof(slug) - 1] = '\0';
        char *dot = strchr(slug, '.');
        if (dot) *dot = '\0';
    }

    snprintf(out, outLen, "%s/%06d_%02d_%s-%03d", PANEL_DIR, page, imageIndex, slug, frame);
}

static bool write_tex_file(const char *texPath, const MspaImage *img) {
    if (!texPath || !img || !img->valid) return false;

    FILE *f = fopen(texPath, "wb");
    if (!f) return false;

    uint32_t magic = TEX_MAGIC;
    uint32_t width = (uint32_t)img->width;
    uint32_t height = (uint32_t)img->height;
    uint32_t texW = (uint32_t)img->tex.width;
    uint32_t texH = (uint32_t)img->tex.height;
    uint32_t format = (uint32_t)GPU_RGBA8;
    uint32_t dataSize = (uint32_t)(texW * texH * 4u);

    bool ok =
        fwrite(&magic, 4, 1, f) == 1 &&
        fwrite(&width, 4, 1, f) == 1 &&
        fwrite(&height, 4, 1, f) == 1 &&
        fwrite(&texW, 4, 1, f) == 1 &&
        fwrite(&texH, 4, 1, f) == 1 &&
        fwrite(&format, 4, 1, f) == 1 &&
        fwrite(&dataSize, 4, 1, f) == 1 &&
        fwrite(img->tex.data, 1, dataSize, f) == dataSize;

    fclose(f);
    if (!ok) remove(texPath);
    return ok;
}

bool mspa_panel_rgba_to_tex_file(const uint8_t *rgba, int w, int h,
                                 const char *texPath, MspaImage *temp) {
    if (!rgba || w <= 0 || h <= 0 || !texPath) return false;

    MspaImage local = {0};
    MspaImage *img = temp ? temp : &local;

    if (!upload_rgba(img, w, h, rgba)) {
        if (!temp) mspa_image_free(img);
        return false;
    }

    bool ok = write_tex_file(texPath, img);
    if (!temp) mspa_image_free(img);
    return ok;
}

bool mspa_panel_download_gif(const char *url, const char *gifPath,
                             void (*status)(const char *)) {
    if (status) status("downloading gif...");

    uint8_t *raw = NULL;
    size_t rawSz = 0;
    if (!http_fetch_raw(url, &raw, &rawSz)) {
        if (status) status("FAIL: download");
        return false;
    }

    FILE *f = fopen(gifPath, "wb");
    if (!f) {
        free(raw);
        if (status) status("FAIL: write gif");
        return false;
    }

    bool ok = fwrite(raw, 1, rawSz, f) == rawSz;
    fclose(f);
    free(raw);

    if (!ok) {
        remove(gifPath);
        if (status) status("FAIL: gif save");
        return false;
    }

    return true;
}

static bool load_gif_file_to_rgba(const char *gifPath, uint8_t **rgba, int *w, int *h) {
    uint8_t *bytes = NULL;
    size_t size = 0;
    if (!read_entire_file(gifPath, &bytes, &size)) return false;

    bool ok = mspa_panel_decode_gif_bytes(bytes, size, w, h, rgba);
    free(bytes);
    return ok;
}

bool mspa_panel_gif_to_tex_file(const char *gifPath, const char *texPath,
                                void (*status)(const char *)) {
    if (status) status("decode gif...");

    int sw = 0, sh = 0;
    uint8_t *pixels = NULL;
    if (!load_gif_file_to_rgba(gifPath, &pixels, &sw, &sh)) {
        if (status) status("FAIL: decode");
        return false;
    }

    if (status) status("make tex...");
    bool ok = convert_and_save_one_frame(pixels, sw, sh, texPath);
    free(pixels);

    if (!ok) {
        if (status) status("FAIL: tex save");
        return false;
    }

    if (status) status("tex saved");
    return true;
}


bool mspa_gif_converter_open(MspaGifConverter *cv,
                             const uint8_t *gifData, size_t gifSize,
                             const char *texBasePath, const char *animPath) {
    if (!cv || !gifData || gifSize == 0 || !texBasePath || !animPath) return false;
    memset(cv, 0, sizeof(*cv));

    gif_animation *gif = (gif_animation *)calloc(1, sizeof(gif_animation));
    if (!gif) return false;

    gif_bitmap_callback_vt cbs = {
        gif_bitmap_create, gif_bitmap_destroy, gif_bitmap_get_buffer,
        gif_bitmap_set_opaque, gif_bitmap_test_opaque, gif_bitmap_modified
    };
    gif_create(gif, &cbs);

    gif_result code;
    do {
        code = gif_initialise(gif, gifSize, (unsigned char *)gifData);
        if (code != GIF_OK && code != GIF_WORKING) {
            log_msg("GIF", "gif_initialise failed");
            gif_finalise(gif);
            free(gif);
            return false;
        }
    } while (code != GIF_OK);

    uint32_t fc = gif->frame_count ? gif->frame_count : gif->frame_count_partial;
    if (fc == 0 || fc > 4096) {
        gif_finalise(gif);
        free(gif);
        return false;
    }

    uint32_t *delays = (uint32_t *)calloc(fc, sizeof(uint32_t));
    if (!delays) {
        gif_finalise(gif);
        free(gif);
        return false;
    }

    cv->_gif      = gif;
    cv->_gifData  = (uint8_t *)gifData; /* not owned */
    cv->_gifSize  = gifSize;
    cv->_delays   = delays;
    cv->frameCount = fc;
    cv->frameDone  = 0;
    cv->done       = false;
    cv->failed     = false;
    strncpy(cv->_basePath, texBasePath, sizeof(cv->_basePath) - 1);
    strncpy(cv->_animPath, animPath,    sizeof(cv->_animPath) - 1);
    return true;
}

void mspa_gif_converter_step(MspaGifConverter *cv) {
    if (!cv || cv->done || cv->failed) return;
    gif_animation *gif = (gif_animation *)cv->_gif;

    uint32_t frame = cv->frameDone;

    gif_result code = gif_decode_frame(gif, frame);
    if (code != GIF_OK && code != GIF_FRAME_NO_DISPLAY) {
        log_msg("GIF", "gif_decode_frame failed in step");
        cv->failed = true;
        return;
    }
    if (!gif->frame_image || gif->width == 0 || gif->height == 0) {
        cv->failed = true;
        return;
    }

    /* scale */
    uint8_t *scaled = NULL;
    int fw = 0, fh = 0;
    if (!resize_rgba_nearest((const uint8_t *)gif->frame_image,
                             (int)gif->width, (int)gif->height,
                             &scaled, &fw, &fh)) {
        cv->failed = true;
        return;
    }

    /* save this frame's tex — upload_rgba linearAlloc is freed inside */
    char framePath[256];
    frame_tex_path(cv->_basePath, frame, framePath, sizeof(framePath));
    MspaImage temp = {0};
    bool ok = mspa_panel_rgba_to_tex_file(scaled, fw, fh, framePath, &temp);
    mspa_image_free(&temp);
    free(scaled);

    if (!ok) {
        log_msg("GIF", "failed to save frame tex in step");
        cv->failed = true;
        return;
    }

    /* record delay */
    /* GIF frame_delay is in centiseconds. Minimum 5cs (50ms) to match browser behaviour. */
    uint32_t delay_cs = gif->frames ? gif->frames[frame].frame_delay : 0u;
    if (delay_cs < 5u) delay_cs = 5u;  /* browsers clamp to 10cs, we use 5cs */
    cv->_delays[frame] = delay_cs * 10u;

    cv->frameDone++;

    /* all frames done — write manifest */
    if (cv->frameDone >= cv->frameCount) {
        FILE *f = fopen(cv->_animPath, "wb");
        if (!f) { cv->failed = true; return; }
        uint32_t magic = ANIM_MAGIC, version = 2;
        MspaAnimManifest meta = { .frameCount = cv->frameCount };
        bool mok = fwrite(&magic,   4, 1, f) == 1 &&
                   fwrite(&version, 4, 1, f) == 1 &&
                   fwrite(&meta, sizeof(meta), 1, f) == 1 &&
                   fwrite(cv->_delays, 4, cv->frameCount, f) == cv->frameCount;
        fclose(f);
        if (!mok) {
            remove(cv->_animPath);
            cv->failed = true;
            return;
        }
        cv->done = true;
    }
}

void mspa_gif_converter_close(MspaGifConverter *cv) {
    if (!cv) return;
    if (cv->_gif) {
        gif_finalise((gif_animation *)cv->_gif);
        free(cv->_gif);
    }
    free(cv->_delays);
    /* on failure, clean up any partial frame files */
    if (cv->failed) {
        for (uint32_t i = 0; i < cv->frameDone; i++) {
            char framePath[256];
            frame_tex_path(cv->_basePath, i, framePath, sizeof(framePath));
            remove(framePath);
        }
        if (cv->_animPath[0]) remove(cv->_animPath);
    }
    memset(cv, 0, sizeof(*cv));
}

bool mspa_panel_gif_to_tex_sequence(const uint8_t *gifData, size_t gifSize,
                                    const char *texBasePath, const char *animPath,
                                    void (*status)(const char *)) {
    MspaGifConverter cv = {0};
    if (!mspa_gif_converter_open(&cv, gifData, gifSize, texBasePath, animPath)) {
        if (status) status("FAIL: open");
        return false;
    }
    while (!cv.done && !cv.failed) {
        if (status) {
            char msg[48];
            snprintf(msg, sizeof(msg), "frame %u/%u", cv.frameDone + 1, cv.frameCount);
            status(msg);
        }
        mspa_gif_converter_step(&cv);
    }
    bool ok = cv.done && !cv.failed;
    mspa_gif_converter_close(&cv);
    if (!ok && status) status("FAIL: convert");
    return ok;
}

bool mspa_panel_load_tex_file(const char *texPath, MspaImage *out) {
    if (!texPath || !out) return false;
    memset(out, 0, sizeof(*out));

    FILE *f = fopen(texPath, "rb");
    if (!f) return false;

    uint32_t magic = 0;
    uint32_t width = 0, height = 0, texW = 0, texH = 0, format = 0, dataSize = 0;
    bool ok =
        fread(&magic, 4, 1, f) == 1 && magic == TEX_MAGIC &&
        fread(&width, 4, 1, f) == 1 &&
        fread(&height, 4, 1, f) == 1 &&
        fread(&texW, 4, 1, f) == 1 &&
        fread(&texH, 4, 1, f) == 1 &&
        fread(&format, 4, 1, f) == 1 &&
        fread(&dataSize, 4, 1, f) == 1;

    if (!ok || dataSize == 0 || texW == 0 || texH == 0 || format != (uint32_t)GPU_RGBA8) {
        fclose(f);
        return false;
    }

    if (!C3D_TexInit(&out->tex, (u16)texW, (u16)texH, GPU_RGBA8)) {
        fclose(f);
        return false;
    }

    size_t expect = (size_t)texW * (size_t)texH * 4u;
    if (expect != dataSize) {
        C3D_TexDelete(&out->tex);
        fclose(f);
        return false;
    }

    if (fread(out->tex.data, 1, expect, f) != expect) {
        C3D_TexDelete(&out->tex);
        fclose(f);
        memset(out, 0, sizeof(*out));
        return false;
    }
    fclose(f);

    GSPGPU_FlushDataCache(out->tex.data, (u32)expect);
    C3D_TexFlush(&out->tex);
    C3D_TexSetFilter(&out->tex, GPU_LINEAR, GPU_LINEAR);
    C3D_TexSetWrap(&out->tex, GPU_CLAMP_TO_EDGE, GPU_CLAMP_TO_EDGE);
    out->tex.border = 0x00000000u;

    out->subtex.left   = 0.0f;
    out->subtex.top    = 1.0f;
    out->subtex.right  = (float)width / (float)texW;
    out->subtex.bottom = 1.0f - ((float)height / (float)texH);
    out->subtex.width  = (u16)width;
    out->subtex.height = (u16)height;
    out->image.tex     = &out->tex;
    out->image.subtex  = &out->subtex;
    out->width  = (int)width;
    out->height = (int)height;
    out->valid  = true;
    return true;
}

bool mspa_panel_download_and_save(const char *url, const char *sdPath,
                                  void (*status)(const char *)) {
    if (status) status("fetching...");

    uint8_t *raw = NULL;
    size_t rawSz = 0;
    if (!http_fetch_raw(url, &raw, &rawSz)) {
        if (status) status("FAIL: network");
        return false;
    }

    if (status) status("saving...");
    ensure_dirs();

    FILE *f = fopen(sdPath, "wb");
    if (!f) {
        free(raw);
        if (status) status("FAIL: write open");
        return false;
    }

    uint32_t magic = RAW_MAGIC;
    uint32_t rawSize = (uint32_t)rawSz;
    bool ok =
        fwrite(&magic, 4, 1, f) == 1 &&
        fwrite(&rawSize, 4, 1, f) == 1 &&
        fwrite(raw, 1, rawSz, f) == rawSz;

    fclose(f);
    free(raw);

    if (!ok) {
        remove(sdPath);
        if (status) status("FAIL: write disk");
        return false;
    }

    if (status) status("saved OK");
    return true;
}

bool mspa_panel_load(const char *sdPath, MspaImage *out) {
    if (!sdPath || !out) return false;
    memset(out, 0, sizeof(*out));

    FILE *f = fopen(sdPath, "rb");
    if (!f) return false;

    uint32_t magic = 0, rawSize = 0;
    if (fread(&magic, 4, 1, f) != 1 || magic != RAW_MAGIC ||
        fread(&rawSize, 4, 1, f) != 1 || rawSize == 0) {
        fclose(f);
        return false;
    }

    uint8_t *raw = (uint8_t *)malloc(rawSize);
    if (!raw) { fclose(f); return false; }
    if (fread(raw, 1, rawSize, f) != rawSize) {
        fclose(f);
        free(raw);
        return false;
    }
    fclose(f);

    int sw = 0, sh = 0;
    uint8_t *pixels = NULL;
    bool ok = mspa_panel_decode_gif_bytes(raw, rawSize, &sw, &sh, &pixels);
    free(raw);
    if (!ok) return false;

    uint8_t *scaled = NULL;
    int dw = 0, dh = 0;
    if (!resize_rgba_nearest(pixels, sw, sh, &scaled, &dw, &dh)) {
        free(pixels);
        return false;
    }
    free(pixels);

    ok = upload_rgba(out, dw, dh, scaled);
    free(scaled);
    return ok;
}

void mspa_image_free(MspaImage *img) {
    if (!img) return;
    if (img->valid) C3D_TexDelete(&img->tex);
    memset(img, 0, sizeof(*img));
}

void mspa_image_make_placeholder(MspaImage *out) {
    if (!out) return;
    const int w = 64, h = 64;
    uint8_t *px = (uint8_t *)calloc((size_t)w * h, 4);
    if (!px) return;
    for (int i = 0; i < w * h; i++) {
        px[i*4+0] = 0x44; px[i*4+1] = 0x44; px[i*4+2] = 0x66; px[i*4+3] = 0xFF;
    }
    upload_rgba(out, w, h, px);
    free(px);
}
