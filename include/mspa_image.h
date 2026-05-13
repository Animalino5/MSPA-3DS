#pragma once
#include <3ds.h>
#include <citro2d.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define MSPA_PANEL_MAX_W 320
#define MSPA_PANEL_MAX_H 240

typedef struct {
    C2D_Image image;
    C3D_Tex   tex;
    Tex3DS_SubTexture subtex;
    int  width, height;
    bool valid;
} MspaImage;

/* Build a base SD card path from a Homestuck media URL + absolute page + frame.
   URL form: .../panels/ACT_SLUG/NNNNN.ext
   Result:   sdmc:/3ds/MSPA-3DS/panels/panel_ACT_SLUG_NNNNN-FFF
   Callers append .gif or .tex as needed.
   Falls back to panel_PPPPPP-FFF if URL doesn't match the pattern. */
void mspa_panel_path_from_url(const char *mediaUrl, int absPage, int imageIndex, int frame,
                              char *out, size_t outLen);

/* Download a GIF URL straight to SD, without decoding it in RAM. */
bool mspa_panel_download_gif(const char *url, const char *gifPath,
                             void (*status)(const char *msg));

/* Decode GIF bytes into a heap RGBA buffer (first frame only). */
bool mspa_panel_decode_gif_bytes(const uint8_t *gifData, size_t gifSize,
                                  int *w, int *h, uint8_t **rgbaOut);

/* Upload RGBA data into a GPU texture and save it as a cached .tex file. */
bool mspa_panel_rgba_to_tex_file(const uint8_t *rgba, int w, int h,
                                 const char *texPath, MspaImage *temp);

/* Streamed GIF-to-tex converter: converts one frame per call so the
   main loop stays responsive and peak RAM stays low.
   Usage:
     MspaGifConverter cv = {0};
     if (!mspa_gif_converter_open(&cv, gifBytes, gifSize, basePath, animPath)) fail;
     while (!cv.done && !cv.failed)
         mspa_gif_converter_step(&cv);
     mspa_gif_converter_close(&cv);   // always call, even on failure
*/
typedef struct {
    /* public read-only */
    uint32_t frameCount;
    uint32_t frameDone;   /* frames converted so far */
    bool     done;
    bool     failed;
    /* private */
    void    *_gif;        /* heap-allocated gif_animation */
    uint8_t *_gifData;    /* caller's gif bytes (NOT owned) */
    size_t   _gifSize;
    uint32_t *_delays;
    char     _basePath[192];
    char     _animPath[224];
} MspaGifConverter;

bool mspa_gif_converter_open(MspaGifConverter *cv,
                             const uint8_t *gifData, size_t gifSize,
                             const char *texBasePath, const char *animPath);
/* Convert one frame. Sets cv->done or cv->failed when finished. */
void mspa_gif_converter_step(MspaGifConverter *cv);
/* Free all internal resources. Safe to call on a zeroed struct. */
void mspa_gif_converter_close(MspaGifConverter *cv);

typedef struct {
    uint32_t frameCount;  /* total frames; each saved as basePath-NNN.tex */
} MspaAnimManifest;

/* Load/save the animation manifest for a GIF spritesheet. */
bool mspa_panel_save_anim_manifest(const char *animPath, const MspaAnimManifest *meta, const uint32_t *delaysMs);
bool mspa_panel_load_anim_manifest(const char *animPath, MspaAnimManifest *metaOut, uint32_t **delaysMsOut);
void mspa_panel_frame_tex_path(const char *texBasePath, uint32_t frameIndex,
                               char *out, size_t outLen);

/* Load a previously saved .tex file from SD into a GPU texture. */
bool mspa_panel_load_tex_file(const char *texPath, MspaImage *out);

/* Free a loaded MspaImage. Safe to call on a zeroed struct. */
void mspa_image_free(MspaImage *img);

/* Checkerboard placeholder for pages with no image yet. */
void mspa_image_make_placeholder(MspaImage *out);

/* Legacy helpers kept for compatibility. */
bool mspa_panel_download_and_save(const char *url, const char *sdPath,
                                  void (*status)(const char *msg));
bool mspa_panel_load(const char *sdPath, MspaImage *out);
bool mspa_panel_gif_to_tex_file(const char *gifPath, const char *texPath,
                                void (*status)(const char *msg));
