#pragma once
#include <3ds.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Simple WAV player for Flash-era pages. */
typedef struct {
    bool active;
    bool failed;
    bool audioReady;
    char path[256];
    u64 lastTickMs;
    int audioChannel;
    s16 *pcm;
    size_t pcmBytes;
    u32 sampleRate;
    ndspWaveBuf wave;
} MspaAudio;

bool mspa_audio_open(MspaAudio *audio, const char *wavPath);
void mspa_audio_tick(MspaAudio *audio);
void mspa_audio_close(MspaAudio *audio);
