/* ftlog.h: frame-time recorder writing "frametime-log v1" (see SPEC.md).
 *
 * A single header for C99 and C++. In exactly ONE .c or .cpp file:
 *
 *     #define FTLOG_IMPLEMENTATION
 *     #include "ftlog.h"
 *
 * Everywhere else just #include "ftlog.h".
 *
 * It is off unless the FTLOG environment variable names an output file:
 *
 *     set FTLOG=C:\logs\run.ftlog
 *
 * Usage:
 *     ftlog_meta("settings.profile", "series-x");   // optional, any time
 *     ftlog_frame();                                 // once per present, right after it
 *     ftlog_mark("loaded");                          // optional named point
 *     ftlog_close();                                 // optional, at shutdown
 *
 * Data is flushed every second, so a crash loses at most about one second.
 * Not thread-safe: call all functions from one thread, normally the thread
 * that presents, or guard them with your own lock.
 *
 * Non-Windows builds with a strict -std=c99 need _POSIX_C_SOURCE >= 199309L
 * (for clock_gettime) defined before the first system header.
 * Numbers are written without printf's %f, so the C locale setting does not
 * change the decimal separator.
 */
#ifndef FTLOG_H
#define FTLOG_H

#ifdef __cplusplus
extern "C" {
#endif

int  ftlog_enabled(void);                               /* 1 when recording */
void ftlog_meta(const char *key, const char *value);    /* "# meta key=value" */
void ftlog_mark(const char *label);                     /* "# mark t_ms label" */
void ftlog_frame(void);                                 /* one presented frame */
void ftlog_flush(void);
void ftlog_close(void);

#ifdef __cplusplus
}
#endif

#endif /* FTLOG_H */

#ifdef FTLOG_IMPLEMENTATION
#ifndef FTLOG_IMPLEMENTED
#define FTLOG_IMPLEMENTED

#include <stdio.h>
#include <stdlib.h>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
static double ftlog__now_ms(void)
{
    static LARGE_INTEGER freq;
    LARGE_INTEGER c;
    if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&c);
    return (double)(c.QuadPart / freq.QuadPart) * 1000.0 +
           (double)(c.QuadPart % freq.QuadPart) * 1000.0 / (double)freq.QuadPart;
}
#else
#include <time.h>
static double ftlog__now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1e6;
}
#endif

#ifdef __cplusplus
extern "C" {
#endif

static FILE  *ftlog__f;
static int    ftlog__state;      /* 0 not tried yet, 1 on, -1 off */
static int    ftlog__have_last;
static double ftlog__start, ftlog__last, ftlog__last_flush;

static int ftlog__init(void)
{
    const char *path;
    if (ftlog__state) return ftlog__state > 0;
    ftlog__state = -1;
#ifdef _MSC_VER
#pragma warning(push)
#pragma warning(disable : 4996)
#endif
    path = getenv("FTLOG");
    if (!path || !*path) return 0;
    ftlog__f = fopen(path, "wb");
#ifdef _MSC_VER
#pragma warning(pop)
#endif
    if (!ftlog__f) return 0;
    setvbuf(ftlog__f, NULL, _IOFBF, 1 << 16);
    fputs("# frametime-log v1\nt_ms,frame_ms\n", ftlog__f);
    ftlog__start = ftlog__now_ms();
    ftlog__state = 1;
    return 1;
}

/* milliseconds with 3 decimals, independent of the C locale */
static void ftlog__ms(double ms)
{
    long long us = (long long)(ms * 1000.0 + 0.5);
    if (us < 0) us = 0;
    fprintf(ftlog__f, "%lld.%03lld", us / 1000, us % 1000);
}

/* mode 0: value, 1: key ('=' -> '_'), 2: label (whitespace -> '-') */
static void ftlog__put(const char *s, int mode)
{
    for (; s && *s; ++s) {
        char c = *s;
        if (c == '\r' || c == '\n') c = (mode == 2) ? '-' : ' ';
        else if (mode == 1 && c == '=') c = '_';
        else if (mode == 2 && (c == ' ' || c == '\t')) c = '-';
        fputc(c, ftlog__f);
    }
}

int ftlog_enabled(void) { return ftlog__init(); }

void ftlog_meta(const char *key, const char *value)
{
    if (!ftlog__init() || !key || !*key) return;
    fputs("# meta ", ftlog__f);
    ftlog__put(key, 1);
    fputc('=', ftlog__f);
    ftlog__put(value ? value : "", 0);
    fputc('\n', ftlog__f);
}

void ftlog_mark(const char *label)
{
    if (!ftlog__init()) return;
    fputs("# mark ", ftlog__f);
    ftlog__ms(ftlog__now_ms() - ftlog__start);
    fputc(' ', ftlog__f);
    ftlog__put((label && *label) ? label : "mark", 2);
    fputc('\n', ftlog__f);
    fflush(ftlog__f);
}

void ftlog_frame(void)
{
    double now, t;
    if (!ftlog__init()) return;
    now = ftlog__now_ms();
    t = now - ftlog__start;
    if (ftlog__have_last) {
        ftlog__ms(t);
        fputc(',', ftlog__f);
        ftlog__ms(now - ftlog__last);
        fputc('\n', ftlog__f);
    }
    ftlog__last = now;
    ftlog__have_last = 1;
    if (t - ftlog__last_flush >= 1000.0) {
        fflush(ftlog__f);
        ftlog__last_flush = t;
    }
}

void ftlog_flush(void)
{
    if (ftlog__state > 0) fflush(ftlog__f);
}

void ftlog_close(void)
{
    if (ftlog__state > 0) fclose(ftlog__f);
    ftlog__f = NULL;
    ftlog__state = -1;
}

#ifdef __cplusplus
}
#endif

#endif /* FTLOG_IMPLEMENTED */
#endif /* FTLOG_IMPLEMENTATION */
