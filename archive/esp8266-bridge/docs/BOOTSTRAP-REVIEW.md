# Bootstrap Review

Review of the project bootstrap (2026-03-09). This project is a head-start / placeholder — the code compiles and has the right structure, but has NOT been tested on hardware. This review is intended for whoever continues the work (human or AI agent).

## Factual Correctness — PASS

- Architecture consistently correct throughout: STB Motherboard (I2C master) <-> ESP8266 (I2C slave). No residual master-mode confusion.
- 4-wire only (GND, 3V3, DAT, CLK). IR explicitly excluded everywhere.
- Self-contained web UI: PROGMEM HTML, ESPAsyncWebServer, WebSocket. No NiceGUI, no Home Assistant, no external dependencies beyond the ESP itself.
- No stale TM1650 references in code. All remaining mentions are in comments explaining the chip family or address variants — appropriate context, not confusion.
- VDD = 3.3V correctly noted. No level-shifter language.

## Suitability for Continuation (AI or Human) — GOOD

### Strengths

- Clear head-start / placeholder labeling in `hw650_emu.h`, `hw650_emu.cpp`, `main.cpp`
- TODO items are specific and actionable (not vague wishes)
- The analyzer is a genuinely useful first tool — the suggested workflow (f -> d -> n -> w) is the right order of operations
- Comments explain *why* decisions were made, not just *what* the code does
- Placeholder key codes are clearly flagged

### Concerns about biasing a future agent

- README and headers prescribe a hardware swap (ESP32, STM32 hybrid) before exhausting software fixes. Interrupt-based START detection, clock stretching, and disabling WiFi during I2C should be tried first.
- The `~80% reliability` figure in `hw650_emu.h` comes from general ESP8266 I2C slave research, not from testing this specific code. Could be mistaken for measured data.

## Code Issues

### `detectStart()` false positives (`hw650_emu.cpp:255`)

The check `CLK==HIGH && DAT==LOW` triggers on **any** data bit where the master is sending a 0 while clock is high — not just START conditions. This **will** misfire on real bus traffic. Most critical functional issue.

### `waitClkHigh()` / `waitClkLow()` hang forever (`hw650_emu.cpp:316-326`)

Spin-waits with no timeout. If the STB stops clocking mid-transaction (power off, bus error), the ESP hangs permanently. Guaranteed hang scenario, not theoretical.

### `data[len] = '\0'` buffer overrun (`main.cpp:271`)

Writing one byte past the WebSocket `data` buffer. ESPAsyncWebServer typically allocates extra space so this works in practice, but it's technically UB.

### `analyzer_capture` conflicts with emulator (`main.cpp:307-334`)

In normal mode, this temporarily runs the analyzer for 2+ seconds. During that time, the STB's I2C transactions go unserviced. The STB may time out or enter an error state.

### `innerHTML +=` in web UI log (`main.cpp:145`)

The `msg` comes from the ESP (trusted), but if an analyzer ever echoes bus data containing HTML-like content, it would render as markup. Low risk in practice.

### Minor

- `BIT_DELAY_US` defined in `config.h` but unused — "probably not used in slave mode" comment is ambiguous.
- `KEY_ADDR_TM1650` (0x49) / `DISP_CTRL_ADDR` (0x48) address overlap: key reads to 0x49 match 0x48 in `isOurAddress()` and work correctly by coincidence via the `isRead` bit check. Deserves a comment.
- `charToSegments()` is defined but unused. Fine as a utility for future use.

## Action Items

| # | Issue | Severity | Effort | Action |
|---|-------|----------|--------|--------|
| 1 | `detectStart()` false-triggers on any data-0-while-CLK-high | **Critical** | Medium | Rewrite or add prominent warning — will misfire on hardware |
| 2 | `waitClkHigh()`/`waitClkLow()` hang forever on bus failure | **High** | Low | Add timeout + return error flag |
| 3 | `data[len] = '\0'` writes past WebSocket buffer | **Medium** | Low | Pass `len` to `deserializeJson` instead |
| 4 | `analyzer_capture` leaves STB unserviced for 2+ seconds | **Medium** | Low | Add warning, or block command in normal mode |
| 5 | `~80% reliability` figure presented without qualifier | **Low** | Trivial | Add "(reported for similar projects)" |
| 6 | README/header prescribe hardware swap before software fixes | **Low** | Trivial | Reword to prioritize interrupt-based detection first |
| 7 | `innerHTML +=` in web UI log | **Low** | Low | Use `textContent` or escape |
| 8 | `BIT_DELAY_US` defined but unused | **Trivial** | Trivial | Remove or comment out |
| 9 | 0x48/0x49 address overlap works by coincidence | **Trivial** | Trivial | Add explanatory comment |
| 10 | `charToSegments()` unused | **Trivial** | None | Leave as utility |

### Suggested triage

- **Fix before first hardware test:** #1, #2
- **Fix before handing off:** #3, #4
- **Fix whenever:** #5, #6, #7
- **Ignore:** #8, #9, #10
