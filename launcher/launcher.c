/*
 * PianoPilot.exe - starts PianoPilot without a command window.
 *
 * All it does: find Python, then run  pythonw.exe "<this folder>\pianopilot.py".
 * Where it looks, in order:
 *   1. runtime\pythonw.exe (the Python that comes with PianoPilot's download)
 *   2. data\python.txt   (PianoPilot writes the Python it last ran with here)
 *   3. pythonw.exe on the PATH
 *   4. %LOCALAPPDATA%\Python\bin\pythonw.exe          (Python install manager)
 *   5. pyw.exe on the PATH                             (the classic "py" launcher)
 *   6. %LOCALAPPDATA%\Microsoft\WindowsApps\pythonw.exe (Microsoft Store Python)
 * If PianoPilot stops with an error in its first seconds, it says so and offers the log.
 *
 * Built without the C runtime (see build.sh), so it only uses kernel32, user32 and shell32.
 */

typedef unsigned short WORD;
typedef unsigned long DWORD;            /* 32-bit on Windows */
typedef int BOOL;
typedef void *HANDLE;
typedef unsigned short WCHAR;           /* L"..." text is 16-bit on Windows */
typedef unsigned long long SIZE_T;

typedef struct {
    DWORD cb;
    WCHAR *lpReserved, *lpDesktop, *lpTitle;
    DWORD dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
    WORD wShowWindow, cbReserved2;
    unsigned char *lpReserved2;
    HANDLE hStdInput, hStdOutput, hStdError;
} STARTUPINFOW;

typedef struct {
    HANDLE hProcess, hThread;
    DWORD dwProcessId, dwThreadId;
} PROCESS_INFORMATION;

#ifndef IMPORT
#define IMPORT __declspec(dllimport)
#endif
#ifndef NORETURN
#define NORETURN __declspec(noreturn)
#endif
IMPORT DWORD GetModuleFileNameW(HANDLE, WCHAR *, DWORD);
IMPORT DWORD GetFileAttributesW(const WCHAR *);
IMPORT HANDLE CreateFileW(const WCHAR *, DWORD, DWORD, void *, DWORD, DWORD, HANDLE);
IMPORT BOOL ReadFile(HANDLE, void *, DWORD, DWORD *, void *);
IMPORT BOOL CloseHandle(HANDLE);
IMPORT int MultiByteToWideChar(unsigned, DWORD, const char *, int, WCHAR *, int);
IMPORT DWORD SearchPathW(const WCHAR *, const WCHAR *, const WCHAR *, DWORD, WCHAR *, WCHAR **);
IMPORT DWORD GetEnvironmentVariableW(const WCHAR *, WCHAR *, DWORD);
IMPORT BOOL CreateProcessW(const WCHAR *, WCHAR *, void *, void *, BOOL, DWORD, void *, const WCHAR *,
                           STARTUPINFOW *, PROCESS_INFORMATION *);
IMPORT DWORD WaitForSingleObject(HANDLE, DWORD);
IMPORT BOOL GetExitCodeProcess(HANDLE, DWORD *);
IMPORT NORETURN void ExitProcess(unsigned);
IMPORT int MessageBoxW(HANDLE, const WCHAR *, const WCHAR *, unsigned);
IMPORT HANDLE ShellExecuteW(HANDLE, const WCHAR *, const WCHAR *, const WCHAR *, const WCHAR *, int);

#define INVALID_HANDLE ((HANDLE)(long long)-1)
#define INVALID_ATTRIBUTES 0xFFFFFFFFul
#define ATTR_DIRECTORY 0x10
#define GENERIC_READ 0x80000000ul
#define FILE_SHARE_READ 1
#define OPEN_EXISTING 3
#define CP_UTF8 65001
#define WAIT_OBJECT_0 0
#define MB_OK 0x0
#define MB_YESNO 0x4
#define MB_ICONERROR 0x10
#define MB_ICONWARNING 0x30
#define MB_SETFOREGROUND 0x10000
#define IDYES 6
#define SW_SHOWNORMAL 1

#define CAP 1024                      /* characters in a path buffer */
#define STARTUP_WATCH_MS 15000        /* how long to watch for a crash right after starting */
#define EXIT_ALREADY_REPORTED 3       /* pianopilot.py showed its own message, so stay quiet */

static const WCHAR TITLE[] = L"PianoPilot";

static WCHAR dir[CAP];                /* the folder this exe is in, without a trailing backslash */
static WCHAR script[CAP];
static WCHAR workdir[CAP];             /* dir, plus a backslash when it's a drive root (C:) */
static WCHAR candidate[CAP];
static WCHAR env[CAP];
static WCHAR cmd[CAP * 2 + 16];
static WCHAR message[CAP * 2];
static WCHAR number[16];
static char raw[CAP * 3];
static STARTUPINFOW si;
static PROCESS_INFORMATION pi;

/* The compiler may emit calls to these even with no C runtime; provide them. */
void *memset(void *dest, int value, SIZE_T count) {
    volatile unsigned char *d = (volatile unsigned char *)dest;
    while (count--) *d++ = (unsigned char)value;
    return dest;
}
void *memcpy(void *dest, const void *src, SIZE_T count) {
    volatile unsigned char *d = (volatile unsigned char *)dest;
    const unsigned char *s = (const unsigned char *)src;
    while (count--) *d++ = *s++;
    return dest;
}

static int len(const WCHAR *s) {
    int n = 0;
    while (s[n]) n++;
    return n;
}

/* Copy (reset=1) or append (reset=0) text into a buffer of `cap` characters. Returns 0 if it didn't fit. */
static int put(WCHAR *dest, int cap, const WCHAR *text, int reset) {
    int n = reset ? 0 : len(dest);
    while (*text) {
        if (n >= cap - 1) { dest[n] = 0; return 0; }
        dest[n++] = *text++;
    }
    dest[n] = 0;
    return 1;
}

static int is_file(const WCHAR *path) {
    DWORD a = GetFileAttributesW(path);
    return a != INVALID_ATTRIBUTES && !(a & ATTR_DIRECTORY);
}

static const WCHAR *decimal(DWORD value) {
    int i = 15;
    number[i] = 0;
    do { number[--i] = (WCHAR)('0' + value % 10); value /= 10; } while (value && i > 0);
    return number + i;
}

static void open_thing(const WCHAR *what) {
    ShellExecuteW(0, L"open", what, 0, 0, SW_SHOWNORMAL);
}

/* data\python.txt: one line, UTF-8, the full path of pythonw.exe. */
static int read_saved_python(void) {
    DWORD got = 0;
    int n, start = 0;
    if (!put(env, CAP, dir, 1) || !put(env, CAP, L"\\data\\python.txt", 0)) return 0;
    HANDLE f = CreateFileW(env, GENERIC_READ, FILE_SHARE_READ, 0, OPEN_EXISTING, 0, 0);
    if (f == INVALID_HANDLE) return 0;
    BOOL ok = ReadFile(f, raw, sizeof(raw) - 1, &got, 0);
    CloseHandle(f);
    if (!ok || got == 0) return 0;
    if (got >= 3 && (unsigned char)raw[0] == 0xEF && (unsigned char)raw[1] == 0xBB && (unsigned char)raw[2] == 0xBF)
        start = 3;
    n = MultiByteToWideChar(CP_UTF8, 0, raw + start, (int)got - start, candidate, CAP - 1);
    if (n <= 0) return 0;
    candidate[n] = 0;
    while (n > 0 && (candidate[n - 1] == '\r' || candidate[n - 1] == '\n' || candidate[n - 1] == ' ' ||
                     candidate[n - 1] == '\t' || candidate[n - 1] == '"'))
        candidate[--n] = 0;
    return n > 0;
}

/* The Python that comes with the download, in the runtime folder next to this exe. */
static int bundled(void) {
    return put(candidate, CAP, dir, 1) && put(candidate, CAP, L"\\runtime\\pythonw.exe", 0);
}

static int on_path(const WCHAR *name) {
    DWORD n = SearchPathW(0, name, 0, CAP, candidate, 0);
    return n > 0 && n < CAP;
}

static int under_local_appdata(const WCHAR *rest) {
    DWORD n = GetEnvironmentVariableW(L"LOCALAPPDATA", env, CAP);
    if (n == 0 || n >= CAP) return 0;
    return put(candidate, CAP, env, 1) && put(candidate, CAP, rest, 0);
}

/* Start PianoPilot with the Python in `candidate`. 1 = started (even if it then failed), 0 = couldn't start. */
static int launch(void) {
    DWORD code = 0;
    if (!is_file(candidate)) return 0;
    if (!put(cmd, CAP * 2 + 16, L"\"", 1) || !put(cmd, CAP * 2 + 16, candidate, 0) ||
        !put(cmd, CAP * 2 + 16, L"\" \"", 0) || !put(cmd, CAP * 2 + 16, script, 0) ||
        !put(cmd, CAP * 2 + 16, L"\"", 0))
        return 0;
    si.cb = sizeof(si);
    if (!CreateProcessW(0, cmd, 0, 0, 0, 0, 0, workdir, &si, &pi)) return 0;
    CloseHandle(pi.hThread);
    if (WaitForSingleObject(pi.hProcess, STARTUP_WATCH_MS) == WAIT_OBJECT_0 &&
        GetExitCodeProcess(pi.hProcess, &code) && code != 0 && code != EXIT_ALREADY_REPORTED) {
        put(env, CAP, dir, 1);
        put(env, CAP, L"\\data\\pianopilot.log", 0);
        put(message, CAP * 2, L"PianoPilot closed while it was starting (error ", 1);
        put(message, CAP * 2, decimal(code), 0);
        put(message, CAP * 2, L").\n\nWhat went wrong is written in the log file. To see it as it happens, "
                              L"start PianoPilot with \"Start PianoPilot.bat\" instead.\n\nOpen the log now?", 0);
        if (is_file(env)) {
            if (MessageBoxW(0, message, TITLE, MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND) == IDYES)
                open_thing(env);
        } else {
            put(message, CAP * 2, L"PianoPilot closed while it was starting (error ", 1);
            put(message, CAP * 2, decimal(code), 0);
            put(message, CAP * 2, L").\n\nStart it with \"Start PianoPilot.bat\" to see what went wrong.", 0);
            MessageBoxW(0, message, TITLE, MB_OK | MB_ICONWARNING | MB_SETFOREGROUND);
        }
    }
    CloseHandle(pi.hProcess);
    return 1;
}

void start(void) {
    DWORD n = GetModuleFileNameW(0, dir, CAP);
    int i;
    if (n == 0 || n >= CAP) {
        MessageBoxW(0, L"PianoPilot is in a folder with a path that's too long. Move the PianoPilot folder "
                       L"somewhere shorter, like Downloads or Documents.", TITLE, MB_OK | MB_ICONERROR);
        ExitProcess(1);
    }
    for (i = (int)n - 1; i >= 0 && dir[i] != '\\' && dir[i] != '/'; i--) {}
    dir[i > 0 ? i : 0] = 0;
    put(workdir, CAP, dir, 1);
    if (len(workdir) == 2 && workdir[1] == ':') put(workdir, CAP, L"\\", 0);   /* "C:" alone means "wherever C: was last" */

    if (!put(script, CAP, dir, 1) || !put(script, CAP, L"\\pianopilot.py", 0) || !is_file(script)) {
        MessageBoxW(0, L"PianoPilot.exe has to stay in the PianoPilot folder, next to pianopilot.py.\n\n"
                       L"To start PianoPilot from somewhere else, use a shortcut instead: in the app, open "
                       L"Settings and click \"Add to desktop and Start menu\".",
                    TITLE, MB_OK | MB_ICONERROR);
        ExitProcess(1);
    }

    if ((bundled() && launch()) ||
        (read_saved_python() && launch()) ||
        (on_path(L"pythonw.exe") && launch()) ||
        (under_local_appdata(L"\\Python\\bin\\pythonw.exe") && launch()) ||
        (on_path(L"pyw.exe") && launch()) ||
        (under_local_appdata(L"\\Microsoft\\WindowsApps\\pythonw.exe") && launch()))
        ExitProcess(0);

    if (MessageBoxW(0, L"PianoPilot needs Python. Its own copy (the runtime folder) is missing, and Python "
                       L"isn't installed on this PC either.\n\n"
                       L"Download PianoPilot again (the download includes Python), or install Python from "
                       L"python.org and open PianoPilot again.\n\n"
                       L"Open the Python download page now?",
                    TITLE, MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND) == IDYES)
        open_thing(L"https://www.python.org/downloads/");
    ExitProcess(2);
}
