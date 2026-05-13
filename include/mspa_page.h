#pragma once
#include <3ds.h>
#include <3ds/services/httpc.h>
#include <stdbool.h>
#include <stddef.h>

typedef struct {
    int page;
    int next;
    char *type;
    char *alt;
    char *command;  /* JSON key is "command" */
    char *media;
    char **text;
    size_t textCount;
} MspaPage;

bool mspa_page_load(int pageNum, MspaPage *out);
void mspa_page_free(MspaPage *page);

/* Homestuck: fetch story module from homestuck.com only (no SD / romfs). */
bool mspa_page_fetch_homestuck_network(int pageNum, MspaPage *out);
/* Save page JSON to sdmc:/3ds/MSPA-3DS/pages/%06d.json (for offline cache). */
bool mspa_page_save_sd(int pageNum, const MspaPage *pg);
/* Remove cached JSON + panel .bin for each page in [fromPage, toPage] inclusive. */
void mspa_page_delete_sd_pagefiles(int fromPage, int toPage);
void mspa_page_homestuck_clear_asset_cache(void);
bool mspa_page_homestuck_in_range(int pageNum);
