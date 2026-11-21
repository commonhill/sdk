/* insncap: the QEMU TCG plugin that counts a guest program's instructions, caps them, and makes
 * everything the program can observe the same on every run. Part of the instruction-count kit (sdk/insncap, vendored verbatim into each project
 * that uses it). Loaded by qemu-x86_64 in the grader's sandbox (sandbox.py) as
 *
 *     -plugin insncap.so,limit=<N>,fd=<F>[,profile=tree,roots=<R>:<R>...][,frames=1]
 *
 * The count. Every executed translation block adds its guest instructions. A rep-prefixed string
 * instruction (movs, stos, cmps, scas, lods, ins, outs) also adds one for every memory access it
 * makes, because a single such instruction can do unbounded work (a 1 MiB `rep movsb` is one
 * instruction).
 *
 * The guest's memory. In user-mode emulation a guest address becomes the host's `guest_base +
 * address`, so a guest that can make any pointer (C, unsafe Rust, Python's ctypes or numpy's
 * `__array_interface__`) could reach the emulator's own memory, the count included. The guest's
 * whole space is the emulator's reserved one (sandbox.py's `-R`, GUEST_SPACE), and the emulator
 * itself checks every guest load and store against it before it happens (qemu.patch): one past it
 * ends the process with status 135 and nothing written here, which sandbox.py reports as
 * `escape`. The system calls that take a guest address and that the host's kernel would follow
 * are checked here (futex, below).
 *
 * The cap and the report, on descriptor F, which the grader opened for it:
 *   - a block that starts with the counter over `limit`, or a guest exit with the counter over it,
 *     writes "over <limit>\n" and exits the process with status 137 (EXIT_OVER);
 *   - a second guest thread writes "thread\n" and exits with status 136 (EXIT_THREAD), so the
 *     count is the work (the policy below refuses clone, so this is a backstop);
 *   - with frames=1, the first read of each input frame writes "item <N>\n", N the count then;
 *   - a guest exit otherwise writes "count <N>\n".
 *
 * Framed input (frames=1). The grader writes its input as frames, each an 8-byte little-endian
 * length and that many bytes (a length of 0, or the input's end, ends the input), and sends the
 * next frame only once it has the program's answer to the last. The program reads plain bytes:
 * a read returns at most what is left of the current frame, waiting for all of it, so what each
 * read returns depends on the program's request and the frames alone, never on how the pipe
 * delivered them. A frame's first read reports its count: an item's work is everything from its
 * frame's first read to the next frame's, and all of the program's work is some item's. What the
 * program controls is only when it reads. It can't see a frame before it has answered the one
 * before (sandbox.py sends the next frame at an answer line, and a line that isn't the item's
 * answer is a wrong answer to it), so it can't do an item's work early; it can put off work its
 * answer doesn't need (freeing the last item's data, say) until after the next frame's first
 * read, which counts that work to the next item.
 *
 * The guest's system calls (`filter`). Those that tell time or draw randomness are answered here,
 * deterministically; what else reaches the host depends on the profile, and everything else fails
 * with ENOSYS.
 *   - time (clock_gettime, gettimeofday, time, clock_getres; the vDSO's versions are these system
 *     calls) is read from the count. The realtime clocks start at BASE_S, the others at 0, and
 *     each instruction is a nanosecond. Sleeping returns at once.
 *   - getrandom fills from a fixed-seed generator; each byte it writes counts as one.
 *   - set_tid_address answers a constant thread id. futex is wait and wake only (plain or with
 *     a bitset), on a word inside GUEST_SPACE: the host's kernel reads the word itself, so a word
 *     outside would reach the emulator's memory (EFAULT); the other operations, which take a
 *     second word or write thread ids, are refused (ENOSYS).
 *   - poll reports nothing ready and nothing wrong. Without this, poll on the output pipe would
 *     show whether the grader has read it yet, which is a clock.
 *   - processes, signals (to itself too: an abort ends in a fault instead), sockets, timers and
 *     resource usage are refused.
 * The `stdio` profile (the default), for a static program: reads only from 0 (the input), writes
 * only to 1 and 2, maps only anonymous memory or the input, and no file, pid, CPU affinity or
 * system identity; so the guest can't write to F, which carries this plugin's report.
 * The `tree` profile, for a program that reads a read-only tree of files (an interpreter and its
 * libraries): on top of stdio's, files are opened read-only, here, and the tree looks the same on
 * every host (`tree_call`):
 *   - a path is resolved here, a component at a time (symbolic links followed as the call says),
 *     to its real absolute path, and only the tree is there: the roots (`roots=`, sandbox.py's
 *     tree), what is under them, and the directories above them (which stat as directories, but
 *     Landlock, which allows reading and listing only the roots, keeps from being opened).
 *     Anything else is ENOENT, whatever the host has there: a name outside the tree, a link's
 *     target outside it and anything past that, and every failure outside it (`walk`);
 *   - stat gives the file's type, permissions and size, and nothing of the host's: device 1, the
 *     inode a hash of the real path (distinct per file, as the dynamic loader needs), one link,
 *     owner root, every time BASE_S, a directory's size 4096;
 *   - a directory lists its names sorted, with made-up inode numbers and offsets;
 *   - descriptors: any but F for reading, seeking, mapping and closing; 1 and 2 for writing;
 *   - readlink, access (never writable) and getcwd ("/") answer from the tree; uname, the user
 *     and group ids (0), the pid and thread id (TID) and CPU affinity (one CPU) are constants;
 *     ioctl is ENOTTY (no terminal), and prlimit64 reads only.
 * The time-stamp counter (rdtsc, rdtscp) is made deterministic in the emulator itself
 * (qemu.patch).
 *
 * Built against QEMU 11.1's plugin API (qemu-plugin.h version 7: userdata-taking callbacks, the
 * syscall filter, guest memory reads and writes). */
#define _GNU_SOURCE /* O_PATH, AT_EMPTY_PATH */
#include <qemu-plugin.h>
#include <glib.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

enum { EXIT_THREAD = 136, EXIT_OVER = 137 }; /* and 135, the emulator's own (qemu.patch) */

/* The guest's address space: the emulator's reserved space, sandbox.py's `-R 0x80000000`, which
 * qemu.patch keeps every guest access inside. */
#define GUEST_SPACE UINT64_C(0x80000000)

/* The realtime clocks' start: 2026-01-01T00:00:00Z. */
#define BASE_S INT64_C(1767225600)
#define NS_PER_S INT64_C(1000000000)

/* Linux's errno values (x86-64). */
enum {
    E_PERM = 1, E_NOENT = 2, E_BADF = 9, E_ACCES = 13, E_FAULT = 14, E_INVAL = 22, E_MFILE = 24,
    E_NOTTY = 25, E_SPIPE = 29, E_ROFS = 30, E_NAMETOOLONG = 36, E_NOSYS = 38, E_NOTDIR = 20,
    E_LOOP = 40,
};

/* The thread id set_tid_address answers (and, under `tree`, getpid and gettid): the host's would
 * differ from run to run. */
#define TID 2

/* x86-64 system call numbers. */
enum {
    SYS_READ = 0, SYS_WRITE = 1, SYS_OPEN = 2, SYS_CLOSE = 3, SYS_STAT = 4, SYS_FSTAT = 5,
    SYS_LSTAT = 6, SYS_POLL = 7, SYS_LSEEK = 8, SYS_MMAP = 9, SYS_MPROTECT = 10, SYS_MUNMAP = 11,
    SYS_BRK = 12, SYS_RT_SIGACTION = 13, SYS_RT_SIGPROCMASK = 14, SYS_RT_SIGRETURN = 15,
    SYS_IOCTL = 16, SYS_PREAD64 = 17, SYS_READV = 19, SYS_WRITEV = 20, SYS_ACCESS = 21,
    SYS_SCHED_YIELD = 24, SYS_MREMAP = 25, SYS_MADVISE = 28, SYS_NANOSLEEP = 35, SYS_GETPID = 39,
    SYS_EXIT = 60, SYS_UNAME = 63, SYS_FCNTL = 72, SYS_GETCWD = 79, SYS_READLINK = 89,
    SYS_GETTIMEOFDAY = 96, SYS_GETUID = 102, SYS_GETGID = 104, SYS_GETEUID = 107,
    SYS_GETEGID = 108, SYS_SIGALTSTACK = 131, SYS_ARCH_PRCTL = 158, SYS_GETTID = 186,
    SYS_TIME = 201, SYS_FUTEX = 202, SYS_SCHED_GETAFFINITY = 204, SYS_GETDENTS64 = 217,
    SYS_SET_TID_ADDRESS = 218, SYS_CLOCK_GETTIME = 228, SYS_CLOCK_GETRES = 229,
    SYS_CLOCK_NANOSLEEP = 230, SYS_EXIT_GROUP = 231, SYS_OPENAT = 257, SYS_NEWFSTATAT = 262,
    SYS_READLINKAT = 267, SYS_FACCESSAT = 269, SYS_PPOLL = 271, SYS_SET_ROBUST_LIST = 273,
    SYS_PRLIMIT64 = 302, SYS_GETRANDOM = 318, SYS_FACCESSAT2 = 439,
};

/* Linux's clock ids up to CLOCK_TAI: the realtime ones count from BASE_S. */
enum { CLOCK_IDS = 12 };
static const bool REALTIME[CLOCK_IDS] = {
    [0] = true,  /* CLOCK_REALTIME */
    [5] = true,  /* CLOCK_REALTIME_COARSE */
    [8] = true,  /* CLOCK_REALTIME_ALARM */
    [11] = true, /* CLOCK_TAI */
};

static struct qemu_plugin_scoreboard *board;
static qemu_plugin_u64 count;
static uint64_t limit = UINT64_MAX;
static int report_fd = -1;
static uint64_t random_state = UINT64_C(0x6172682d696e736e); /* fixed: one stream on every run */
static bool tree = false;   /* profile=tree */
static char **roots;        /* its roots=, real absolute paths */
static bool framed = false; /* frames=1 */

static void report(const char *line) {
    size_t n = strlen(line);
    while (n > 0) {
        ssize_t w = write(report_fd, line, n);
        if (w <= 0) {
            return;
        }
        line += w;
        n -= (size_t)w;
    }
}

static void over(unsigned int vcpu, void *udata) {
    char line[64];
    snprintf(line, sizeof line, "over %" PRIu64 "\n", limit);
    report(line);
    _exit(EXIT_OVER);
}

static void vcpu_init(unsigned int vcpu, void *udata) {
    if (vcpu > 0) {
        report("thread\n");
        _exit(EXIT_THREAD);
    }
}

/* Whether the instruction is a rep- or repne-prefixed string instruction: prefixes, then the
 * opcode. The prefixes are the legacy ones and REX bytes (0x40-0x4f), in any order and number: a
 * REX that isn't last is ignored by the CPU, not an end to the prefixes. */
static bool rep_string(const struct qemu_plugin_insn *insn) {
    uint8_t bytes[16];
    size_t n = qemu_plugin_insn_data(insn, bytes, sizeof bytes);
    bool rep = false;
    size_t i = 0;
    for (; i < n; i++) {
        uint8_t b = bytes[i];
        if (b == 0xf2 || b == 0xf3) {
            rep = true;
        } else if ((b & 0xf0) != 0x40 && b != 0xf0 && b != 0x2e && b != 0x36 && b != 0x3e &&
                   b != 0x26 && b != 0x64 && b != 0x65 && b != 0x66 && b != 0x67) {
            break;
        }
    }
    if (!rep || i >= n) {
        return false;
    }
    uint8_t op = bytes[i];
    return (op >= 0x6c && op <= 0x6f) || (op >= 0xa4 && op <= 0xa7) || (op >= 0xaa && op <= 0xaf);
}

static void tb_trans(struct qemu_plugin_tb *tb, void *udata) {
    size_t n = qemu_plugin_tb_n_insns(tb);
    qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(tb, QEMU_PLUGIN_INLINE_ADD_U64, count, n);
    qemu_plugin_register_vcpu_tb_exec_cond_cb(tb, over, QEMU_PLUGIN_CB_NO_REGS, QEMU_PLUGIN_COND_GT,
                                              count, limit, NULL);
    for (size_t i = 0; i < n; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        if (rep_string(insn)) {
            qemu_plugin_register_vcpu_mem_inline_per_vcpu(insn, QEMU_PLUGIN_MEM_RW,
                                                          QEMU_PLUGIN_INLINE_ADD_U64, count, 1);
        }
    }
}

static bool put(uint64_t address, const void *data, size_t len) {
    GByteArray *bytes = g_byte_array_new();
    g_byte_array_append(bytes, data, (guint)len);
    bool done = qemu_plugin_write_memory_vaddr(address, bytes);
    g_byte_array_unref(bytes);
    return done;
}

/* The count as nanoseconds on clock `id`: -E_INVAL for an id there is none of. */
static int64_t now_ns(uint64_t id) {
    if (id >= CLOCK_IDS) {
        return -E_INVAL;
    }
    int64_t ns = (int64_t)qemu_plugin_u64_sum(count);
    return REALTIME[id] ? BASE_S * NS_PER_S + ns : ns;
}

static int64_t clock_gettime_(uint64_t id, uint64_t tp) {
    int64_t ns = now_ns(id);
    if (ns < 0) {
        return ns;
    }
    int64_t ts[2] = {ns / NS_PER_S, ns % NS_PER_S};
    return put(tp, ts, sizeof ts) ? 0 : -E_FAULT;
}

static int64_t gettimeofday_(uint64_t tv, uint64_t tz) {
    int64_t ns = now_ns(0);
    int64_t value[2] = {ns / NS_PER_S, ns % NS_PER_S / 1000};
    int32_t zone[2] = {0, 0};
    if ((tv != 0 && !put(tv, value, sizeof value)) || (tz != 0 && !put(tz, zone, sizeof zone))) {
        return -E_FAULT;
    }
    return 0;
}

static int64_t time_(uint64_t t) {
    int64_t s = now_ns(0) / NS_PER_S;
    return t != 0 && !put(t, &s, sizeof s) ? -E_FAULT : s;
}

static int64_t clock_getres_(uint64_t id, uint64_t res) {
    if (id >= CLOCK_IDS) {
        return -E_INVAL;
    }
    int64_t one[2] = {0, 1};
    return res != 0 && !put(res, one, sizeof one) ? -E_FAULT : 0;
}

/* splitmix64. */
static uint64_t next_random(void) {
    uint64_t z = (random_state += UINT64_C(0x9e3779b97f4a7c15));
    z = (z ^ (z >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)) * UINT64_C(0x94d049bb133111eb);
    return z ^ (z >> 31);
}

static int64_t getrandom_(uint64_t buf, uint64_t len) {
    if (len > 33554431) { /* Linux's most per call */
        len = 33554431;
    }
    uint8_t chunk[256];
    for (uint64_t done = 0; done < len;) {
        size_t n = len - done < sizeof chunk ? (size_t)(len - done) : sizeof chunk;
        for (size_t i = 0; i < n; i += 8) {
            uint64_t r = next_random();
            memcpy(chunk + i, &r, n - i < 8 ? n - i : 8);
        }
        if (!put(buf + done, chunk, n)) {
            return done > 0 ? (int64_t)done : -E_FAULT;
        }
        done += n;
        qemu_plugin_u64_add(count, 0, n);
    }
    return (int64_t)len;
}

/* poll and ppoll: nothing ready, nothing wrong (each pollfd's revents 0). */
static int64_t poll_(uint64_t fds, uint64_t nfds) {
    if (nfds > 1024) {
        return -E_INVAL;
    }
    for (uint64_t i = 0; i < nfds; i++) {
        int16_t revents = 0;
        if (!put(fds + i * 8 + 6, &revents, sizeof revents)) {
            return -E_FAULT;
        }
    }
    return 0;
}

/* futex(uaddr, op, ...): 0 to let it through, or -errno. Only WAIT, WAKE, WAIT_BITSET and
 * WAKE_BITSET, whose one word is `uaddr`, and that word inside GUEST_SPACE: the host's kernel
 * follows the address itself, unchecked by the emulator. */
static int64_t futex_(uint64_t uaddr, uint64_t op) {
    switch (op & 0x7f) { /* without FUTEX_PRIVATE_FLAG and FUTEX_CLOCK_REALTIME */
    case 0: case 1: case 9: case 10:
        return uaddr < GUEST_SPACE && GUEST_SPACE - uaddr >= 4 ? 0 : -E_FAULT;
    default:
        return -E_NOSYS;
    }
}

/* ---- framed input (frames=1) ---- */

static uint64_t frame_left;  /* bytes of the current frame not yet read */
static bool frames_ended;    /* the input's end, or a frame of length 0 */

/* All `len` bytes from the host's descriptor 0, waiting for them; false at its end or an error. */
static bool read_input(void *buf, size_t len) {
    uint8_t *at = buf;
    while (len > 0) {
        ssize_t r = read(0, at, len);
        if (r < 0 && errno == EINTR) {
            continue;
        }
        if (r <= 0) {
            return false;
        }
        at += r;
        len -= (size_t)r;
    }
    return true;
}

/* read(0, buf, len) of framed input: at most what is left of the current frame, all of it waited
 * for; at a frame's start, its header first, and its count reported. */
static int64_t framed_read(uint64_t buf, uint64_t len) {
    if (len == 0 || frames_ended) {
        return 0;
    }
    if (frame_left == 0) {
        uint8_t header[8];
        if (!read_input(header, sizeof header)) {
            frames_ended = true;
            return 0;
        }
        uint64_t n = 0;
        for (int i = 7; i >= 0; i--) {
            n = n << 8 | header[i];
        }
        if (n == 0) {
            frames_ended = true;
            return 0;
        }
        frame_left = n;
        char line[64];
        snprintf(line, sizeof line, "item %" PRIu64 "\n", qemu_plugin_u64_sum(count));
        report(line);
    }
    uint8_t chunk[1 << 16];
    size_t n = sizeof chunk;
    if (len < n) {
        n = (size_t)len;
    }
    if (frame_left < n) {
        n = (size_t)frame_left;
    }
    if (!read_input(chunk, n)) { /* a frame cut short: the grader's input ends here */
        frames_ended = true;
        return 0;
    }
    frame_left -= n;
    return put(buf, chunk, n) ? (int64_t)n : -E_FAULT;
}

/* ---- the tree profile (profile=tree) ---- */

enum { FDS = 1024 };             /* descriptors the tree's state is kept for */
static char *fd_path[FDS];       /* an open file's real path (open_), for stat and *at calls */

struct entry {
    char *name;
    uint8_t type;
};
struct listing {                 /* a directory read whole and sorted (getdents_) */
    struct entry *entries;
    size_t n, next;
};
static struct listing *listings[FDS];

/* The guest's NUL-terminated string at `address` into `out`; false if it is unreadable or longer
 * than `size` - 1. */
static bool guest_string(uint64_t address, char *out, size_t size) {
    GByteArray *bytes = g_byte_array_new();
    size_t have = 0;
    bool ended = false;
    while (!ended && have < size) {
        uint64_t at = address + have;
        size_t want = 256 - (size_t)(at % 256); /* never across a page */
        g_byte_array_set_size(bytes, 0);
        if (!qemu_plugin_read_memory_vaddr(at, bytes, want)) {
            break;
        }
        for (size_t i = 0; i < want && have < size; i++) {
            out[have++] = (char)bytes->data[i];
            if (bytes->data[i] == 0) {
                ended = true;
                break;
            }
        }
    }
    g_byte_array_unref(bytes);
    return ended;
}

enum { OUTSIDE, ABOVE, INSIDE };

/* Where the absolute path `path` (with no "." or ".." in it) is: INSIDE a root (or one), ABOVE one
 * (a directory a root is under), or OUTSIDE the tree. */
static int place(const char *path) {
    size_t n = strcmp(path, "/") == 0 ? 0 : strlen(path);
    int found = OUTSIDE;
    for (char **r = roots; *r != NULL; r++) {
        size_t m = strlen(*r);
        if (strncmp(path, *r, m) == 0 && (path[m] == 0 || path[m] == '/')) {
            return INSIDE;
        }
        if (strncmp(*r, path, n) == 0 && (*r)[n] == '/') {
            found = ABOVE;
        }
    }
    return found;
}

enum { MAX_LINKS = 40 }; /* symbolic links followed in one resolution, as Linux's */

/* The absolute path `path` resolved a component at a time, as realpath does: its real path in `out`
 * and 0, the last component followed if it is a symbolic link and `follow_last`. The host is asked
 * about a component only when the directory it is in, real, is in the tree or above it (`place`),
 * and an error is the host's only when that directory is in the tree: anything else, a name
 * outside the tree, a resolution that passes through a directory outside it (by a link's target)
 * or fails there, is ENOENT. So no error tells anything of the host's own files, where they are. A
 * link outside the tree that leads into it (Debian's /lib) is followed. */
static int walk(const char *path, bool follow_last, char out[PATH_MAX]) {
    char todo[2 * PATH_MAX];
    char next_todo[2 * PATH_MAX];
    char cur[PATH_MAX] = ""; /* resolved so far; "" is / */
    int links = 0;
    snprintf(todo, sizeof todo, "%s", path);
    const char *rest = todo;
    for (;;) {
        rest += strspn(rest, "/");
        if (*rest == 0) {
            break;
        }
        int here = place(cur[0] != 0 ? cur : "/");
        if (here == OUTSIDE) {
            return -E_NOENT;
        }
        int failed = here == INSIDE ? 0 : -E_NOENT; /* an error's errno, or ENOENT above the tree */
        size_t len = strcspn(rest, "/");
        if (len > NAME_MAX) {
            return failed != 0 ? failed : -E_NAMETOOLONG;
        }
        char name[NAME_MAX + 1];
        memcpy(name, rest, len);
        name[len] = 0;
        rest += len;
        bool last = rest[strspn(rest, "/")] == 0;
        if (strcmp(name, ".") == 0) {
            continue;
        }
        if (strcmp(name, "..") == 0) {
            char *slash = strrchr(cur, '/');
            if (slash != NULL) {
                *slash = 0;
            }
            continue;
        }
        char next[PATH_MAX];
        if ((size_t)snprintf(next, sizeof next, "%s/%s", cur, name) >= sizeof next) {
            return failed != 0 ? failed : -E_NAMETOOLONG;
        }
        struct stat st;
        if (lstat(next, &st) != 0) {
            return failed != 0 ? failed : -errno;
        }
        if (S_ISLNK(st.st_mode) && (!last || follow_last)) {
            char target[PATH_MAX];
            ssize_t n = readlink(next, target, sizeof target - 1);
            if (n < 0 || ++links > MAX_LINKS) {
                return failed != 0 ? failed : n < 0 ? -errno : -E_LOOP;
            }
            target[n] = 0;
            if ((size_t)snprintf(next_todo, sizeof next_todo, "%s%s", target, rest) >= PATH_MAX) {
                return failed != 0 ? failed : -E_NAMETOOLONG;
            }
            memcpy(todo, next_todo, sizeof todo);
            rest = todo;
            if (target[0] == '/') {
                cur[0] = 0;
            }
            continue;
        }
        if (!last && !S_ISDIR(st.st_mode)) {
            return failed != 0 ? failed : -E_NOTDIR;
        }
        memcpy(cur, next, sizeof cur);
    }
    snprintf(out, PATH_MAX, "%s", cur[0] != 0 ? cur : "/");
    return 0;
}

/* `path` from `dirfd` (AT_FDCWD: the guest's working directory, always /) as a real absolute path
 * in `out`, its last component followed if it is a symbolic link and `follow`: 0, or -errno.
 * Only the tree is there (`walk`, `place`): a path that is, or resolves, outside it is ENOENT, and
 * no error tells anything of the host's own files. */
static int resolve(int64_t dirfd, const char *path, bool follow, char out[PATH_MAX]) {
    char joined[2 * PATH_MAX];
    if (path[0] == '/') {
        snprintf(joined, sizeof joined, "%s", path);
    } else if ((int32_t)dirfd == AT_FDCWD) {
        snprintf(joined, sizeof joined, "/%s", path);
    } else if (dirfd >= 0 && dirfd < FDS && fd_path[dirfd] != NULL) {
        snprintf(joined, sizeof joined, "%s/%s", fd_path[dirfd], path);
    } else {
        return -E_BADF;
    }
    if (strlen(joined) >= PATH_MAX) {
        return -E_NAMETOOLONG;
    }
    size_t len = strlen(joined);
    while (len > 1 && joined[len - 1] == '/') {
        joined[--len] = 0;
    }
    const char *base = strrchr(joined, '/') + 1;
    bool dots = *base == 0 || strcmp(base, ".") == 0 || strcmp(base, "..") == 0;
    int r = walk(joined, follow || dots, out);
    if (r == 0 && place(out) == OUTSIDE) {
        return -E_NOENT;
    }
    return r;
}

/* The guest's path argument resolved (resolve), or -errno. */
static int guest_path(int64_t dirfd, uint64_t address, bool follow, char out[PATH_MAX]) {
    char path[PATH_MAX];
    if (!guest_string(address, path, sizeof path)) {
        return -E_FAULT;
    }
    return resolve(dirfd, path, follow, out);
}

/* FNV-1a, made odd so that it is never 0: a file's inode is its real path's. */
static uint64_t path_hash(const char *p) {
    uint64_t h = UINT64_C(1469598103934665603);
    for (; *p; p++) {
        h = (h ^ (unsigned char)*p) * UINT64_C(1099511628211);
    }
    return h | 1;
}

static int64_t put_stat(uint64_t address, struct stat *st, const char *path) {
    st->st_dev = 1;
    st->st_ino = path_hash(path);
    st->st_nlink = 1;
    st->st_uid = 0;
    st->st_gid = 0;
    st->st_rdev = 0;
    if (S_ISDIR(st->st_mode)) {
        st->st_size = 4096;
    }
    st->st_blksize = 4096;
    st->st_blocks = (st->st_size + 511) / 512;
    st->st_atim.tv_sec = st->st_mtim.tv_sec = st->st_ctim.tv_sec = BASE_S;
    st->st_atim.tv_nsec = st->st_mtim.tv_nsec = st->st_ctim.tv_nsec = 0;
    return put(address, st, sizeof *st) ? 0 : -E_FAULT;
}

/* The name a descriptor without a path (0, 1, 2) has for its inode. */
static const char *fd_name(int fd) {
    static char name[32];
    snprintf(name, sizeof name, "fd:%d", fd);
    return fd >= 0 && fd < FDS && fd_path[fd] != NULL ? fd_path[fd] : name;
}

static int64_t fstat_(uint64_t fd, uint64_t buf) {
    struct stat st;
    if ((int)fd == report_fd || fd >= FDS) {
        return -E_BADF;
    }
    if (fstat((int)fd, &st) != 0) {
        return -errno;
    }
    return put_stat(buf, &st, fd_name((int)fd));
}

static int64_t fstatat_(int64_t dirfd, uint64_t path, uint64_t buf, uint64_t flags) {
    char first = 0;
    if ((flags & AT_EMPTY_PATH) != 0 && guest_string(path, &first, 1) && first == 0) {
        return fstat_((uint64_t)dirfd, buf);
    }
    char real[PATH_MAX];
    struct stat st;
    bool follow = (flags & AT_SYMLINK_NOFOLLOW) == 0;
    int r = guest_path(dirfd, path, follow, real);
    if (r < 0) {
        return r;
    }
    if ((follow ? stat(real, &st) : lstat(real, &st)) != 0) {
        return -errno;
    }
    return put_stat(buf, &st, real);
}

static int64_t open_(int64_t dirfd, uint64_t path, uint64_t flags) {
    if ((flags & O_ACCMODE) != O_RDONLY || (flags & (O_CREAT | O_TRUNC | O_PATH | 020000000)) != 0) {
        return -E_ACCES; /* 020000000: O_TMPFILE without O_DIRECTORY */
    }
    char real[PATH_MAX];
    int r = guest_path(dirfd, path, (flags & O_NOFOLLOW) == 0, real);
    if (r < 0) {
        return r;
    }
    int fd = open(real, (int)(flags & (O_ACCMODE | O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK)) | O_CLOEXEC);
    if (fd < 0) {
        return -errno;
    }
    if (fd >= FDS) {
        close(fd);
        return -E_MFILE;
    }
    fd_path[fd] = g_strdup(real);
    return fd;
}

static int compare_entries(const void *a, const void *b) {
    return strcmp(((const struct entry *)a)->name, ((const struct entry *)b)->name);
}

/* The directory open on `fd`, read whole and sorted at its first getdents. */
static struct listing *listing_of(int fd) {
    if (listings[fd] != NULL) {
        return listings[fd];
    }
    struct listing *l = g_new0(struct listing, 1);
    size_t cap = 64;
    l->entries = g_new(struct entry, cap);
    char buf[32768];
    for (;;) {
        long got = syscall(SYS_getdents64, fd, buf, sizeof buf);
        if (got <= 0) {
            break;
        }
        for (long at = 0; at < got;) {
            unsigned short reclen;
            memcpy(&reclen, buf + at + 16, sizeof reclen);
            if (l->n == cap) {
                cap *= 2;
                l->entries = g_renew(struct entry, l->entries, cap);
            }
            l->entries[l->n].type = (uint8_t)buf[at + 18];
            l->entries[l->n].name = g_strdup(buf + at + 19);
            l->n++;
            at += reclen;
        }
    }
    qsort(l->entries, l->n, sizeof *l->entries, compare_entries);
    listings[fd] = l;
    return l;
}

static void drop(int fd) {
    if (fd < 0 || fd >= FDS) {
        return;
    }
    g_free(fd_path[fd]);
    fd_path[fd] = NULL;
    struct listing *l = listings[fd];
    if (l != NULL) {
        for (size_t i = 0; i < l->n; i++) {
            g_free(l->entries[i].name);
        }
        g_free(l->entries);
        g_free(l);
        listings[fd] = NULL;
    }
}

/* getdents64: the sorted listing, from where the last call stopped, as linux_dirent64 records
 * (d_ino and d_off the entry's place, from 1). */
static int64_t getdents_(uint64_t fd, uint64_t buf, uint64_t size) {
    if (fd >= FDS || fd_path[fd] == NULL) {
        return -E_BADF;
    }
    struct listing *l = listing_of((int)fd);
    uint8_t out[32768];
    size_t used = 0;
    if (size > sizeof out) {
        size = sizeof out;
    }
    while (l->next < l->n) {
        const struct entry *e = &l->entries[l->next];
        size_t len = strlen(e->name);
        size_t reclen = (19 + len + 1 + 7) & ~(size_t)7;
        if (used + reclen > size) {
            break;
        }
        uint8_t *at = out + used;
        memset(at, 0, reclen);
        uint64_t place = l->next + 1;
        uint16_t rl = (uint16_t)reclen;
        memcpy(at, &place, 8);
        memcpy(at + 8, &place, 8);
        memcpy(at + 16, &rl, 2);
        at[18] = e->type;
        memcpy(at + 19, e->name, len);
        used += reclen;
        l->next++;
    }
    if (used == 0) {
        return l->next < l->n ? -E_INVAL : 0; /* a buffer too small for the next entry */
    }
    return put(buf, out, used) ? (int64_t)used : -E_FAULT;
}

static int64_t readlink_(int64_t dirfd, uint64_t path, uint64_t buf, uint64_t size) {
    char real[PATH_MAX];
    char target[PATH_MAX];
    int r = guest_path(dirfd, path, false, real);
    if (r < 0) {
        return r;
    }
    ssize_t n = readlink(real, target, sizeof target);
    if (n < 0) {
        return -errno;
    }
    if ((uint64_t)n > size) {
        n = (ssize_t)size;
    }
    return put(buf, target, (size_t)n) ? n : -E_FAULT;
}

static int64_t access_(int64_t dirfd, uint64_t path, uint64_t mode, uint64_t flags) {
    char real[PATH_MAX];
    int r = guest_path(dirfd, path, (flags & AT_SYMLINK_NOFOLLOW) == 0, real);
    if (r < 0) {
        return r;
    }
    if ((mode & W_OK) != 0) {
        return -E_ROFS;
    }
    return access(real, (int)(mode & (R_OK | X_OK))) == 0 ? 0 : -errno;
}

static int64_t uname_(uint64_t buf) {
    char u[6][65];
    memset(u, 0, sizeof u);
    strcpy(u[0], "Linux");
    strcpy(u[1], "commonhill");
    strcpy(u[2], "6.1.0");
    strcpy(u[3], "#1 SMP");
    strcpy(u[4], "x86_64");
    return put(buf, u, sizeof u) ? 0 : -E_FAULT;
}

/* A call only the tree profile answers: true and `*ret` set if `num` is one. */
static bool tree_call(int64_t num, uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, int64_t *ret) {
    switch (num) {
    case SYS_OPEN:
        *ret = open_(AT_FDCWD, a1, a2);
        return true;
    case SYS_OPENAT:
        *ret = open_((int64_t)(int32_t)a1, a2, a3);
        return true;
    case SYS_STAT:
        *ret = fstatat_(AT_FDCWD, a1, a2, 0);
        return true;
    case SYS_LSTAT:
        *ret = fstatat_(AT_FDCWD, a1, a2, AT_SYMLINK_NOFOLLOW);
        return true;
    case SYS_FSTAT:
        *ret = fstat_(a1, a2);
        return true;
    case SYS_NEWFSTATAT:
        *ret = fstatat_((int64_t)(int32_t)a1, a2, a3, a4);
        return true;
    case SYS_GETDENTS64:
        *ret = getdents_(a1, a2, a3);
        return true;
    case SYS_READLINK:
        *ret = readlink_(AT_FDCWD, a1, a2, a3);
        return true;
    case SYS_READLINKAT:
        *ret = readlink_((int64_t)(int32_t)a1, a2, a3, a4);
        return true;
    case SYS_ACCESS:
        *ret = access_(AT_FDCWD, a1, a2, 0);
        return true;
    case SYS_FACCESSAT:
        *ret = access_((int64_t)(int32_t)a1, a2, a3, 0);
        return true;
    case SYS_FACCESSAT2:
        *ret = access_((int64_t)(int32_t)a1, a2, a3, a4);
        return true;
    case SYS_GETCWD:
        *ret = a2 < 2 ? -E_INVAL : put(a1, "/", 2) ? 2 : -E_FAULT;
        return true;
    case SYS_FCNTL: /* the descriptor flags and status flags; nothing that makes a descriptor */
        if ((int)a1 == report_fd || a2 < 1 || a2 > 4) {
            *ret = (int)a1 == report_fd ? -E_BADF : -E_INVAL;
            return true;
        }
        return false;
    case SYS_IOCTL:
        *ret = -E_NOTTY;
        return true;
    case SYS_UNAME:
        *ret = uname_(a1);
        return true;
    case SYS_GETUID: case SYS_GETGID: case SYS_GETEUID: case SYS_GETEGID:
        *ret = 0;
        return true;
    case SYS_GETPID: case SYS_GETTID:
        *ret = TID;
        return true;
    case SYS_SCHED_GETAFFINITY: {
        uint64_t one = 1;
        *ret = a2 < sizeof one ? -E_INVAL : put(a3, &one, sizeof one) ? (int64_t)sizeof one : -E_FAULT;
        return true;
    }
    case SYS_PRLIMIT64:
        if (a1 != 0 || a3 != 0) { /* another process's, or a change */
            *ret = -E_PERM;
            return true;
        }
        return false;
    default:
        return false;
    }
}

/* ---- the filter ---- */

static bool filter(unsigned int vcpu, int64_t num, uint64_t a1, uint64_t a2, uint64_t a3,
                   uint64_t a4, uint64_t a5, uint64_t a6, uint64_t a7, uint64_t a8, int64_t *ret,
                   void *udata) {
    if (framed && a1 == 0) { /* the framed input is read only as plain reads */
        switch (num) {
        case SYS_READ:
            *ret = framed_read(a2, a3);
            return true;
        case SYS_PREAD64: case SYS_READV:
            *ret = -E_INVAL;
            return true;
        case SYS_LSEEK:
            *ret = -E_SPIPE;
            return true;
        default:
            break;
        }
    }
    if (tree && tree_call(num, a1, a2, a3, a4, ret)) {
        return true;
    }
    switch (num) {
    case SYS_READ: case SYS_PREAD64: case SYS_READV: case SYS_LSEEK:
        if (tree ? (int)a1 == report_fd : a1 != 0) { /* stdio: the input only */
            *ret = -E_BADF;
            return true;
        }
        return false;
    case SYS_WRITE: case SYS_WRITEV:
        if (a1 != 1 && a1 != 2) { /* the output and /dev/null only, never the report */
            *ret = -E_BADF;
            return true;
        }
        return false;
    case SYS_CLOSE:
        if (tree ? (int)a1 == report_fd : a1 > 2) {
            *ret = -E_BADF;
            return true;
        }
        drop((int)a1);
        return false;
    case SYS_MMAP:
        if ((a4 & 0x20) == 0) { /* not MAP_ANONYMOUS: a file's */
            int fd = (int)a5;
            if (fd == report_fd || (framed && fd == 0) || (!tree && fd != 0)) {
                *ret = -E_BADF;
                return true;
            }
        }
        return false;
    case SYS_FUTEX:
        *ret = futex_(a1, a2);
        return *ret != 0;
    case SYS_SET_TID_ADDRESS:
        *ret = TID; /* no thread ever exits but the program, so the address is never used */
        return true;
    case SYS_FCNTL: case SYS_PRLIMIT64: case SYS_SET_ROBUST_LIST: /* tree_call let them through */
        if (!tree) {
            *ret = -E_NOSYS;
            return true;
        }
        return false;
    case SYS_MPROTECT: case SYS_MUNMAP: case SYS_BRK: case SYS_RT_SIGACTION:
    case SYS_RT_SIGPROCMASK: case SYS_RT_SIGRETURN: case SYS_SCHED_YIELD: case SYS_MREMAP:
    case SYS_MADVISE: case SYS_EXIT: case SYS_SIGALTSTACK: case SYS_ARCH_PRCTL:
    case SYS_EXIT_GROUP:
        return false; /* to the host */
    case SYS_CLOCK_GETTIME:
        *ret = clock_gettime_(a1, a2);
        return true;
    case SYS_GETTIMEOFDAY:
        *ret = gettimeofday_(a1, a2);
        return true;
    case SYS_TIME:
        *ret = time_(a1);
        return true;
    case SYS_CLOCK_GETRES:
        *ret = clock_getres_(a1, a2);
        return true;
    case SYS_NANOSLEEP: case SYS_CLOCK_NANOSLEEP:
        *ret = 0;
        return true;
    case SYS_GETRANDOM:
        *ret = getrandom_(a1, a2);
        return true;
    case SYS_POLL: case SYS_PPOLL:
        *ret = poll_(a1, a2);
        return true;
    default:
        *ret = -E_NOSYS;
        return true;
    }
}

static void at_exit(void *udata) {
    char line[64];
    uint64_t n = qemu_plugin_u64_sum(count);
    if (n > limit) {
        over(0, NULL);
    }
    snprintf(line, sizeof line, "count %" PRIu64 "\n", n);
    report(line);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id, const qemu_info_t *info, int argc,
                                           char **argv) {
    for (int i = 0; i < argc; i++) {
        char *end = NULL;
        if (strncmp(argv[i], "limit=", 6) == 0) {
            limit = strtoull(argv[i] + 6, &end, 10);
        } else if (strncmp(argv[i], "fd=", 3) == 0) {
            report_fd = (int)strtol(argv[i] + 3, &end, 10);
        } else if (strcmp(argv[i], "profile=tree") == 0) {
            tree = true;
            continue;
        } else if (strncmp(argv[i], "roots=", 6) == 0 && roots == NULL) {
            roots = g_strsplit(argv[i] + 6, ":", -1);
            for (char **r = roots; *r != NULL; r++) {
                if ((*r)[0] != '/' || strcmp(*r, "/") == 0) {
                    return -1;
                }
            }
            continue;
        } else if (strcmp(argv[i], "profile=stdio") == 0) {
            continue;
        } else if (strcmp(argv[i], "frames=1") == 0) {
            framed = true;
            continue;
        } else {
            return -1;
        }
        if (end == NULL || *end != '\0') {
            return -1;
        }
    }
    if (report_fd < 0 || limit == UINT64_MAX || tree != (roots != NULL && roots[0] != NULL)) {
        return -1;
    }
    board = qemu_plugin_scoreboard_new(sizeof(uint64_t));
    count = qemu_plugin_scoreboard_u64(board);
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init, NULL);
    qemu_plugin_register_vcpu_tb_trans_cb(id, tb_trans, NULL);
    qemu_plugin_register_vcpu_syscall_filter_cb(id, filter, NULL);
    qemu_plugin_register_atexit_cb(id, at_exit, NULL);
    return 0;
}
