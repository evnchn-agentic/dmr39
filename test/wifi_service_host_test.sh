#!/bin/bash
# Host test for the stay-online WiFi logic in src/main.cpp (no board needed).
# Cuts the "=== Stay-online" section out of main.cpp, compiles it against stubs
# and drives a fake clock. Run: test/wifi_service_host_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
awk '/=== Stay-online/{on=1} /=== NEC TX/{on=0} on' src/main.cpp > "$tmp/section.inc"
[ -s "$tmp/section.inc" ] || { echo "section markers not found in src/main.cpp"; exit 1; }

cat > "$tmp/t.cpp" <<'EOF'
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <functional>
static uint32_t t_ms = 0;
static uint32_t millis() { return t_ms; }
static void delay(uint32_t) {}
#define F(x) x
#define WIFI_SSID "s"
#define WIFI_PASS "p"
enum WiFiEvent_t { ARDUINO_EVENT_WIFI_STA_DISCONNECTED, ARDUINO_EVENT_WIFI_STA_GOT_IP };
enum { WL_CONNECTED = 3, WL_DISCONNECTED = 6, WIFI_STA = 1 };
struct Restart {};
static int restarts = 0, begins = 0;
static uint32_t lastBegin = 0, maxGap = 0;
struct { void println(const char*) {} template <class... A> void printf(const char*, A...) {} } Serial;
struct { void restart() { restarts++; throw Restart{}; } } ESP;
static void (*eventCb)(WiFiEvent_t) = nullptr;
static int linkStatus = WL_CONNECTED;
static std::function<void()> onStatusRead;  // lets a test run the WiFi event task mid-wifiService()
struct {
    int status() { if (onStatusRead) { auto f = onStatusRead; onStatusRead = nullptr; f(); } return linkStatus; }
    void onEvent(void (*cb)(WiFiEvent_t)) { eventCb = cb; }
    void mode(int) {} void setHostname(const char*) {} void setAutoReconnect(bool) {}
    void persistent(bool) {} void setSleep(bool) {} void disconnect(bool) {}
    void begin(const char*, const char*) { if (begins && t_ms - lastBegin > maxGap) maxGap = t_ms - lastBegin; lastBegin = t_ms; begins++; }
} WiFi;
static void fire(WiFiEvent_t e) { if (eventCb) eventCb(e); }  // old firmware registers a handler, new one does not
#include "section.inc"

static int fails = 0;
#define CHECK(c, msg) do { if (!(c)) { printf("FAIL: %s\n", msg); fails++; } else printf("ok:   %s\n", msg); } while (0)
static bool tick() { try { wifiService(); return false; } catch (Restart&) { return true; } }
static void reset(uint32_t start) {
    t_ms = start; restarts = begins = 0; maxGap = 0; linkStatus = WL_CONNECTED;
    wifiDownSince = 0; nextRetryAt = 0; retryDelayMs = 1000; eventCb = nullptr;
}
static void install() {
#ifdef HAS_EVENT_HANDLER
    WiFi.onEvent(onWiFiEvent);
#endif
}

int main() {
    // 1. Disconnect event lands while wifiService() is between millis() and its reads (preemption).
    reset(20 * 60 * 1000); install(); tick();
    linkStatus = WL_DISCONNECTED;
    onStatusRead = [] { t_ms += 1; fire(ARDUINO_EVENT_WIFI_STA_DISCONNECTED); };
    CHECK(!tick(), "a fresh disconnect does not reboot, even if the event task runs mid-check");

    // 2. Real outage: reboot after 10 min, not before; backoff 1 s doubling to 60 s.
    reset(20 * 60 * 1000); install(); tick();
    linkStatus = WL_DISCONNECTED; fire(ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
    uint32_t down = t_ms; bool rebooted = false; uint32_t at = 0;
    for (int i = 0; i < 11 * 60 * 10 && !rebooted; i++) { t_ms += 100; if (tick()) { rebooted = true; at = t_ms - down; } }
    CHECK(rebooted && at > 10 * 60 * 1000 && at <= 10 * 60 * 1000 + 200, "reboots ~10 min into a real outage");
    CHECK(begins >= 12 && begins <= 20, "retries back off (1,2,4..60 s) instead of hammering");
    CHECK(maxGap <= 60 * 1000 + 100, "retry gap never exceeds the 60 s cap");

    // 3. Short outage then recovery: no reboot, and the next outage restarts backoff at 1 s.
    reset(20 * 60 * 1000); install(); tick();
    linkStatus = WL_DISCONNECTED; fire(ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
    for (int i = 0; i < 9 * 60 * 10; i++) { t_ms += 100; tick(); }
    linkStatus = WL_CONNECTED; fire(ARDUINO_EVENT_WIFI_STA_GOT_IP); t_ms += 100;
    CHECK(!tick(), "recovery at 9 min does not reboot");
    linkStatus = WL_DISCONNECTED; fire(ARDUINO_EVENT_WIFI_STA_DISCONNECTED); begins = 0;
    for (int i = 0; i < 15; i++) { t_ms += 100; tick(); }
    CHECK(begins == 1, "next outage retries again after 1 s");
    printf(fails ? "\n%d FAILED\n" : "\nALL PASS\n", fails);
    return fails ? 1 : 0;
}
EOF
flags=""
grep -q 'onWiFiEvent' "$tmp/section.inc" && flags="-DHAS_EVENT_HANDLER"
c++ -std=c++17 -w $flags -I"$tmp" "$tmp/t.cpp" -o "$tmp/t"
"$tmp/t"
