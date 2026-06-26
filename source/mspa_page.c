#include "mspa_page.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ═══════════════════════════════════════════════════════════════════════
 * JSON PARSER (kept from original — used to parse cached page JSONs
 * extracted from ZIP bundles by mspa_bundle.c)
 * ═══════════════════════════════════════════════════════════════════════ */

static char *dupstr(const char *s) {
    if (!s) return NULL;
    size_t n = strlen(s);
    char *out = (char *)malloc(n + 1);
    if (!out) return NULL;
    memcpy(out, s, n + 1);
    return out;
}

#define strdup dupstr

static char *json_unescape_string(const char *src, const char **endOut) {
    if (*src != '"') return NULL;
    src++;

    size_t cap = 64, len = 0;
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
            if (!tmp) { free(out); return NULL; }
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

static void free_string_array(char **items, size_t count) {
    if (!items) return;
    for (size_t i = 0; i < count; i++) free(items[i]);
    free(items);
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

    while (*p && *p != ']') {
        while (*p && isspace((unsigned char)*p)) p++;
        if (*p == '"') {
            const char *end = NULL;
            char *val = json_unescape_string(p, &end);
            if (val) {
                if (count >= cap) {
                    cap *= 2;
                    char **tmp = (char **)realloc(items, cap * sizeof(char *));
                    if (!tmp) { free_string_array(items, count); free(val); return false; }
                    items = tmp;
                }
                items[count++] = val;
                p = end ? end : p + 1;
            } else {
                p++;
            }
        } else {
            p++;
        }
        while (*p && *p != '"' && *p != ']') p++;
    }

    *itemsOut = items;
    *countOut = count;
    return true;
}

bool parse_page_json(const char *json, MspaPage *out) {
    if (!json || !out) return false;
    MspaPage temp = {0};

    temp.page = parse_int_value(json, "page", 0);
    temp.next = parse_int_value(json, "next", 0);
    temp.type = parse_string_value(json, "type");
    temp.alt = parse_string_value(json, "alt");
    temp.command = parse_string_value(json, "command");
    temp.audio = parse_string_value(json, "audio");

    /* media array */
    {
        const char *p = find_key(json, "media");
        p = skip_colon_and_ws(p);
        if (p && *p == '[') {
            size_t mcount = 0, mcap = 4;
            char **mitems = (char **)calloc(mcap, sizeof(char *));
            p++;
            while (*p && *p != ']') {
                while (*p && isspace((unsigned char)*p)) p++;
                if (*p == '"') {
                    const char *end = NULL;
                    char *val = json_unescape_string(p, &end);
                    if (val) {
                        if (mcount >= mcap) {
                            mcap *= 2;
                            char **tmp = (char **)realloc(mitems, mcap * sizeof(char *));
                            if (!tmp) { free_string_array(mitems, mcount); free(val); mitems = NULL; mcount = 0; break; }
                            mitems = tmp;
                        }
                        mitems[mcount++] = val;
                        p = end ? end : p + 1;
                    } else {
                        p++;
                    }
                } else {
                    p++;
                }
                while (*p && *p != '"' && *p != ']') p++;
            }
            temp.media = mitems;
            temp.mediaCount = mcount;
        }
    }

    if (!parse_text_array(json, "text", &temp.text, &temp.textCount)) {
        temp.text = NULL;
        temp.textCount = 0;
    }

    if (!temp.type)    temp.type    = strdup("");
    if (!temp.alt)     temp.alt     = strdup("");
    if (!temp.command) temp.command = strdup("");
    if (!temp.audio)   temp.audio   = strdup("");

    *out = temp;
    if (out->page == 0) out->page = temp.page;
    return true;
}

void mspa_page_free(MspaPage *page) {
    if (!page) return;
    free(page->type);
    free(page->alt);
    free(page->command);
    free(page->audio);
    if (page->media) {
        for (size_t i = 0; i < page->mediaCount; i++) free(page->media[i]);
        free(page->media);
    }
    if (page->text) {
        for (size_t i = 0; i < page->textCount; i++) free(page->text[i]);
        free(page->text);
    }
    memset(page, 0, sizeof(*page));
}
