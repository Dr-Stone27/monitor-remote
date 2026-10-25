// ddc — talk to an external monitor over DDC/CI on Apple Silicon.
//
//   ddc get <vcp> [<vcp>...]   current and max value per code, one line each (hex or decimal)
//   ddc set <vcp> <value>      set a VCP code
//   ddc set-alt <vcp> <value>  set via source address 0x50 (how LG monitors take input switches)
//   ddc get-alt <vcp>...       read via source address 0x50
//   ddc caps                   the monitor's capabilities string
//
// DDC_REPEAT=1..5 changes how many copies of each command are sent (default 2).
// Uses the same private IOAVService calls as m1ddc / MonitorControl.

#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef CFTypeRef IOAVServiceRef;
extern IOAVServiceRef IOAVServiceCreateWithService(CFAllocatorRef, io_service_t);
extern IOReturn IOAVServiceReadI2C(IOAVServiceRef, uint32_t chip, uint32_t offset, void *buf, uint32_t len);
extern IOReturn IOAVServiceWriteI2C(IOAVServiceRef, uint32_t chip, uint32_t addr, void *buf, uint32_t len);

#define DDC_CHIP 0x37
#define HOST_ADDR 0x51

// MCCS asks the host to leave at least 50 ms between messages
#define DDC_GAP_US 50000

static int ddc_write_gap(IOAVServiceRef sv, uint8_t addr, const uint8_t *payload, int n, useconds_t repeat_gap) {
    uint8_t buf[40];
    buf[0] = 0x80 | n;
    memcpy(buf + 1, payload, n);
    uint8_t chk = 0x6E ^ addr;
    for (int i = 0; i < n + 1; i++) chk ^= buf[i];
    buf[n + 1] = chk;
    IOReturn r = kIOReturnSuccess;
    // Apple Silicon drops single writes often enough that sending twice is the norm;
    // DDC_REPEAT overrides it when probing how a monitor treats repeated commands
    const char *env = getenv("DDC_REPEAT");
    int repeats = env ? atoi(env) : 2;
    if (repeats < 1 || repeats > 5) repeats = 2;
    for (int i = 0; i < repeats; i++) {
        usleep(i == 0 ? DDC_GAP_US : repeat_gap);
        r = IOAVServiceWriteI2C(sv, DDC_CHIP, addr, buf, n + 2);
    }
    return r == kIOReturnSuccess ? 0 : -1;
}

// Read requests are answered straight away, so their duplicate can follow quickly
static int ddc_write(IOAVServiceRef sv, uint8_t addr, const uint8_t *payload, int n) {
    return ddc_write_gap(sv, addr, payload, n, 10000);
}

static int ddc_get_from(IOAVServiceRef sv, uint8_t addr, uint8_t vcp, int *cur, int *max) {
    for (int attempt = 0; attempt < 4; attempt++) {
        uint8_t p[2] = {0x01, vcp};
        if (ddc_write(sv, addr, p, 2)) continue;
        usleep(50000);
        uint8_t r[12] = {0};
        if (IOAVServiceReadI2C(sv, DDC_CHIP, addr, r, 12) != kIOReturnSuccess) continue;
        if (getenv("DDC_DEBUG")) {
            for (int i = 0; i < 12; i++) fprintf(stderr, "%02X ", r[i]);
            fprintf(stderr, "\n");
        }
        // 6E 88 02 <result> <vcp> <type> <maxH> <maxL> <curH> <curL> <chk>
        if (r[2] != 0x02 || r[4] != vcp) continue;
        if (r[3] != 0x00) return 1;  // monitor says the code is unsupported
        *max = r[6] << 8 | r[7];
        *cur = r[8] << 8 | r[9];
        return 0;
    }
    return -1;
}

static int ddc_get(IOAVServiceRef sv, uint8_t vcp, int *cur, int *max) {
    return ddc_get_from(sv, HOST_ADDR, vcp, cur, max);
}

static int ddc_caps(IOAVServiceRef sv, char *out, int outlen) {
    int off = 0, len = 0;
    for (int frag = 0; frag < 64; frag++) {
        uint8_t p[3] = {0xF3, off >> 8, off & 0xFF};
        uint8_t r[40] = {0};
        int ok = 0;
        for (int attempt = 0; attempt < 4 && !ok; attempt++) {
            if (ddc_write(sv, HOST_ADDR, p, 3)) continue;
            usleep(60000);
            if (IOAVServiceReadI2C(sv, DDC_CHIP, HOST_ADDR, r, 38) != kIOReturnSuccess) continue;
            // 6E <0x80|n> E3 <offH> <offL> <data...> <chk>
            if (r[2] == 0xE3 && ((r[3] << 8) | r[4]) == off) ok = 1;
        }
        if (!ok) return -1;
        int n = (r[1] & 0x7F) - 3;
        if (n <= 0) break;
        // The length byte comes from the monitor; never trust it past what was actually read
        if (n > 32) n = 32;
        for (int i = 0; i < n && len < outlen - 1; i++) {
            if (r[5 + i]) out[len++] = r[5 + i];
        }
        off += n;
    }
    out[len] = 0;
    return 0;
}

// Every external AV port has a proxy; only the one with a monitor on it answers.
static IOAVServiceRef find_monitor(void) {
    io_iterator_t it;
    io_registry_entry_t root = IORegistryGetRootEntry(kIOMainPortDefault);
    if (IORegistryEntryCreateIterator(root, kIOServicePlane, kIORegistryIterateRecursively, &it)) return NULL;
    io_service_t s;
    IOAVServiceRef candidates[8];
    int n = 0;
    while (n < 8 && (s = IOIteratorNext(it))) {
        io_name_t cls;
        IOObjectGetClass(s, cls);
        if (strcmp(cls, "DCPAVServiceProxy") == 0) {
            CFStringRef loc = IORegistryEntryCreateCFProperty(s, CFSTR("Location"), kCFAllocatorDefault, 0);
            if (loc && CFStringCompare(loc, CFSTR("External"), 0) == kCFCompareEqualTo) {
                IOAVServiceRef sv = IOAVServiceCreateWithService(kCFAllocatorDefault, s);
                if (sv) candidates[n++] = sv;
            }
            if (loc) CFRelease(loc);
        }
        IOObjectRelease(s);
    }
    IOObjectRelease(it);
    // One external port in use needs no probe, which saves a round trip on every command
    if (n == 1) return candidates[0];
    IOAVServiceRef found = NULL;
    for (int i = 0; i < n; i++) {
        int cur, max;
        if (!found && ddc_get(candidates[i], 0x10, &cur, &max) == 0) found = candidates[i];
        else CFRelease(candidates[i]);
    }
    return found;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: ddc get <vcp> | set <vcp> <value> | set-alt <vcp> <value> | caps\n");
        return 2;
    }
    IOAVServiceRef sv = find_monitor();
    if (!sv) {
        fprintf(stderr, "no external monitor answered over DDC\n");
        return 1;
    }
    if ((!strcmp(argv[1], "get") || !strcmp(argv[1], "get-alt")) && argc >= 3) {
        // Several codes in one run saves finding the monitor again for each
        uint8_t addr = !strcmp(argv[1], "get-alt") ? 0x50 : HOST_ADDR;
        int failed = 0;
        for (int i = 2; i < argc; i++) {
            int cur, max;
            int r = ddc_get_from(sv, addr, (uint8_t)strtol(argv[i], NULL, 0), &cur, &max);
            if (r == 1) { printf("unsupported\n"); failed = 3; }
            else if (r) { printf("err\n"); failed = 1; }
            else printf("%d %d\n", cur, max);
        }
        return failed;
    }
    if ((!strcmp(argv[1], "set") || !strcmp(argv[1], "set-alt")) && argc == 4) {
        int v = (int)strtol(argv[3], NULL, 0);
        uint8_t p[4] = {0x03, (uint8_t)strtol(argv[2], NULL, 0), v >> 8, v & 0xFF};
        uint8_t addr = !strcmp(argv[1], "set-alt") ? 0x50 : HOST_ADDR;
        if (ddc_write_gap(sv, addr, p, 4, DDC_GAP_US)) { fprintf(stderr, "write failed\n"); return 1; }
        return 0;
    }
    if (!strcmp(argv[1], "caps")) {
        char caps[2048];
        if (ddc_caps(sv, caps, sizeof caps)) { fprintf(stderr, "no reply\n"); return 1; }
        printf("%s\n", caps);
        return 0;
    }
    fprintf(stderr, "unknown command\n");
    return 2;
}
