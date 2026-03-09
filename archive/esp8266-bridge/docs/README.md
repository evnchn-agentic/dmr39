# DMR-39 Bridge

ESP8266 bridge for reading/controlling a SUPER DMR-39 set-top box by impersonating its front panel chip.

**Status: untested head-start.** The code compiles and has the right structure, but the I2C slave implementation has NOT been verified on hardware. The key codes are placeholders. Expect to iterate with hardware-in-the-loop.

## Architecture

```
STB Motherboard ──DAT/CLK──> ESP8266 (pretending to be HW650EP)
                                │
                            WiFi ──> Web UI on phone/laptop
```

The front panel board is **removed**. The ESP8266 connects directly to the STB's front panel header and impersonates the HW650EP display driver chip:

- **Captures display writes** from the STB → we see the current channel
- **Responds to key read polls** with injected key codes → remote control (CH+, CH-, Standby)

A USB-HDMI capture card on the STB provides visual feedback for development.

## Hardware

- **Board:** ESP8266 NodeMCU Amica
- **Front panel IC (being impersonated):** HW650EP (FD650 family, TM1650-compatible)
- **VDD:** 3.3V (confirmed)

### Wiring (4 wires)

| STB Front Panel Header | ESP8266 |
|------------------------|---------|
| GND | GND |
| VDD | 3V3 |
| DAT | D2 / GPIO4 |
| CLK | D1 / GPIO5 |

No IR line connected (pin headers don't fit).

## What works / what's placeholder

| Feature | Status |
|---------|--------|
| Signal analyzer (edge capture, I2C decode, freq) | Should work — passive, no timing-critical slave code |
| Display capture (reading what STB writes) | **Placeholder** — I2C slave bit-bang needs testing |
| Key injection (faking button presses) | **Placeholder** — key codes are guesses, need bus analysis |
| Web UI | Should work once WiFi is up |
| I2C slave reliability with WiFi active | **Unknown** — ESP8266 has no hardware I2C, WiFi interrupts may interfere |

## Known limitations

- ESP8266 has **no hardware I2C slave**. Everything is bit-banged in software. WiFi interrupts can preempt timing.
- We need to respond to **6 different I2C addresses** (0x24, 0x27, 0x34-0x37 in 7-bit notation). The ESP8266 Wire library only supports 1. We use a custom bit-bang approach instead.
- The STB's I2C clock speed is unknown (expected ~100kHz). If the ESP can't keep up, consider an ESP32 or STM32+ESP8266 hybrid.
- Key codes (standby, CH+, CH-) are **placeholders**. Use the analyzer to sniff real key presses first.

## Building

```
# Set WiFi credentials in platformio.ini build_flags first
pio run
pio run -t upload
pio device monitor
```

## Usage — suggested workflow

1. Flash the ESP8266, open serial monitor (115200 baud)
2. **First: check if the bus is alive**
   - `f` — measure CLK frequency. If 0 Hz, STB might not be sending to front panel when disconnected.
3. **Analyze the protocol**
   - `d` — capture 2 seconds + decode as I2C frames. Look for writes to 0x68-0x6E (display data) and reads from 0x49/0x4F (key scan).
   - `a` — raw edge log if decode looks wrong
   - `s` — continuous sniff for ongoing monitoring
4. **Once protocol is understood, try emulator mode**
   - `n` — start emulator (ESP becomes I2C slave)
   - `i` — inject a test key press
   - `c` — clear injected key
5. **Add WiFi for remote control**
   - `w` — connect WiFi and start web server
   - Open `http://<ESP_IP>/` in a browser

### Serial commands

| Cmd | Description |
|-----|-------------|
| `f` | Measure CLK frequency (is bus active?) |
| `a` | Capture bus edges for 2 seconds |
| `d` | Capture + decode as I2C frames |
| `s` | Continuous sniff mode (Enter to stop) |
| `n` | Start emulator (normal mode) |
| `i` | Inject test key (placeholder standby) |
| `c` | Clear injected key |
| `w` | Connect WiFi and start web server |

## TODO (needs hardware testing) — ABANDONED

Abandoned in favor of ESP32-C3 direct NEC IR approach. See `src/main.cpp` in project root.

- [x] ~~Verify STB sends I2C traffic when front panel is disconnected~~ N/A
- [x] ~~Determine actual I2C clock speed~~ N/A
- [x] ~~Find real key codes for standby, CH+, CH-~~ Done via IR capture (see `ir_codes.json`)
- [x] ~~Tune I2C slave timing for reliable operation~~ N/A (IR is timing-trivial)
- [x] ~~Test whether WiFi + I2C slave can coexist~~ N/A
- [x] ~~If unreliable: consider ESP32~~ Resolved: ESP32-C3 chosen with IR instead of I2C

## References

- See `[AI GENERATED] Reverse-engineering the SUPER DMR-39 STB front panel.md` for detailed research (with corrections annotated)
- [peter-kutak/FD650](https://github.com/peter-kutak/FD650) — FD650 Arduino driver + datasheets
- [linux-chenxing.org MSD7816 reference](http://linux-chenxing.org/kronus/klf7816_t2_02/) — confirms TM1650 on similar boards
