#include "mspa_http.h"

#include <3ds/services/sslc.h>
#include <stdio.h>
#include <string.h>

/*
 * Short UA (httpc header fields are length-limited). Still reads as a normal
 * browser + app tag — closer to Playwright than "MSPA-3DS/1.0" alone.
 */
#define MSPA_HTTP_UA "Mozilla/5.0 (Mobile; rv:52.0) Gecko/20100101 Firefox/52.0 MSPA-3DS/1"

void mspa_http_apply_request_headers(httpcContext *ctx, const char *optional_referer) {
    httpcSetSSLOpt(ctx, SSLCOPT_DisableVerify);
    httpcSetKeepAlive(ctx, HTTPC_KEEPALIVE_ENABLED);
    httpcAddRequestHeaderField(ctx, "User-Agent", MSPA_HTTP_UA);
    httpcAddRequestHeaderField(ctx, "Accept", "*/*");
    httpcAddRequestHeaderField(ctx, "Accept-Language", "en-US,en;q=0.9");
    httpcAddRequestHeaderField(ctx, "Connection", "Keep-Alive");
    if (optional_referer && optional_referer[0])
        httpcAddRequestHeaderField(ctx, "Referer", optional_referer);
}

bool mspa_http_resolve_location(const char *request_url, const char *loc, char *out, size_t outlen) {
    if (!request_url || !loc || !loc[0] || outlen < 8) return false;

    if (strncmp(loc, "https://", 8) == 0 || strncmp(loc, "http://", 7) == 0) {
        snprintf(out, outlen, "%s", loc);
        return out[0] != '\0';
    }

    if (strncmp(loc, "//", 2) == 0) {
        const char *proto = "https:";
        if (strncmp(request_url, "http://", 7) == 0) proto = "http:";
        snprintf(out, outlen, "%s%s", proto, loc);
        return true;
    }

    if (loc[0] != '/') return false;

    const char *auth = strstr(request_url, "://");
    if (!auth) return false;
    auth += 3;
    const char *path_start = strchr(auth, '/');
    if (!path_start) {
        snprintf(out, outlen, "%s%s", request_url, loc);
        return true;
    }

    size_t origin_len = (size_t)(path_start - request_url);
    if (origin_len + strlen(loc) + 1 >= outlen) return false;
    snprintf(out, outlen, "%.*s%s", (int)origin_len, request_url, loc);
    return true;
}
