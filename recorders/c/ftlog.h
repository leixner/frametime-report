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
 *     ftlog_frame_busy(cpu_ms, gpu_ms);              // ...or with the frame's CPU/GPU busy ms
 *     ftlog_mark("loaded");                          // optional named point
 *     ftlog_close();                                 // optional, at shutdown
 *
 * ftlog_frame_busy() lets the report tell CPU-bound from GPU-bound runs.
 * Typical sources: the CPU time of the thread that builds and submits the
 * frame, and GPU timestamp queries. GPU times that arrive a few frames late
 * are fine. Pass a negative value for a time you don't have.
 *
 * On Windows, recording also writes the hardware names once (pc.cpu, pc.gpu,
 * pc.vramGB, pc.ramGB, pc.os, pc.driver, pc.cores; model names only), from a
 * background thread. With FTLOG_SAMPLE=1 (every 500 ms) or FTLOG_SAMPLE=<ms>
 * (100 to 10000) that thread also writes "# sample" lines: this process's CPU
 * % and memory, system CPU % and memory load, GPU % and dedicated VRAM. The
 * thread that calls ftlog_frame() does no extra work for it. Everything beyond
 * kernel32 is loaded at run time: nothing extra to link.
 *
 * Data is flushed every second, so a crash loses at most about one second.
 * Call the ftlog_ functions from one thread, normally the thread that presents,
 * or guard them with your own lock. Every line is written with one fwrite, so
 * the background thread's lines never mix with yours.
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
void ftlog_frame_busy(double cpu_ms, double gpu_ms);    /* ...with busy times */
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
#include <string.h>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <wchar.h>
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

#define FTLOG__LINE 1024

/* append to a line buffer; returns the new length, never past the buffer */
static int ftlog__cat(char *buf, int len, const char *s)
{
    while (s && *s && len < FTLOG__LINE - 2) buf[len++] = *s++;
    buf[len] = 0;
    return len;
}

/* A non-negative fixed-point number (value / 10^decimals) written by hand:
 * no printf, so neither the C locale nor a strict feature-test macro (which
 * hides snprintf on some systems) can affect it. Returns the length. */
static int ftlog__fixed(char *out, double value, int decimals)
{
    char tmp[32];
    int n = 0, len = 0, scale = 1, i;
    unsigned long long v, whole, frac;
    for (i = 0; i < decimals; i++) scale *= 10;
    if (!(value >= 0.0)) value = 0.0;               /* also NaN */
    if (value > 1e15) value = 1e15;
    v = (unsigned long long)(value * (double)scale + 0.5);
    whole = v / (unsigned long long)scale;
    frac = v % (unsigned long long)scale;
    do { tmp[n++] = (char)('0' + (int)(whole % 10)); whole /= 10; } while (whole);
    while (n) out[len++] = tmp[--n];
    if (decimals) {
        out[len++] = '.';
        for (i = decimals - 1; i >= 0; i--) { out[len + i] = (char)('0' + (int)(frac % 10)); frac /= 10; }
        len += decimals;
    }
    out[len] = 0;
    return len;
}

/* milliseconds with 3 decimals */
static int ftlog__cat_ms(char *buf, int len, double ms)
{
    char tmp[40];
    ftlog__fixed(tmp, ms, 3);
    return ftlog__cat(buf, len, tmp);
}

/* mode 0: value, 1: key ('=' -> '_'), 2: label (whitespace -> '-') */
static int ftlog__cat_clean(char *buf, int len, const char *s, int mode)
{
    for (; s && *s && len < FTLOG__LINE - 2; ++s) {
        char c = *s;
        if (c == '\r' || c == '\n') c = (mode == 2) ? '-' : ' ';
        else if (mode == 1 && c == '=') c = '_';
        else if (mode == 2 && (c == ' ' || c == '\t')) c = '-';
        buf[len++] = c;
    }
    buf[len] = 0;
    return len;
}

/* one line, one fwrite: the C library locks the stream per call */
static void ftlog__line(char *buf, int len)
{
    buf[len++] = '\n';
    fwrite(buf, 1, (size_t)len, ftlog__f);
}

static void ftlog__write_meta(const char *key, const char *value)
{
    char buf[FTLOG__LINE];
    int len = ftlog__cat(buf, 0, "# meta ");
    len = ftlog__cat_clean(buf, len, key, 1);
    len = ftlog__cat(buf, len, "=");
    len = ftlog__cat_clean(buf, len, value ? value : "", 0);
    ftlog__line(buf, len);
}

#ifdef _WIN32
static void ftlog__start_background(void);
static void ftlog__stop_background(void);
#else
static void ftlog__start_background(void) {}
static void ftlog__stop_background(void) {}
#endif

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
    ftlog__start_background();
    return 1;
}

int ftlog_enabled(void) { return ftlog__init(); }

void ftlog_meta(const char *key, const char *value)
{
    if (!ftlog__init() || !key || !*key) return;
    ftlog__write_meta(key, value);
}

void ftlog_mark(const char *label)
{
    char buf[FTLOG__LINE];
    int len;
    if (!ftlog__init()) return;
    len = ftlog__cat(buf, 0, "# mark ");
    len = ftlog__cat_ms(buf, len, ftlog__now_ms() - ftlog__start);
    len = ftlog__cat(buf, len, " ");
    len = ftlog__cat_clean(buf, len, (label && *label) ? label : "mark", 2);
    ftlog__line(buf, len);
    fflush(ftlog__f);
}

static void ftlog__record(int has_busy, double cpu_ms, double gpu_ms)
{
    double now, t;
    if (!ftlog__init()) return;
    now = ftlog__now_ms();
    t = now - ftlog__start;
    if (ftlog__have_last) {
        char buf[160];
        int len = ftlog__fixed(buf, t, 3);
        buf[len++] = ',';
        len += ftlog__fixed(buf + len, now - ftlog__last, 3);
        if (has_busy) {
            /* "v >= 0 && v < 1e12" also rejects NaN */
            buf[len++] = ',';
            if (cpu_ms >= 0.0 && cpu_ms < 1e12) len += ftlog__fixed(buf + len, cpu_ms, 3);
            buf[len++] = ',';
            if (gpu_ms >= 0.0 && gpu_ms < 1e12) len += ftlog__fixed(buf + len, gpu_ms, 3);
        }
        buf[len++] = '\n';
        fwrite(buf, 1, (size_t)len, ftlog__f);
    }
    ftlog__last = now;
    ftlog__have_last = 1;
    if (t - ftlog__last_flush >= 1000.0) {
        fflush(ftlog__f);
        ftlog__last_flush = t;
    }
}

void ftlog_frame(void) { ftlog__record(0, 0.0, 0.0); }

void ftlog_frame_busy(double cpu_ms, double gpu_ms) { ftlog__record(1, cpu_ms, gpu_ms); }

void ftlog_flush(void)
{
    if (ftlog__state > 0) fflush(ftlog__f);
}

void ftlog_close(void)
{
    if (ftlog__state > 0) {
        ftlog__stop_background();
        fclose(ftlog__f);
    }
    ftlog__f = NULL;
    ftlog__state = -1;
}

/* ------------------------------------------------------------------------
 * Windows: hardware names and usage sampling on a background thread.
 * Registry for names, kernel32 for CPU and memory, PDH (the counters Task
 * Manager uses) for GPU load and VRAM on any vendor.
 */
#ifdef _WIN32

typedef void *ftlog__handle;
typedef LONG (WINAPI *ftlog__RegGetValueW)(ftlog__handle, LPCWSTR, LPCWSTR, DWORD, LPDWORD, PVOID, LPDWORD);
typedef LONG (WINAPI *ftlog__PdhOpenQueryW)(LPCWSTR, ULONG_PTR, ftlog__handle *);
typedef LONG (WINAPI *ftlog__PdhAddEnglishCounterW)(ftlog__handle, LPCWSTR, ULONG_PTR, ftlog__handle *);
typedef LONG (WINAPI *ftlog__PdhCollectQueryData)(ftlog__handle);
typedef LONG (WINAPI *ftlog__PdhGetFormattedCounterArrayW)(ftlog__handle, DWORD, LPDWORD, LPDWORD, void *);
typedef DWORD (WINAPI *ftlog__GetActiveProcessorCount)(WORD);

typedef struct {
    DWORD cb, PageFaultCount;
    SIZE_T PeakWorkingSetSize, WorkingSetSize, QuotaPeakPagedPoolUsage, QuotaPagedPoolUsage,
           QuotaPeakNonPagedPoolUsage, QuotaNonPagedPoolUsage, PagefileUsage, PeakPagefileUsage, PrivateUsage;
} ftlog__memcounters;
typedef BOOL (WINAPI *ftlog__GetProcessMemoryInfo)(HANDLE, ftlog__memcounters *, DWORD);

typedef struct { DWORD status; double value; } ftlog__pdh_value;
typedef struct { LPWSTR name; ftlog__pdh_value value; } ftlog__pdh_item;

#define FTLOG__HKLM ((ftlog__handle)(ULONG_PTR)(LONG)0x80000002)
#define FTLOG__RRF_RT_REG_SZ 0x00000002
#define FTLOG__RRF_RT_ANY 0x0000ffff
#define FTLOG__PDH_FMT 0x00008200 /* PDH_FMT_DOUBLE | PDH_FMT_NOCAP100 */
#define FTLOG__PDH_MORE_DATA ((LONG)0x800007D2)

static HANDLE ftlog__thread, ftlog__stop;
static DWORD ftlog__interval;
static ftlog__RegGetValueW ftlog__reg;

static void (*ftlog__proc(const wchar_t *dll, const char *name))(void)
{
    HMODULE m = LoadLibraryW(dll);
    return m ? (void (*)(void))GetProcAddress(m, name) : NULL;
}

/* registry string as UTF-8, trimmed; 0 when missing */
static int ftlog__reg_str(const wchar_t *key, const wchar_t *value, char *out, int cap)
{
    wchar_t w[512];
    DWORD size = sizeof w;
    int n, a = 0, b;
    out[0] = 0;
    if (!ftlog__reg || ftlog__reg(FTLOG__HKLM, key, value, FTLOG__RRF_RT_REG_SZ, NULL, w, &size) != 0) return 0;
    w[511] = 0;
    n = WideCharToMultiByte(CP_UTF8, 0, w, -1, out, cap, NULL, NULL);
    if (n <= 1) { out[0] = 0; return 0; }
    b = n - 1;
    while (a < b && out[a] == ' ') a++;
    while (b > a && out[b - 1] == ' ') b--;
    memmove(out, out + a, (size_t)(b - a));
    out[b - a] = 0;
    return b > a;
}

static unsigned long long ftlog__reg_u64(const wchar_t *key, const wchar_t *value)
{
    unsigned char data[8] = {0};
    DWORD size = sizeof data;
    unsigned long long v = 0;
    int i;
    if (!ftlog__reg || ftlog__reg(FTLOG__HKLM, key, value, FTLOG__RRF_RT_ANY, NULL, data, &size) != 0) return 0;
    if (size != 8 && size != 4) return 0;
    for (i = (int)size - 1; i >= 0; i--) v = (v << 8) | data[i];
    return v;
}

static void ftlog__meta_u(const char *key, unsigned long long v)
{
    char tmp[32];
    snprintf(tmp, sizeof tmp, "%llu", v);
    ftlog__write_meta(key, tmp);
}

static unsigned long long ftlog__gb(unsigned long long bytes)
{
    return (bytes + (1ull << 29)) >> 30; /* rounded to whole GB */
}

static DWORD ftlog__cores(void)
{
    ftlog__GetActiveProcessorCount count = (ftlog__GetActiveProcessorCount)ftlog__proc(L"kernel32.dll", "GetActiveProcessorCount");
    SYSTEM_INFO si;
    if (count) { DWORD n = count(0xFFFF); if (n) return n; }
    GetSystemInfo(&si);
    return si.dwNumberOfProcessors ? si.dwNumberOfProcessors : 1;
}

static void ftlog__hardware(void)
{
    char s[512], drv[128], disp[64];
    wchar_t key[160];
    unsigned long long best_vram = 0, vram;
    char best_name[512] = "", best_driver[128] = "";
    int i, have = 0;
    MEMORYSTATUSEX ms;

    ftlog__meta_u("pc.cores", ftlog__cores());
    ftlog__reg = (ftlog__RegGetValueW)ftlog__proc(L"advapi32.dll", "RegGetValueW");
    if (ftlog__reg_str(L"HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0", L"ProcessorNameString", s, sizeof s))
        ftlog__write_meta("pc.cpu", s);
    /* display adapters: take the one with the most dedicated memory */
    for (i = 0; i < 16; i++) {
        static const wchar_t prefix[] = L"SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\00";
        size_t pl = sizeof prefix / sizeof prefix[0] - 1;
        memcpy(key, prefix, pl * sizeof(wchar_t));
        key[pl] = (wchar_t)(L'0' + i / 10);
        key[pl + 1] = (wchar_t)(L'0' + i % 10);
        key[pl + 2] = 0;
        if (!ftlog__reg_str(key, L"DriverDesc", s, sizeof s)) continue;
        vram = ftlog__reg_u64(key, L"HardwareInformation.qwMemorySize");
        if (!vram) vram = ftlog__reg_u64(key, L"HardwareInformation.MemorySize");
        if (!have || vram > best_vram) {
            have = 1;
            best_vram = vram;
            memcpy(best_name, s, sizeof best_name);
            if (!ftlog__reg_str(key, L"DriverVersion", best_driver, sizeof best_driver)) best_driver[0] = 0;
        }
    }
    if (have) {
        ftlog__write_meta("pc.gpu", best_name);
        if (best_vram) ftlog__meta_u("pc.vramGB", ftlog__gb(best_vram));
        if (best_driver[0]) ftlog__write_meta("pc.driver", best_driver);
    }
    /* ProductName still says "Windows 10" on Windows 11, so go by the build number */
    if (ftlog__reg_str(L"SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", L"CurrentBuild", drv, sizeof drv)) {
        long build = strtol(drv, NULL, 10);
        const char *name = build >= 22000 ? "Windows 11" : build >= 10240 ? "Windows 10" : "Windows";
        if (ftlog__reg_str(L"SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", L"DisplayVersion", disp, sizeof disp))
            snprintf(s, sizeof s, "%s (build %s, %s)", name, drv, disp);
        else
            snprintf(s, sizeof s, "%s (build %s)", name, drv);
        ftlog__write_meta("pc.os", s);
    }
    ms.dwLength = sizeof ms;
    if (GlobalMemoryStatusEx(&ms)) ftlog__meta_u("pc.ramGB", ftlog__gb(ms.ullTotalPhys));
}

static unsigned long long ftlog__ticks(FILETIME f)
{
    return ((unsigned long long)f.dwHighDateTime << 32) | f.dwLowDateTime;
}

/* key=value with one decimal, independent of the C locale */
static int ftlog__cat_kv(char *buf, int len, const char *key, double v)
{
    char tmp[64];
    long long t;
    if (!(v >= 0.0 && v < 1e15)) return len;
    t = (long long)(v * 10.0 + 0.5);
    snprintf(tmp, sizeof tmp, "%s%s=%lld.%lld", len > 0 && buf[len - 1] != ' ' ? " " : "", key, t / 10, t % 10);
    return ftlog__cat(buf, len, tmp);
}

typedef struct {
    ftlog__PdhCollectQueryData collect;
    ftlog__PdhGetFormattedCounterArrayW array;
    ftlog__handle query, engine, memory;
    void *buf;
    DWORD cap;
} ftlog__pdh;

static int ftlog__pdh_open(ftlog__pdh *p)
{
    ftlog__PdhOpenQueryW open = (ftlog__PdhOpenQueryW)ftlog__proc(L"pdh.dll", "PdhOpenQueryW");
    ftlog__PdhAddEnglishCounterW add = (ftlog__PdhAddEnglishCounterW)ftlog__proc(L"pdh.dll", "PdhAddEnglishCounterW");
    memset(p, 0, sizeof *p);
    p->collect = (ftlog__PdhCollectQueryData)ftlog__proc(L"pdh.dll", "PdhCollectQueryData");
    p->array = (ftlog__PdhGetFormattedCounterArrayW)ftlog__proc(L"pdh.dll", "PdhGetFormattedCounterArrayW");
    if (!open || !add || !p->collect || !p->array || open(NULL, 0, &p->query) != 0) return 0;
    if (add(p->query, L"\\GPU Engine(*)\\Utilization Percentage", 0, &p->engine) != 0) p->engine = NULL;
    if (add(p->query, L"\\GPU Adapter Memory(*)\\Dedicated Usage", 0, &p->memory) != 0) p->memory = NULL;
    if (!p->engine && !p->memory) return 0;
    p->collect(p->query); /* rate counters need a first reading */
    return 1;
}

/* returns the item count, or -1 */
static int ftlog__pdh_items(ftlog__pdh *p, ftlog__handle counter, ftlog__pdh_item **items)
{
    DWORD size = 0, count = 0;
    LONG r = p->array(counter, FTLOG__PDH_FMT, &size, &count, NULL);
    if (r == 0) return 0;
    if (r != FTLOG__PDH_MORE_DATA || size == 0) return -1;
    if (size > p->cap) {
        void *nb = realloc(p->buf, size);
        if (!nb) return -1;
        p->buf = nb;
        p->cap = size;
    }
    if (p->array(counter, FTLOG__PDH_FMT, &size, &count, p->buf) != 0) return -1;
    *items = (ftlog__pdh_item *)p->buf;
    return (int)count;
}

#define FTLOG__ENGINES 256

/* GPU % like Task Manager: per engine, the sum over all processes; the busiest engine */
static double ftlog__pdh_gpu(ftlog__pdh *p)
{
    static const wchar_t *keys[FTLOG__ENGINES];
    static double sums[FTLOG__ENGINES];
    ftlog__pdh_item *it = NULL;
    int n = ftlog__pdh_items(p, p->engine, &it), k = 0, i, j;
    double best = 0.0;
    if (n <= 0) return -1.0;
    for (i = 0; i < n; i++) {
        const wchar_t *name = it[i].name, *key;
        if (it[i].value.status > 1 || !name) continue;
        key = wcsstr(name, L"luid_");
        if (!key) key = name;
        for (j = 0; j < k && wcscmp(keys[j], key) != 0; j++) {}
        if (j == k) {
            if (k == FTLOG__ENGINES) continue;
            keys[k] = key;
            sums[k++] = 0.0;
        }
        sums[j] += it[i].value.value;
    }
    if (!k) return -1.0;
    for (j = 0; j < k; j++) if (sums[j] > best) best = sums[j];
    return best > 100.0 ? 100.0 : best;
}

static double ftlog__pdh_vram(ftlog__pdh *p)
{
    ftlog__pdh_item *it = NULL;
    int n = ftlog__pdh_items(p, p->memory, &it), i;
    double best = -1.0;
    for (i = 0; i < n; i++)
        if (it[i].value.status <= 1 && it[i].value.value > best) best = it[i].value.value;
    return best < 0 ? -1.0 : best / 1048576.0;
}

static DWORD WINAPI ftlog__background(LPVOID unused)
{
    ftlog__pdh pdh;
    int have_pdh;
    double cores, last;
    FILETIME c, e, k, u, si, sk, su;
    unsigned long long proc, sys_idle, sys_total;
    ftlog__GetProcessMemoryInfo meminfo;
    (void)unused;

    ftlog__hardware();
    if (!ftlog__interval) return 0;

    cores = (double)ftlog__cores();
    meminfo = (ftlog__GetProcessMemoryInfo)ftlog__proc(L"kernel32.dll", "K32GetProcessMemoryInfo");
    have_pdh = ftlog__pdh_open(&pdh);
    GetProcessTimes(GetCurrentProcess(), &c, &e, &k, &u);
    proc = ftlog__ticks(k) + ftlog__ticks(u);
    GetSystemTimes(&si, &sk, &su);
    sys_idle = ftlog__ticks(si);
    sys_total = ftlog__ticks(sk) + ftlog__ticks(su); /* kernel time includes idle */
    last = ftlog__now_ms();

    while (WaitForSingleObject(ftlog__stop, ftlog__interval) == WAIT_TIMEOUT) {
        char buf[FTLOG__LINE];
        int len;
        double now = ftlog__now_ms(), wall = (now - last) / 1000.0;
        unsigned long long p2, i2, t2;
        ftlog__memcounters mc;
        MEMORYSTATUSEX ms;
        last = now;

        len = ftlog__cat(buf, 0, "# sample ");
        len = ftlog__cat_ms(buf, len, now - ftlog__start);
        len = ftlog__cat(buf, len, " ");

        GetProcessTimes(GetCurrentProcess(), &c, &e, &k, &u);
        p2 = ftlog__ticks(k) + ftlog__ticks(u);
        if (wall > 0) {
            double cpu = (double)(p2 - proc) / 1e7 / wall / cores * 100.0;
            len = ftlog__cat_kv(buf, len, "cpu", cpu > 100.0 ? 100.0 : cpu);
        }
        proc = p2;
        memset(&mc, 0, sizeof mc);
        mc.cb = sizeof mc;
        if (meminfo && meminfo(GetCurrentProcess(), &mc, sizeof mc))
            len = ftlog__cat_kv(buf, len, "ram", (double)mc.PrivateUsage / 1048576.0);
        GetSystemTimes(&si, &sk, &su);
        i2 = ftlog__ticks(si);
        t2 = ftlog__ticks(sk) + ftlog__ticks(su);
        if (t2 > sys_total) {
            double busy = (1.0 - (double)(i2 - sys_idle) / (double)(t2 - sys_total)) * 100.0;
            len = ftlog__cat_kv(buf, len, "syscpu", busy < 0 ? 0 : busy > 100 ? 100 : busy);
        }
        sys_idle = i2;
        sys_total = t2;
        ms.dwLength = sizeof ms;
        if (GlobalMemoryStatusEx(&ms)) len = ftlog__cat_kv(buf, len, "sysram", (double)ms.dwMemoryLoad);
        if (have_pdh && pdh.collect(pdh.query) == 0) {
            if (pdh.engine) len = ftlog__cat_kv(buf, len, "gpu", ftlog__pdh_gpu(&pdh));
            if (pdh.memory) len = ftlog__cat_kv(buf, len, "vram", ftlog__pdh_vram(&pdh));
        }
        ftlog__line(buf, len);
    }
    free(pdh.buf);
    return 0;
}

static void ftlog__start_background(void)
{
    const char *v;
#ifdef _MSC_VER
#pragma warning(push)
#pragma warning(disable : 4996)
#endif
    v = getenv("FTLOG_SAMPLE");
#ifdef _MSC_VER
#pragma warning(pop)
#endif
    ftlog__interval = 0;
    if (v && *v && strcmp(v, "0") != 0 && strcmp(v, "off") != 0 && strcmp(v, "false") != 0) {
        long ms = (strcmp(v, "1") == 0 || strcmp(v, "on") == 0 || strcmp(v, "true") == 0) ? 500 : strtol(v, NULL, 10);
        if (ms > 0) ftlog__interval = (DWORD)(ms < 100 ? 100 : ms > 10000 ? 10000 : ms);
    }
    ftlog__stop = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (ftlog__stop) ftlog__thread = CreateThread(NULL, 0, ftlog__background, NULL, 0, NULL);
}

static void ftlog__stop_background(void)
{
    if (ftlog__thread) {
        SetEvent(ftlog__stop);
        WaitForSingleObject(ftlog__thread, INFINITE);
        CloseHandle(ftlog__thread);
        ftlog__thread = NULL;
    }
    if (ftlog__stop) {
        CloseHandle(ftlog__stop);
        ftlog__stop = NULL;
    }
}

#endif /* _WIN32 */

#ifdef __cplusplus
}
#endif

#endif /* FTLOG_IMPLEMENTED */
#endif /* FTLOG_IMPLEMENTATION */
