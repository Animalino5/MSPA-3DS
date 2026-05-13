#pragma once
#include <3ds.h>
#include <3ds/services/httpc.h>
#include <stdbool.h>
#include <stddef.h>

/* Match browser-ish defaults so CDNs (Cloudflare, GitHub) behave like scrape.js + fetch(). */
void mspa_http_apply_request_headers(httpcContext *ctx, const char *optional_referer);

/* Turn Location into an absolute URL (handles /path, //host/path, full URLs). */
bool mspa_http_resolve_location(const char *request_url, const char *location,
                                char *out, size_t outlen);
