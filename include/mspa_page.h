#pragma once
#include <3ds.h>
#include <stdbool.h>
#include <stddef.h>

typedef struct {
    int page;
    int next;
    char *type;
    char *alt;
    char *command;
    char *audio;
    char **media;
    size_t mediaCount;
    char **text;
    size_t textCount;
} MspaPage;

/* Parse a page JSON string into an MspaPage struct. */
bool parse_page_json(const char *json, MspaPage *out);
void mspa_page_free(MspaPage *page);
