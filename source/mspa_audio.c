#include "mspa_audio.h"

#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <malloc.h>

static bool g_ndsp_active = false;

static bool read_entire_file(const char *path, uint8_t **bufOut, size_t *sizeOut) {
    if (bufOut) *bufOut = NULL;
    if (sizeOut) *sizeOut = 0;
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return false; }
    long len = ftell(f);
    if (len <= 0) { fclose(f); return false; }
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

typedef struct {
    uint16_t audioFormat;
    uint16_t channels;
    uint32_t sampleRate;
    uint16_t bitsPerSample;
    const uint8_t *data;
    size_t dataSize;
} WavInfo;

static uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t rd32(const uint8_t *p) { return (uint32_t)(p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24)); }

static bool parse_wav(const uint8_t *buf, size_t size, WavInfo *out) {
    if (!buf || size < 44 || !out) return false;
    if (memcmp(buf, "RIFF", 4) != 0 || memcmp(buf + 8, "WAVE", 4) != 0) return false;
    memset(out, 0, sizeof(*out));
    size_t off = 12;
    while (off + 8 <= size) {
        const uint8_t *chunk = buf + off;
        uint32_t csz = rd32(chunk + 4);
        size_t cend = off + 8u + (size_t)csz + (csz & 1u);
        if (cend > size) return false;
        if (memcmp(chunk, "fmt ", 4) == 0 && csz >= 16) {
            out->audioFormat = rd16(chunk + 8);
            out->channels = rd16(chunk + 10);
            out->sampleRate = rd32(chunk + 12);
            out->bitsPerSample = rd16(chunk + 22);
        } else if (memcmp(chunk, "data", 4) == 0) {
            out->data = chunk + 8;
            out->dataSize = csz;
        }
        off = cend;
    }
    return out->data && out->dataSize > 0 && out->sampleRate > 0 && out->channels > 0;
}

static bool wav_to_pcm16_stereo(const WavInfo *wav, s16 **pcmOut, size_t *bytesOut) {
    if (!wav || !pcmOut || !bytesOut) return false;
    *pcmOut = NULL;
    *bytesOut = 0;

    size_t frames = 0;
    s16 *pcm = NULL;

    if (wav->audioFormat != 1) return false; /* PCM only */

    if (wav->bitsPerSample == 16) {
        size_t samples = wav->dataSize / 2u;
        frames = samples / wav->channels;
        size_t outSamples = frames * 2u;
        pcm = (s16 *)linearAlloc(outSamples * sizeof(s16));
        if (!pcm) return false;
        const int16_t *src = (const int16_t *)wav->data;
        if (wav->channels == 2) {
            memcpy(pcm, src, outSamples * sizeof(s16));
        } else if (wav->channels == 1) {
            for (size_t i = 0; i < frames; i++) {
                s16 v = src[i];
                pcm[i*2+0] = v;
                pcm[i*2+1] = v;
            }
        } else {
            linearFree(pcm);
            return false;
        }
        *pcmOut = pcm;
        *bytesOut = outSamples * sizeof(s16);
        return true;
    }

    if (wav->bitsPerSample == 8) {
        frames = wav->dataSize / wav->channels;
        size_t outSamples = frames * 2u;
        pcm = (s16 *)linearAlloc(outSamples * sizeof(s16));
        if (!pcm) return false;
        const uint8_t *src = wav->data;
        for (size_t i = 0; i < frames; i++) {
            s16 left, right;
            if (wav->channels == 1) {
                s16 v = (s16)(((int)src[i] - 128) << 8);
                left = right = v;
            } else if (wav->channels >= 2) {
                left  = (s16)(((int)src[i*wav->channels + 0] - 128) << 8);
                right = (s16)(((int)src[i*wav->channels + 1] - 128) << 8);
            } else {
                linearFree(pcm);
                return false;
            }
            pcm[i*2+0] = left;
            pcm[i*2+1] = right;
        }
        *pcmOut = pcm;
        *bytesOut = outSamples * sizeof(s16);
        return true;
    }

    return false;
}

bool mspa_audio_open(MspaAudio *audio, const char *wavPath) {
    if (!audio || !wavPath || !wavPath[0]) return false;
    memset(audio, 0, sizeof(*audio));
    snprintf(audio->path, sizeof(audio->path), "%s", wavPath);
    audio->audioChannel = 0;

    uint8_t *raw = NULL;
    size_t rawSz = 0;
    if (!read_entire_file(wavPath, &raw, &rawSz)) return false;

    WavInfo wav = {0};
    bool ok = parse_wav(raw, rawSz, &wav);
    s16 *pcm = NULL;
    size_t pcmBytes = 0;
    if (ok) ok = wav_to_pcm16_stereo(&wav, &pcm, &pcmBytes);
    free(raw);
    if (!ok || !pcm) return false;

    if (!g_ndsp_active) {
        if (R_SUCCEEDED(ndspInit())) {
            ndspSetOutputMode(NDSP_OUTPUT_STEREO);
            ndspSetOutputCount(2);
            g_ndsp_active = true;
        }
    }
    if (!g_ndsp_active) {
        linearFree(pcm);
        return false;
    }

    ndspChnReset(audio->audioChannel);
    ndspChnSetInterp(audio->audioChannel, NDSP_INTERP_LINEAR);
    ndspChnSetRate(audio->audioChannel, (float)wav.sampleRate);
    ndspChnSetFormat(audio->audioChannel, NDSP_FORMAT_STEREO_PCM16);

    memset(&audio->wave, 0, sizeof(audio->wave));
    audio->wave.data_pcm16 = pcm;
    audio->wave.nsamples = (u32)(pcmBytes / sizeof(s16));
    audio->wave.looping = true;
    audio->wave.status = NDSP_WBUF_FREE;
    audio->pcm = pcm;
    audio->pcmBytes = pcmBytes;
    audio->sampleRate = wav.sampleRate;
    audio->audioReady = true;
    audio->active = true;
    audio->failed = false;
    DSP_FlushDataCache(pcm, (u32)pcmBytes);
    ndspChnWaveBufAdd(audio->audioChannel, &audio->wave);
    audio->lastTickMs = osGetTime();
    return true;
}

void mspa_audio_tick(MspaAudio *audio) {
    (void)audio;
}

void mspa_audio_close(MspaAudio *audio) {
    if (!audio) return;
    if (audio->audioReady) {
        ndspChnWaveBufClear(audio->audioChannel);
        audio->audioReady = false;
    }
    if (audio->pcm) {
        linearFree(audio->pcm);
        audio->pcm = NULL;
    }
    memset(&audio->wave, 0, sizeof(audio->wave));
    audio->pcmBytes = 0;
    audio->active = false;
    audio->failed = false;
}
