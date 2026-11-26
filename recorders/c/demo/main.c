/* Exercises ftlog.h: run with FTLOG=<path> and check the file with
 * `python -I tests/check_demo_log.py <path>`.
 * Builds as C (main.c + impl.c) or as C++ (the same files compiled as C++). */
#include <locale.h>
#include <stdio.h>
#include "../ftlog.h"

#ifdef _WIN32
#include <windows.h>
#define SLEEP_MS(n) Sleep(n)
#else
#include <time.h>
static void sleep_ms(int n)
{
    struct timespec ts;
    ts.tv_sec = 0;
    ts.tv_nsec = n * 1000000L;
    nanosleep(&ts, NULL);
}
#define SLEEP_MS(n) sleep_ms(n)
#endif

int main(void)
{
    int i;
    /* a locale with a decimal comma: the log must still use points */
    const char *loc = setlocale(LC_ALL, "German_Germany.1252");
    if (!loc) loc = setlocale(LC_ALL, "de_DE.UTF-8");
    printf("locale: %s, printf %%f gives %f\n", loc ? loc : "(unchanged)", 1.5);
    printf("enabled: %d\n", ftlog_enabled());
    ftlog_meta("settings.profile", "demo");
    ftlog_meta("settings.output", "1280x720");
    ftlog_meta("weird=key\nx", "multi\nline value");
    ftlog_meta("empty", NULL);
    for (i = 0; i < 120; ++i) {
        if (i == 20) ftlog_mark("loaded scene");
        SLEEP_MS(i % 50 == 49 ? 40 : 8);
        ftlog_frame();
    }
    ftlog_mark(NULL);
    ftlog_close();
    ftlog_frame(); /* after close: a no-op */
    return 0;
}
