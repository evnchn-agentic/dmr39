# DMR-39 wiring (expected state, firmware 06fded9)

Pin map comes from `src/main.cpp` (`IR_RX_PIN 3`, `IR_TX_PIN 0`, `STB_VCC 1`).
Status column is what was measured on 2026-11-14 with the ESP on .180 USB.

```
 STB PCB                                          ESP32-C3 SuperMini
 ┌───────────────────────────────────┐            ┌──────────────────────┐
 │ IR receiver module (38 kHz)       │            │                      │
 │   OUT (demodulated, active LOW) ──┼──────────► │ GPIO3  IR_RX (sniff) │
 │                                   │            │                      │
 │   ╳  trace CUT between receiver   │            │                      │
 │      OUT and SoC IR input         │            │                      │
 │                                   │            │                      │
 │ SoC IR-input trace ◄──────────────┼─────────── │ GPIO0  IR_TX         │
 │                                   │            │  (idle HIGH, replays │
 │                                   │            │   NEC / raw frames)  │
 │ VCC rail (power sense) ───────────┼─────────── │ GPIO1  STB_VCC       │
 │                                   │            │  (must be <= 3.3 V)  │
 │ GND ──────────────────────────────┼─────────── │ GND                  │
 └───────────────────────────────────┘            │ USB-C ◄── .180 USB   │
                                                  │  (power + serial)    │
                                                  └──────────────────────┘
```

Signal path: remote → IR receiver → GPIO3 → firmware decodes, auto-forward is
ON by default → GPIO0 → STB SoC. Web `/ir?code=XXXX` also drives GPIO0.
Because the trace is cut, the STB only hears the remote through the ESP.

| Wire | Status 2026-11-14 | How checked |
|------|-------------------|-------------|
| GPIO0 → SoC IR input | Works | MENU, RIGHT, DOWN, OK, EXIT, INFO all acted on screen (HDMI capture), direct and via relay :8889 |
| GND | Works | Implied by GPIO0 working |
| GPIO3 ← receiver OUT | Unknown | Reads `RX=1` at idle, which fits an active-LOW output, but `caps=0`; needs a physical remote press |
| Trace cut | Unknown | Same remote press: the STB reacts and `caps` rises with `->FWD` = in-line works; STB reacts and `caps` stays 0 = trace not cut, or GPIO3 not connected |
| GPIO1 ← VCC sense | Suspect | Reads `vcc=0` while HDMI shows the STB on: wire off, wrong tap point, or damaged pin |

Before reconnecting GPIO1, measure the tap point with a multimeter. ESP32-C3 pins
are not 5 V tolerant. If the rail is above 3.3 V, put a divider in, for example
10 kΩ series and 20 kΩ to GND.

Power: the ESP currently runs from .180 USB. If it is ever fed from an STB rail
as well, do not connect both supplies at once.
