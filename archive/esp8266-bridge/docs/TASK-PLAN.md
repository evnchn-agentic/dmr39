# DMR-39 Bridge — Task Plan

## Goal
Remote channel control of SUPER DMR-39 STB from ESP8266 web UI.

## Critical Path

### 1. GET KEY CODES TO THE STB [IN PROGRESS — V3+V4 ready to test]
**Status:** GPIO key injection works on front panel HW650EP (D6→0x4C, D7→0x44),
but STB can't read them (separate I2C buses). Software I2C slave emulation on
ESP8266 has failed across 11+ attempts due to ISR latency.

**Current firmware (ready to flash):**
- **V3 ISR** (default): Direct ETS GPIO handler (bypasses Arduino dispatch),
  WiFi OFF, -O2 optimization, STOP detection, 200μs bit timeout
- **V4 Polling** (serial 'v'): Tight GPI register polling loop (~33MHz poll rate),
  unrolled state machine, zero interrupt overhead. Toggle: 'n'=V3, 'v'=V4

**Key improvements in this build:**
1. WiFi radio OFF (`-DWIFI_DISABLED`) — eliminates WiFi timer interrupts
2. `-O2` compiler optimization (was `-Os`) — better ISR codegen
3. Direct ETS_GPIO_INTR_ATTACH (V3) — skips Arduino dispatch table (~1-2μs saved)
4. Single GPI read per interrupt (V3) — consistent pin state snapshot
5. V4 polling loop — no ISR context save/restore at all (~30ns per poll)

**ESP32-C3 Zero (backup plan — found by user, needs headers):**
- Has HARDWARE I2C slave peripheral — would solve this problem entirely
- Address matching + ACK in silicon, zero CPU timing dependency
- Only 1 I2C controller (slave OR master, not both)
- Multi-address TM1650 emulation needs broadcast mode + SW filtering
- PlatformIO: `board = esp32-c3-devkitm-1`, `platform = espressif32`
- User is sourcing pin headers, ESP8266 stays primary until switchover announced

**Physical options (if software still fails):**
1. Short D2↔D3 and D1↔D4 (user reluctant — loses independent bus control)
2. Reconnect front panel to STB (ESP only for button GPIO)
3. Hardware I2C bridge chip (PCA9515/PCA9517)

### 2. INJECT KEY CODES VIA GPIO [CODE READY — needs step 1]
Non-blocking `startKeyInject()`: D6 HIGH → 0x4C, D7 HIGH → 0x44, D5 LOW → 0x34.
Serial commands: 'i'=CH_A, 'j'=CH_B. Web UI + HTTP API also wired.

### 3. WIFI CONNECTIVITY [DISABLED FOR PERF]
WiFi OFF in current build for maximum ISR/polling performance.
USB serial at 230400 baud is primary interface.
Can re-enable by removing `-DWIFI_DISABLED` from wifi_credentials.ini.
User may plug in USB WiFi adapter if needed.

### 4. DISPLAY READBACK (NICE TO HAVE)
After buses connected, snoop I2C for display write commands.

### 5. HDMI CAPTURE VERIFICATION
MJPEG stream on port 8888 (threaded, multi-client). Ready to verify channel changes.

## Discovered Key Codes
| Code | Trigger | Likely Function |
|------|---------|----------------|
| 0x74 | Idle | No key pressed |
| 0x34 | D5 LOW | Power/Standby |
| 0x4C | D6 HIGH | CH+ or CH- |
| 0x44 | D7 HIGH | CH+ or CH- |

## Architecture (current)
```
STB Motherboard ──DAT(D2)/CLK(D1)──> ESP8266 ──DAT(D3)/CLK(D4)──> Front Panel HW650EP
                                        │
                                     D6/D7 ──> Button matrix (key injection)
                                        │
                                     USB Serial ──> Host PC (230400 baud)
```

## I2C Slave Emulation History
| Version | Approach | Result |
|---------|----------|--------|
| V1-V3 | Basic ISR | Wrong addresses |
| V4 | Dual ISR (SDA+CLK) | Correct addresses, unstable |
| V5-V8 | Various ISR/polling | ~250kHz noise defeated all |
| V9 | Real-time signal bridge | Too slow at 160MHz |
| V10 | Stub (gave up) | N/A |
| V11 | Dual-ISR state machine | STB sends only 2 CLK cycles |
| V3(new) | Direct ETS ISR, WiFi-off, -O2 | **Ready to test** |
| V4(new) | Tight polling loop, unrolled | **Ready to test** |

## Serial Commands
| Key | Action |
|-----|--------|
| n | V3 ISR mode (default) |
| v | V4 polling mode |
| i | Inject CH_A (D6 HIGH) |
| j | Inject CH_B (D7 HIGH) |
| x | Stats (starts, stops, handled, nack, timeout, addr log) |
| a | Capture edges (2s) |
| d | Capture + I2C decode |
| f | Measure CLK frequency |
| s | Continuous sniff |
| p | Pin edge test (1s) |
| k | Front panel key scan |
| h | Help |

## Blockers
- **PRIMARY:** Key codes don't reach STB (V3+V4 firmware ready to test)
- ESP32-C3 needs pin headers before it can be used
