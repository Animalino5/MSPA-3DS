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

void mspa_panel_path_from_url(const char *mediaUrl, int absPage, int imageIndex, int frame,
                              char *out, size_t outLen);

/* Decode GIF bytes into a heap RGBA buffer (first frame only). */
bool mspa_panel_decode_gif_bytes(const uint8_t *gifData, size_t gifSize,
                                  int *w, int *h, uint8_t **rgbaOut);

/* Upload RGBA data into a GPU texture and save it as a cached .tex file. */
bool mspa_panel_rgba_to_tex_file(const uint8_t *rgba, int w, int h,
                                 const char *texPath, MspaImage *temp);

/* Upload raw RGBA into an in-memory texture (no file save). */
bool mspa_image_from_rgba(MspaImage *out, int w, int h, const uint8_t *rgba);

/* Streamed GIF-to-tex converter: converts one frame per call so the
   main loop stays responsive and peak RAM stays low. */
typedef struct {
    uint32_t frameCount;
    uint32_t frameDone;
    bool     done;
    bool     failed;
    void    *_gif;
    uint8_t *_gifData;
    size_t   _gifSize;
    uint32_t *_delays;
    char     _basePath[192];
    char     _animPath[224];
} MspaGifConverter;

bool mspa_gif_converter_open(MspaGifConverter *cv,
                             const uint8_t *gifData, size_t gifSize,
                             const char *texBasePath, const char *animPath);
void mspa_gif_converter_step(MspaGifConverter *cv);
void mspa_gif_converter_close(MspaGifConverter *cv);

typedef struct {
    uint32_t frameCount;
} MspaAnimManifest;

bool mspa_panel_save_anim_manifest(const char *animPath, const MspaAnimManifest *meta, const uint32_t *delaysMs);
bool mspa_panel_load_anim_manifest(const char *animPath, MspaAnimManifest *metaOut, uint32_t **delaysMsOut);
void mspa_panel_frame_tex_path(const char *texBasePath, uint32_t frameIndex,
                               char *out, size_t outLen);

bool mspa_panel_load_tex_file(const char *texPath, MspaImage *out);
void mspa_image_free(MspaImage *img);
void mspa_image_make_placeholder(MspaImage *out);

bool mspa_panel_gif_to_tex_file(const char *gifPath, const char *texPath,
                                void (*status)(const char *msg));
bool mspa_panel_load(const char *sdPath, MspaImage *out);
