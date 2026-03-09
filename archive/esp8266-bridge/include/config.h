#pragma once

// ── Architecture (SOLDER-BLOB BRIDGE) ──
// STB Motherboard ←→ [solder blob] ←→ Front Panel HW650EP
// ESP8266 passively sniffs the shared I2C bus + injects keys via GPIO.
//
// Physical wiring (NodeMCU right-side pins, top to bottom):
//   D1 (GPIO5) ← FP SDA   ┐ solder blob → SDA bridged
//   D2 (GPIO4) ← STB SDA  ┘
//   D3 (GPIO0) ← STB SCL  ┐ solder blob → SCL bridged
//   D4 (GPIO2) ← FP SCL   ┘
//
// After blobs: D1=D2=SDA (shared), D3=D4=SCL (shared).
// Boot-safe: D3/D4 carry SCL which idles HIGH (satisfies GPIO0/GPIO2 boot reqs).
// ESP sniffs via one pin from each pair. Does NOT drive the bus.

#define PIN_DAT     4   // D2 / GPIO4  — I2C SDA (unused in GPIO-only mode)
#define PIN_CLK     5   // D1 / GPIO5  — I2C SCL (unused in GPIO-only mode)

// ── HW650EP / FD650 I2C-like addresses (8-bit notation) ──
// The STB writes to these addresses; the ESP captures the data.
// Display data:    0x68, 0x6A, 0x6C, 0x6E (digits 1-4)
// Display control: 0x48
// Key scan read:   0x49 (TM1650) or 0x4F (FD650/HW650EP)
#define DISP_BASE_ADDR   0x68
#define DISP_CTRL_ADDR   0x48
#define KEY_ADDR_TM1650  0x49
#define KEY_ADDR_FD650   0x4F

// ── VDD = 3.3V (confirmed) ──
// Direct connection to ESP8266 GPIOs is safe. No level shifter needed.

// ── Timing ──
#define BIT_DELAY_US       5   // microseconds — probably not used in slave mode
#define DISPLAY_POLL_MS  500   // how often to broadcast display state to web clients

// ── HTTP ──
#define HTTP_PORT 80

// ── Operating modes ──
enum OperatingMode {
    MODE_NORMAL,      // emulator active: capture display, respond to key reads, serve web UI
    MODE_ANALYZER,    // idle, waiting for serial analyzer commands
    MODE_PASSTHROUGH  // continuous bus sniff to serial
};
