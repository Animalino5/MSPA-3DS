#pragma once
#include <3ds.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "mspa_image.h"

typedef struct {
    bool active;
    bool failed;
    bool audioReady;
    char path[256];
    void *plm;
    MspaImage image;
    u64 lastTickMs;
    int srcW;
    int srcH;
    double fps;
    int sampleRate;
    int audioChannel;
} MspaVideo;

bool mspa_video_open(MspaVideo *video, const char *path);
void mspa_video_tick(MspaVideo *video);
void mspa_video_close(MspaVideo *video);
