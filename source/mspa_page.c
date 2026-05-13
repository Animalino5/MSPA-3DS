#include "mspa_page.h"

#include <3ds/services/httpc.h>
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>

#include "mspa_image.h"
#include "mspa_http.h"

static char *dupstr(const char *s) {
    if (!s) return NULL;
    size_t n = strlen(s);
    char *out = (char *)malloc(n + 1);
    if (!out) return NULL;
    memcpy(out, s, n + 1);
    return out;
}

#define strdup dupstr

#define MSPA_REMOTE_PAGE_BASE "https://raw.githubusercontent.com/Animalino5/MSPA-3DS/main/pages/"

/* Homestuck.com live story chunks (scrape.js extractors).
 * Discovery order:
 *   1) GET homestuck.com/%06d — find any path ending in %06dHS-*.js (Vite chunk name).
 *   2) If the shell has no such reference, reuse cached StandardPageView manifest
 *      (refetched only when index-*.js id in the shell changes).
 * Module .js is parsed like "JSON": next link, media src, text, command (see scrape.js). */
#define HOMESTUCK_ORIGIN           "https://homestuck.com/"
#define HOMESTUCK_MEDIA_PREFIX     "https://storage.homestuck.com/story/homestuck/media/images/"
#define HOMESTUCK_PAGE_MIN         1901
#define HOMESTUCK_PAGE_MAX         10030
#define HS_FLASH_FALLBACK_MEDIA    HOMESTUCK_MEDIA_PREFIX "panels/act-1/00001.gif"

static char *slurp_file(const char *path, size_t *sizeOut) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;

    if (fseek(f, 0, SEEK_END) != 0) {
        fclose(f);
        return NULL;
    }

    long len = ftell(f);
    if (len < 0) {
        fclose(f);
        return NULL;
    }
    rewind(f);

    char *buf = (char *)malloc((size_t)len + 1);
    if (!buf) {
        fclose(f);
        return NULL;
    }

    size_t read = fread(buf, 1, (size_t)len, f);
    fclose(f);
    buf[read] = '\0';

    if (sizeOut) *sizeOut = read;
    return buf;
}

static void ensure_log_dir(void) {
    mkdir("sdmc:/3ds", 0777);
    mkdir("sdmc:/3ds/MSPA-3DS", 0777);
}

/* Helper function to write errors so we can debug the real hardware! */
static void log_http_error(const char *url, u32 status, Result ret, const char *step) {
    ensure_log_dir();
    FILE *f = fopen("sdmc:/3ds/MSPA-3DS/debug.log", "a");
    if (f) {
        fprintf(f, "[PAGE ERROR] URL: %s\n", url);
        fprintf(f, "Step: %s\n", step);
        fprintf(f, "HTTP Status Code: %lu\n", status);
        fprintf(f, "3DS Result Code: %08lX\n\n", ret);
        fclose(f);
    }
}

/* ------------------------------------------------------------------ */
/* HTTP fetch - Using DevkitPro's robust pending-loop logic           */
/* ------------------------------------------------------------------ */
static bool http_fetch(const char *url, char **bufOut, size_t *sizeOut) {
    Result ret = 0;
    httpcContext context;
    char *newurl = NULL;
    u32 statuscode = 0;
    u32 contentsize = 0, readsize = 0, size = 0;
    char *buf = NULL, *lastbuf = NULL;
    const char *current_url = url;

    if (bufOut) *bufOut = NULL;
    if (sizeOut) *sizeOut = 0;

    do {
        ret = httpcOpenContext(&context, HTTPC_METHOD_GET, current_url, 1);
        if (R_FAILED(ret)) {
            log_http_error(current_url, 0, ret, "httpcOpenContext");
            if (newurl) free(newurl);
            return false;
        }

        mspa_http_apply_request_headers(&context, NULL);

        ret = httpcBeginRequest(&context);
        if (R_FAILED(ret)) {
            log_http_error(current_url, 0, ret, "httpcBeginRequest (No Internet/SSL failure?)");
            httpcCloseContext(&context);
            if (newurl) free(newurl);
            return false;
        }

        ret = httpcGetResponseStatusCode(&context, &statuscode);
        if (R_FAILED(ret)) {
            log_http_error(current_url, statuscode, ret, "httpcGetResponseStatusCode");
            httpcCloseContext(&context);
            if (newurl) free(newurl);
            return false;
        }

        if ((statuscode >= 301 && statuscode <= 303) || (statuscode >= 307 && statuscode <= 308)) {
            if (newurl == NULL) newurl = (char*)malloc(0x1000);
            if (newurl == NULL) {
                httpcCloseContext(&context);
                return false;
            }
            ret = httpcGetResponseHeader(&context, "Location", newurl, 0x1000);
            if (R_FAILED(ret)) {
                httpcCloseContext(&context);
                free(newurl);
                return false;
            }
            {
                char resolved[1024];
                if (!mspa_http_resolve_location(current_url, newurl, resolved, sizeof(resolved))) {
                    httpcCloseContext(&context);
                    free(newurl);
                    return false;
                }
                if (strlen(resolved) >= 0x1000) {
                    httpcCloseContext(&context);
                    free(newurl);
                    return false;
                }
                snprintf(newurl, 0x1000, "%s", resolved);
            }
            current_url = newurl;
            httpcCloseContext(&context); // Close before we try the redirected URL
        }
    } while ((statuscode >= 301 && statuscode <= 303) || (statuscode >= 307 && statuscode <= 308));

    if (statuscode != 200) {
        log_http_error(current_url, statuscode, 0, "Bad HTTP Status (Not 200)");
        httpcCloseContext(&context);
        if (newurl) free(newurl);
        return false;
    }

    httpcGetDownloadSizeState(&context, NULL, &contentsize);

    buf = (char*)malloc(0x1000);
    if (buf == NULL) {
        httpcCloseContext(&context);
        if (newurl) free(newurl);
        return false;
    }

    do {
        ret = httpcDownloadData(&context, (u8*)buf + size, 0x1000, &readsize);
        size += readsize;
        if (ret == (s32)HTTPC_RESULTCODE_DOWNLOADPENDING) {
            lastbuf = buf;
            buf = (char*)realloc(buf, size + 0x1000);
            if (buf == NULL) {
                httpcCloseContext(&context);
                free(lastbuf);
                if (newurl) free(newurl);
                return false;
            }
        }
    } while (ret == (s32)HTTPC_RESULTCODE_DOWNLOADPENDING);

    if (ret != 0) {
        log_http_error(current_url, statuscode, ret, "httpcDownloadData failed midway");
        httpcCloseContext(&context);
        if (newurl) free(newurl);
        free(buf);
        return false;
    }

    /* Resize buffer to exact size + 1 for null terminator */
    lastbuf = buf;
    buf = (char*)realloc(buf, size + 1);
    if (buf == NULL) {
        httpcCloseContext(&context);
        free(lastbuf);
        if (newurl) free(newurl);
        return false;
    }
    buf[size] = '\0';

    httpcCloseContext(&context);
    if (newurl) free(newurl);

    *bufOut = buf;
    if (sizeOut) *sizeOut = size;
    return true;
}

static char *json_unescape_string(const char *src, const char **endOut) {
    if (*src != '"') return NULL;
    src++;

    size_t cap = 64;
    size_t len = 0;
    char *out = (char *)malloc(cap);
    if (!out) return NULL;

    while (*src && *src != '"') {
        char ch = *src++;
        if (ch == '\\' && *src) {
            char esc = *src++;
            switch (esc) {
                case 'n': ch = '\n'; break;
                case 'r': ch = '\r'; break;
                case 't': ch = '\t'; break;
                case '\\': ch = '\\'; break;
                case '"': ch = '"'; break;
                case '/': ch = '/'; break;
                default: ch = esc; break;
            }
        }

        if (len + 1 >= cap) {
            cap *= 2;
            char *tmp = (char *)realloc(out, cap);
            if (!tmp) {
                free(out);
                return NULL;
            }
            out = tmp;
        }
        out[len++] = ch;
    }

    if (*src == '"') src++;
    out[len] = '\0';
    if (endOut) *endOut = src;
    return out;
}

static const char *find_key(const char *json, const char *key) {
    char pattern[128];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    return strstr(json, pattern);
}

static const char *skip_colon_and_ws(const char *p) {
    if (!p) return NULL;
    p = strchr(p, ':');
    if (!p) return NULL;
    p++;
    while (*p && isspace((unsigned char)*p)) p++;
    return p;
}

static char *parse_string_value(const char *json, const char *key) {
    const char *p = find_key(json, key);
    p = skip_colon_and_ws(p);
    if (!p || *p != '"') return NULL;
    return json_unescape_string(p, NULL);
}

static int parse_int_value(const char *json, const char *key, int fallback) {
    const char *p = find_key(json, key);
    p = skip_colon_and_ws(p);
    if (!p) return fallback;
    return (int)strtol(p, NULL, 10);
}

static bool parse_text_array(const char *json, const char *key, char ***itemsOut, size_t *countOut) {
    *itemsOut = NULL;
    *countOut = 0;

    const char *p = find_key(json, key);
    p = skip_colon_and_ws(p);
    if (!p || *p != '[') return false;
    p++;

    size_t count = 0;
    size_t cap = 4;
    char **items = (char **)calloc(cap, sizeof(char *));
    if (!items) return false;

    while (*p) {
        while (*p && isspace((unsigned char)*p)) p++;
        if (*p == ']') break;

        if (*p == '"') {
            char *str = json_unescape_string(p, &p);
            if (!str) {
                for (size_t i = 0; i < count; i++) free(items[i]);
                free(items);
                return false;
            }

            if (count >= cap) {
                cap *= 2;
                char **tmp = (char **)realloc(items, cap * sizeof(char *));
                if (!tmp) {
                    free(str);
                    for (size_t i = 0; i < count; i++) free(items[i]);
                    free(items);
                    return false;
                }
                items = tmp;
            }

            items[count++] = str;
            while (*p && *p != ',' && *p != ']') p++;
            if (*p == ',') p++;
        } else {
            p++;
        }
    }

    *itemsOut = items;
    *countOut = count;
    return true;
}

static void free_string_array(char **items, size_t count) {
    if (!items) return;
    for (size_t i = 0; i < count; i++) free(items[i]);
    free(items);
}

static bool parse_page_json(const char *json, MspaPage *out) {
    if (!json || !out) return false;

    MspaPage temp = {0};
    temp.page = parse_int_value(json, "page", 0);
    temp.next = parse_int_value(json, "next", 0);
    temp.type = parse_string_value(json, "type");
    temp.alt = parse_string_value(json, "alt");
    
    /* CRITICAL FIX: The key in Homestuck JSON is "command", not "nextCommand" */
    temp.command = parse_string_value(json, "command");

    char **media = NULL;
    size_t mediaCount = 0;
    if (parse_text_array(json, "media", &media, &mediaCount) && mediaCount > 0) {
        temp.media = media[0];
        for (size_t i = 1; i < mediaCount; i++) free(media[i]);
        free(media);
    } else if (media) {
        free_string_array(media, mediaCount);
    }

    if (!parse_text_array(json, "text", &temp.text, &temp.textCount)) {
        temp.text = NULL;
        temp.textCount = 0;
    }

    *out = temp;
    if (out->page == 0) out->page = temp.page;
    return true;
}

/* ------------------------------------------------------------------ */
/* Homestuck.com — port of /home/lumi/Desktop/scrape.js extractors    */
/* Discovery: comic shell → index-*.js → StandardPageView-*.js        */
/* (page chunk id appears in that bundle as "assets/NNNNNNHS-*.js")   */
/* ------------------------------------------------------------------ */

static void trim_cstr(char *s) {
    if (!s) return;
    char *end;
    while (*s && isspace((unsigned char)*s)) memmove(s, s + 1, strlen(s) + 1);
    if (!*s) return;
    end = s + strlen(s);
    while (end > s && isspace((unsigned char)end[-1])) end--;
    *end = '\0';
}

/* src points at first character inside a JS double-quoted string; writes decoded bytes to *outPtr (malloc'd). */
static bool homestuck_read_js_string(const char *src, char **outPtr, const char **endAfterQuote) {
    size_t cap = 128, len = 0;
    char *buf = (char *)malloc(cap);
    if (!buf) return false;

    while (*src && *src != '"') {
        char ch = *src++;
        if (ch == '\\' && *src) {
            char esc = *src++;
            switch (esc) {
                case 'n': ch = '\n'; break;
                case 'r': ch = '\r'; break;
                case 't': ch = '\t'; break;
                case '\\': ch = '\\'; break;
                case '"': ch = '"'; break;
                case '/': ch = '/'; break;
                default: ch = esc; break;
            }
        }
        if (len + 1 >= cap) {
            cap *= 2;
            char *nb = (char *)realloc(buf, cap);
            if (!nb) {
                free(buf);
                return false;
            }
            buf = nb;
        }
        buf[len++] = ch;
    }
    if (*src != '"') {
        free(buf);
        return false;
    }
    src++;
    buf[len] = '\0';
    *outPtr = buf;
    if (endAfterQuote) *endAfterQuote = src;
    return true;
}

static bool homestuck_append_text(char ***items, size_t *count, size_t *cap, char *line) {
    trim_cstr(line);
    if (!line[0]) {
        free(line);
        return true;
    }
    if (*count >= *cap) {
        size_t nc = *cap ? *cap * 2 : 4;
        char **nitems = (char **)realloc(*items, nc * sizeof(char *));
        if (!nitems) {
            free(line);
            return false;
        }
        *items = nitems;
        *cap = nc;
    }
    (*items)[(*count)++] = line;
    return true;
}

static bool homestuck_media_is_flash(const char *url) {
    if (!url) return false;
    size_t n = strlen(url);
    if (n >= 4 && strcmp(url + n - 4, ".swf") == 0) return true;
    return strstr(url, "scratch/") != NULL;
}

static bool homestuck_pick_first_asset_url(const char *body, const char *prefix, char *outUrl, size_t outLen) {
    const char *p = strstr(body, prefix);
    if (!p) return false;
    const char *end = strstr(p, ".js");
    if (!end) return false;
    end += 3;
    {
        int n = snprintf(outUrl, outLen, "%s%.*s", HOMESTUCK_ORIGIN, (int)(end - p), p);
        return n > 0 && (size_t)n < outLen;
    }
}

/* Cached Vite manifest: only refetched when the shell's index-*.js asset id changes. */
static char *g_hs_std_manifest = NULL;
static char g_hs_cached_index_asset[200];

static void homestuck_clear_resolve_cache(void) {
    free(g_hs_std_manifest);
    g_hs_std_manifest = NULL;
    g_hs_cached_index_asset[0] = '\0';
}

void mspa_page_homestuck_clear_asset_cache(void) {
    homestuck_clear_resolve_cache();
}

bool mspa_page_homestuck_in_range(int pageNum) {
    return pageNum >= HOMESTUCK_PAGE_MIN && pageNum <= HOMESTUCK_PAGE_MAX;
}

static bool homestuck_url_from_assets_path(const char *assetsPathStart, char *outUrl, size_t outLen) {
    const char *end = strstr(assetsPathStart, ".js\"");
    if (!end) {
        end = strstr(assetsPathStart, ".js'");
        if (!end) return false;
    }
    end += 3;
    {
        int n = snprintf(outUrl, outLen, "%s%.*s", HOMESTUCK_ORIGIN, (int)(end - assetsPathStart), assetsPathStart);
        return n > 0 && (size_t)n < outLen;
    }
}

/* Comic route HTML may reference the page chunk as …/assets/001901HS-bLwrJzIw.js */
static bool homestuck_try_hs_chunk_url_from_shell_html(const char *html, int pageNum, char *outUrl, size_t outLen) {
    char needle[16];
    snprintf(needle, sizeof(needle), "%06dHS-", pageNum);

    for (const char *p = strstr(html, needle); p != NULL; p = strstr(p + 1, needle)) {
        const char *hash = p + strlen(needle);
        const char *ext = hash;
        while (*ext && (isalnum((unsigned char)*ext) || *ext == '-' || *ext == '_')) ext++;
        if (strncmp(ext, ".js", 3) != 0) continue;
        const char *end = ext + 3;

        const char *start = NULL;
        if (p >= html + 8 && strncmp(p - 8, "\"assets/", 8) == 0) start = p - 7;
        else if (p >= html + 8 && strncmp(p - 8, "'assets/", 8) == 0) start = p - 7;
        else if (p >= html + 8 && strncmp(p - 8, "/assets/", 8) == 0) start = p - 7;
        else if (p >= html + 7 && strncmp(p - 7, "assets/", 7) == 0) start = p - 7;
        else continue;

        int n = snprintf(outUrl, outLen, "%s%.*s", HOMESTUCK_ORIGIN, (int)(end - start), start);
        if (n > 0 && (size_t)n < outLen) return true;
    }
    return false;
}

static bool homestuck_discover_module_url(int pageNum, char *outUrl, size_t outLen) {
    char comicUrl[128];
    snprintf(comicUrl, sizeof(comicUrl), "%s%06d", HOMESTUCK_ORIGIN, pageNum);

    char *html = NULL;
    size_t htmlSz = 0;
    if (!http_fetch(comicUrl, &html, &htmlSz)) return false;

    if (homestuck_try_hs_chunk_url_from_shell_html(html, pageNum, outUrl, outLen)) {
        free(html);
        return true;
    }

    const char *ip = strstr(html, "assets/index-");
    if (!ip) {
        free(html);
        return false;
    }
    const char *iend = strstr(ip, ".js");
    if (!iend) {
        free(html);
        return false;
    }
    iend += 3;
    if ((size_t)(iend - ip) >= sizeof(g_hs_cached_index_asset)) {
        free(html);
        return false;
    }
    char idxAsset[200];
    memcpy(idxAsset, ip, (size_t)(iend - ip));
    idxAsset[(size_t)(iend - ip)] = '\0';
    free(html);

    if (!g_hs_std_manifest || strcmp(idxAsset, g_hs_cached_index_asset) != 0) {
        char indexUrl[384];
        snprintf(indexUrl, sizeof(indexUrl), "%s%s", HOMESTUCK_ORIGIN, idxAsset);

        char *indexJs = NULL;
        size_t indexSz = 0;
        if (!http_fetch(indexUrl, &indexJs, &indexSz)) return false;

        char stdUrl[512];
        if (!homestuck_pick_first_asset_url(indexJs, "assets/StandardPageView-", stdUrl, sizeof(stdUrl))) {
            free(indexJs);
            return false;
        }
        free(indexJs);

        char *stdJs = NULL;
        size_t stdSz = 0;
        if (!http_fetch(stdUrl, &stdJs, &stdSz)) return false;

        free(g_hs_std_manifest);
        g_hs_std_manifest = stdJs;
        strncpy(g_hs_cached_index_asset, idxAsset, sizeof(g_hs_cached_index_asset) - 1);
        g_hs_cached_index_asset[sizeof(g_hs_cached_index_asset) - 1] = '\0';
    }

    char pageKey[24];
    snprintf(pageKey, sizeof(pageKey), "%06dHS-", pageNum);
    const char *hit = strstr(g_hs_std_manifest, pageKey);
    if (!hit || hit < g_hs_std_manifest + 8 || strncmp(hit - 8, "\"assets/", 8) != 0) return false;
    const char *start = hit - 8 + 1;
    return homestuck_url_from_assets_path(start, outUrl, outLen);
}

static char *homestuck_absolute_media_url(const char *rel) {
    if (!rel || !rel[0]) return NULL;
    if (strncmp(rel, "http://", 7) == 0 || strncmp(rel, "https://", 8) == 0) {
        return strdup(rel);
    }
    size_t plen = strlen(HOMESTUCK_MEDIA_PREFIX), rlen = strlen(rel);
    char *out = (char *)malloc(plen + rlen + 1);
    if (!out) return NULL;
    memcpy(out, HOMESTUCK_MEDIA_PREFIX, plen);
    memcpy(out + plen, rel, rlen + 1);
    return out;
}

static int homestuck_parse_next_page(const char *js) {
    const char *p = strstr(js, "next-page-link\":\"/");
    if (!p) return 0;
    p += strlen("next-page-link\":\"/");
    return (int)strtol(p, NULL, 10);
}

static char *homestuck_parse_first_quoted_after(const char *js, const char *key) {
    const char *p = strstr(js, key);
    if (!p) return strdup("");
    p += strlen(key);
    if (*p != '"') return strdup("");
    p++;
    char *decoded = NULL;
    const char *end = NULL;
    if (!homestuck_read_js_string(p, &decoded, &end)) return strdup("");
    trim_cstr(decoded);
    return decoded;
}

static bool homestuck_parse_story_module(const char *js, int pageNum, MspaPage *out) {
    MspaPage temp = {0};
    temp.page = pageNum;

    char **texts = NULL;
    size_t tcount = 0, tcap = 0;
    char **medias = NULL;
    size_t mcount = 0, mcap = 0;

    const char *needle = "\"p\",null,\"";
    for (const char *q = js; (q = strstr(q, needle)) != NULL; ) {
        q += strlen(needle);
        char *decoded = NULL;
        const char *endq = NULL;
        if (!homestuck_read_js_string(q, &decoded, &endq)) goto fail;
        if (!homestuck_append_text(&texts, &tcount, &tcap, decoded)) goto fail;
        q = endq;
    }

    const char *lit = "src:\"";
    for (const char *p = js; (p = strstr(p, lit)) != NULL; ) {
        p += strlen(lit);
        char *rel = NULL;
        const char *endq = NULL;
        if (!homestuck_read_js_string(p, &rel, &endq)) goto fail;
        char *absu = homestuck_absolute_media_url(rel);
        free(rel);
        if (!absu) goto fail;
        if (mcount >= mcap) {
            size_t nc = mcap ? mcap * 2 : 4;
            char **nm = (char **)realloc(medias, nc * sizeof(char *));
            if (!nm) {
                free(absu);
                goto fail;
            }
            medias = nm;
            mcap = nc;
        }
        medias[mcount++] = absu;
        p = endq;
    }

    bool flash = false;
    for (size_t i = 0; i < mcount; i++) {
        if (homestuck_media_is_flash(medias[i])) {
            flash = true;
            break;
        }
    }
    if (flash) {
        free_string_array(medias, mcount);
        mcount = 0;
        medias = NULL;
        char *fb = strdup(HS_FLASH_FALLBACK_MEDIA);
        if (!fb) goto fail;
        medias = (char **)malloc(sizeof(char *));
        if (!medias) {
            free(fb);
            goto fail;
        }
        medias[0] = fb;
        mcount = 1;
    }

    if (mcount > 0) {
        temp.media = medias[0];
        for (size_t i = 1; i < mcount; i++) free(medias[i]);
        free(medias);
    } else {
        free(medias);
    }
    medias = NULL;
    mcount = 0;

    temp.text = texts;
    temp.textCount = tcount;
    texts = NULL;
    tcount = 0;

    temp.alt = homestuck_parse_first_quoted_after(js, "alt:\"");
    temp.command = homestuck_parse_first_quoted_after(js, "link-text\":\"");
    if (!temp.alt || !temp.command) goto fail;
    temp.next = homestuck_parse_next_page(js);
    temp.type = NULL;

    *out = temp;
    return true;

fail:
    free_string_array(texts, tcount);
    free_string_array(medias, mcount);
    mspa_page_free(&temp);
    memset(out, 0, sizeof(*out));
    return false;
}


static char *mirror_html_decode(const char *src) {
    if (!src) return NULL;
    size_t cap = strlen(src) * 2 + 16;
    char *out = (char *)malloc(cap);
    if (!out) return NULL;

    size_t len = 0;
    for (size_t i = 0; src[i]; ) {
        char ch = src[i];
        if (ch == '<') {
            if (!strncasecmp(src + i, "<br", 3)) {
                ch = '\n';
                const char *gt = strchr(src + i, '>');
                if (!gt) break;
                i = (size_t)(gt - src) + 1;
            } else {
                const char *gt = strchr(src + i, '>');
                if (!gt) break;
                i = (size_t)(gt - src) + 1;
                continue;
            }
        } else if (ch == '&') {
            if (!strncmp(src + i, "&nbsp;", 6)) { ch = ' '; i += 6; }
            else if (!strncmp(src + i, "&amp;", 5)) { ch = '&'; i += 5; }
            else if (!strncmp(src + i, "&quot;", 6)) { ch = '"'; i += 6; }
            else if (!strncmp(src + i, "&#39;", 5)) { ch = '\''; i += 5; }
            else if (!strncmp(src + i, "&lt;", 4)) { ch = '<'; i += 4; }
            else if (!strncmp(src + i, "&gt;", 4)) { ch = '>'; i += 4; }
            else { i++; continue; }
        } else {
            i++;
        }

        if (len + 2 >= cap) {
            cap *= 2;
            char *tmp = (char *)realloc(out, cap);
            if (!tmp) {
                free(out);
                return NULL;
            }
            out = tmp;
        }
        out[len++] = ch;
    }

    out[len] = '\0';
    while (out[0] && isspace((unsigned char)out[0])) memmove(out, out + 1, strlen(out));
    len = strlen(out);
    while (len > 0 && isspace((unsigned char)out[len - 1])) out[--len] = '\0';
    return out;
}

static char *mirror_extract_between(const char *html, const char *startToken, const char *endToken) {
    const char *start = strstr(html, startToken);
    if (!start) return NULL;
    start += strlen(startToken);
    const char *end = strstr(start, endToken);
    if (!end) return NULL;
    size_t len = (size_t)(end - start);
    char *out = (char *)malloc(len + 1);
    if (!out) return NULL;
    memcpy(out, start, len);
    out[len] = '\0';
    return out;
}

static char *mirror_abs_url(const char *src) {
    if (!src || !src[0]) return NULL;
    if (!strncmp(src, "http://", 7) || !strncmp(src, "https://", 8)) {
        /* Rewrite any homestuck.com image URL through the HTTP mirror
           to avoid TLS 1.2 (3DS httpc only supports up to TLS 1.1) */
        if (strstr(src, "homestuck.com")) {
            const char *path = strstr(src, "://");
            if (path) { path += 3; path = strchr(path, '/'); }
            if (path) {
                char *out = (char *)malloc(64 + strlen(path));
                if (!out) return NULL;
                sprintf(out, "http://mspa.chadthundercock.com%s", path);
                return out;
            }
        }
        return strdup(src);
    }
    /* Relative URL — prepend mirror origin */
    size_t plen = strlen("http://mspa.chadthundercock.com");
    size_t slen = strlen(src);
    char *out = (char *)malloc(plen + slen + 2);
    if (!out) return NULL;
    memcpy(out, "http://mspa.chadthundercock.com", plen);
    if (src[0] != '/') { out[plen] = '/'; memcpy(out + plen + 1, src, slen + 1); }
    else memcpy(out + plen, src, slen + 1);
    return out;
}

static char *mirror_extract_title(const char *html) {
    char *raw = mirror_extract_between(html, "<h2 id=\"title\">", "</h2>");
    if (!raw) return strdup("");
    char *txt = mirror_html_decode(raw);
    free(raw);
    return txt ? txt : strdup("");
}

static char *mirror_extract_image_url(const char *html) {
    const char *media = strstr(html, "<div id=\"media\"");
    if (!media) return strdup("");
    const char *content = strstr(media, "<div id=\"content\"");
    if (!content) content = html + strlen(html);

    const char *src = strstr(media, "src=\"");
    if (!src || src > content) return strdup("");
    src += 5;
    const char *q = strchr(src, '"');
    if (!q || q > content) return strdup("");

    size_t len = (size_t)(q - src);
    char tmp[1024];
    if (len >= sizeof(tmp)) len = sizeof(tmp) - 1;
    memcpy(tmp, src, len);
    tmp[len] = '\0';
    char *abs = mirror_abs_url(tmp);
    return abs ? abs : strdup("");
}

static char *mirror_extract_command(const char *html, int *nextPageOut) {
    if (nextPageOut) *nextPageOut = 0;
    const char *cmd = strstr(html, "<div class=\"commands\">");
    if (!cmd) return strdup("");

    const char *a = strstr(cmd, "<a href=\"");
    if (!a) return strdup("");
    a += strlen("<a href=\"");
    const char *hrefEnd = strchr(a, '"');
    if (!hrefEnd) return strdup("");

    char href[256];
    size_t hrefLen = (size_t)(hrefEnd - a);
    if (hrefLen >= sizeof(href)) hrefLen = sizeof(href) - 1;
    memcpy(href, a, hrefLen);
    href[hrefLen] = '\0';

    const char *textStart = strchr(hrefEnd, '>');
    if (!textStart) return strdup("");
    textStart++;
    const char *textEnd = strstr(textStart, "</a>");
    if (!textEnd) return strdup("");

    size_t innerLen = (size_t)(textEnd - textStart);
    char *inner = (char *)malloc(innerLen + 1);
    if (!inner) return strdup("");
    memcpy(inner, textStart, innerLen);
    inner[innerLen] = '\0';
    char *txt = mirror_html_decode(inner);
    free(inner);
    if (!txt) txt = strdup("");

    const char *p = strrchr(href, '/');
    if (p && nextPageOut) {
        *nextPageOut = atoi(p + 1);
    }
    return txt;
}

static bool mirror_extract_texts(const char *html, char ***itemsOut, size_t *countOut) {
    *itemsOut = NULL;
    *countOut = 0;

    const char *content = strstr(html, "<div id=\"content\"");
    if (!content) return false;
    const char *commands = strstr(content, "<div class=\"commands\">");
    if (!commands) commands = strstr(content, "<div id=\"page-footer\">");
    if (!commands) commands = html + strlen(html);

    char **items = NULL;
    size_t count = 0, cap = 0;

    const char *p = content;
    while ((p = strstr(p, "<p")) && p < commands) {
        const char *gt = strchr(p, '>');
        if (!gt || gt > commands) break;
        const char *close = strstr(gt + 1, "</p>");
        if (!close || close > commands) break;

        size_t innerLen = (size_t)(close - (gt + 1));
        char *inner = (char *)malloc(innerLen + 1);
        if (!inner) break;
        memcpy(inner, gt + 1, innerLen);
        inner[innerLen] = '\0';

        char *txt = mirror_html_decode(inner);
        free(inner);
        if (txt && txt[0]) {
            if (count >= cap) {
                size_t nc = cap ? cap * 2 : 4;
                char **tmp = (char **)realloc(items, nc * sizeof(char *));
                if (!tmp) {
                    free(txt);
                    break;
                }
                items = tmp;
                cap = nc;
            }
            items[count++] = txt;
        } else {
            free(txt);
        }
        p = close + 4;
    }

    *itemsOut = items;
    *countOut = count;
    return true;
}

static bool mirror_parse_page_html(const char *html, int pageNum, MspaPage *out) {
    if (!html || !out) return false;
    MspaPage temp = {0};
    temp.page = pageNum;
    temp.type = mirror_extract_title(html);
    temp.media = mirror_extract_image_url(html);
    temp.command = mirror_extract_command(html, &temp.next);
    temp.alt = strdup("");

    if (!mirror_extract_texts(html, &temp.text, &temp.textCount)) {
        mspa_page_free(&temp);
        return false;
    }

    if (!temp.type) temp.type = strdup("");
    if (!temp.command) temp.command = strdup("");
    if (!temp.media) temp.media = strdup("");
    if (!temp.alt) temp.alt = strdup("");

    *out = temp;
    return true;
}

static bool homestuck_live_load(int pageNum, MspaPage *out) {
    char url[256];
    snprintf(url, sizeof(url), "http://mspa.chadthundercock.com/read/6/%06d", pageNum);

    char *html = NULL;
    size_t htmlSz = 0;
    if (!http_fetch(url, &html, &htmlSz)) return false;

    bool ok = mirror_parse_page_html(html, pageNum, out);
    free(html);
    return ok;
}

bool mspa_page_fetch_homestuck_network(int pageNum, MspaPage *out) {
    return homestuck_live_load(pageNum, out);
}

static bool try_load_candidate(const char *path, MspaPage *out) {
    size_t size = 0;
    char *json = slurp_file(path, &size);
    if (!json) return false;

    bool ok = parse_page_json(json, out);
    free(json);
    return ok;
}

bool mspa_page_load(int pageNum, MspaPage *out) {
    if (!out) return false;

    char rel[128];
    snprintf(rel, sizeof(rel), "sdmc:/3ds/MSPA-3DS/pages/%06d.json", pageNum);
    if (try_load_candidate(rel, out)) {
        if (out->page == 0) out->page = pageNum;
        return true;
    }

    snprintf(rel, sizeof(rel), "romfs:/pages/%06d.json", pageNum);
    if (try_load_candidate(rel, out)) {
        if (out->page == 0) out->page = pageNum;
        return true;
    }

    if (mspa_page_homestuck_in_range(pageNum)) {
        if (homestuck_live_load(pageNum, out)) {
            if (out->page == 0) out->page = pageNum;
            return true;
        }
    }

    return false;
}

void mspa_page_free(MspaPage *page) {
    if (!page) return;
    free(page->type);
    free(page->alt);
    free(page->command);
    free(page->media);
    if (page->text) {
        for (size_t i = 0; i < page->textCount; i++) free(page->text[i]);
        free(page->text);
    }
    memset(page, 0, sizeof(*page));
}

static void mspa_page_ensure_sd_dirs(void) {
    mkdir("sdmc:/3ds", 0777);
    mkdir("sdmc:/3ds/MSPA-3DS", 0777);
    mkdir("sdmc:/3ds/MSPA-3DS/pages", 0777);
}

static void fputc_json_string(FILE *f, const char *s) {
    fputc('"', f);
    if (s) {
        for (; *s; s++) {
            if (*s == '"' || *s == '\\') {
                fputc('\\', f);
                fputc(*s, f);
            } else if (*s == '\n') {
                fputs("\\n", f);
            } else if (*s == '\r') {
                fputs("\\r", f);
            } else if (*s == '\t') {
                fputs("\\t", f);
            } else {
                fputc(*s, f);
            }
        }
    }
    fputc('"', f);
}

bool mspa_page_save_sd(int pageNum, const MspaPage *pg) {
    if (!pg) return false;
    mspa_page_ensure_sd_dirs();
    char path[128];
    snprintf(path, sizeof(path), "sdmc:/3ds/MSPA-3DS/pages/%06d.json", pageNum);
    FILE *f = fopen(path, "wb");
    if (!f) return false;

    fprintf(f, "{\n  \"page\": %d,\n  \"next\": %d", pg->page, pg->next);
    if (pg->type) {
        fputs(",\n  \"type\": ", f);
        fputc_json_string(f, pg->type);
    }
    fputs(",\n  \"alt\": ", f);
    fputc_json_string(f, pg->alt ? pg->alt : "");
    fputs(",\n  \"command\": ", f);
    fputc_json_string(f, pg->command ? pg->command : "");
    fputs(",\n  \"media\": [", f);
    if (pg->media && pg->media[0]) {
        fputc_json_string(f, pg->media);
    }
    fputs("],\n  \"text\": [\n", f);
    for (size_t i = 0; i < pg->textCount; i++) {
        if (i) fputs(",\n", f);
        fputs("    ", f);
        fputc_json_string(f, pg->text[i]);
    }
    fputs("\n  ]\n}\n", f);
    fclose(f);
    return true;
}

void mspa_page_delete_sd_pagefiles(int fromPage, int toPage) {
    if (fromPage > toPage) return;
    for (int p = fromPage; p <= toPage; p++) {
        char jpath[128];
        snprintf(jpath, sizeof(jpath), "sdmc:/3ds/MSPA-3DS/pages/%06d.json", p);
        char *jraw = NULL;
        size_t jz = 0;
        jraw = slurp_file(jpath, &jz);
        if (jraw) {
            MspaPage pg = {0};
            if (parse_page_json(jraw, &pg)) {
                if (pg.media && pg.media[0]) {
                    char bbase[160], bpath[192], animPath[192];
                    mspa_panel_path_from_url(pg.media, p, 1, 1, bbase, sizeof(bbase));
                    snprintf(bpath, sizeof(bpath), "%s.gif", bbase);
                    remove(bpath);
                    snprintf(animPath, sizeof(animPath), "%s.anim", bbase);
                    MspaAnimManifest meta = {0};
                    uint32_t *delays = NULL;
                    if (mspa_panel_load_anim_manifest(animPath, &meta, &delays)) {
                        free(delays);
                    }
                    /* Remove both the new spritesheet cache and any older per-frame cache files. */
                    snprintf(bpath, sizeof(bpath), "%s.sheet.tex", bbase);
                    remove(bpath);
                    snprintf(bpath, sizeof(bpath), "%s.tex", bbase);
                    remove(bpath);
                    snprintf(bpath, sizeof(bpath), "%s-000.tex", bbase);
                    remove(bpath);
                    for (uint32_t i = 0; i < meta.frameCount; i++) {
                        snprintf(bpath, sizeof(bpath), "%s-%03u.tex", bbase, (unsigned)i);
                        remove(bpath);
                    }
                    remove(animPath);
                }
                mspa_page_free(&pg);
            }
            free(jraw);
        }
        remove(jpath);
    }
}