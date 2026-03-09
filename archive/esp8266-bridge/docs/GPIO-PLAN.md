# GPIO Pin Assignment Plan

## Current (in use — DO NOT CHANGE)
| Function | GPIO | NodeMCU | Notes |
|----------|------|---------|-------|
| DAT (SDA) | GPIO4 | D2 | I2C data to STB motherboard |
| CLK (SCL) | GPIO5 | D1 | I2C clock to STB motherboard |

## Front Panel Board (original, for sniffing)
| Function | GPIO | NodeMCU | Notes |
|----------|------|---------|-------|
| FP DAT | GPIO0 | D3 | Front panel SDA — has pullup, boot-safe if HIGH |
| FP CLK | GPIO2 | D4 | Front panel SCL — has pullup, boot-safe if HIGH |

## Buttons (from original front panel PCB)

You found that the button "common" pin goes to the front panel's onboard microcontroller (not to VCC or GND). This means the HW650EP/FD650 chip actively drives the "common" as a scan line — it pulses voltages on it to detect which button in the matrix is pressed.

**We should NOT ignore this scanning common.** Monitoring it with the ESP8266's ADC (A0) lets us observe the scan pattern and understand the button matrix.

### Recommended approach: Monitor everything

| Wire | GPIO | NodeMCU | Notes |
|------|------|---------|-------|
| Button common (scan) | A0 | A0 | **Analog read** — monitor HW650EP scan voltage |
| Power button | GPIO12 | D6 | Digital input with pullup, also wire to front panel |
| CH+ button | GPIO13 | D7 | Digital input with pullup, also wire to front panel |
| CH- button | GPIO14 | D5 | Digital input with pullup, also wire to front panel |
| FP DAT | GPIO0 | D3 | Front panel SDA (I2C sniff) |
| FP CLK | GPIO2 | D4 | Front panel SCL (I2C sniff) |

**Wiring instructions for buttons:**
- Keep the buttons connected to the front panel PCB (both sides)
- Add a **parallel lead** from each button pin (non-common side) to the ESP GPIO
- Add a lead from the **common/scan pin** to A0
- The buttons still function normally with the HW650EP
- The ESP can simultaneously read the button states digitally AND monitor the scan pattern on A0
- The front panel board also needs VDD (3.3V) and GND

**A0 voltage limit:** ESP8266 A0 accepts 0-1V (NodeMCU has a voltage divider: 0-3.3V). If the scan voltage exceeds 3.3V, add a resistor divider. Measure first with a multimeter before connecting.

### What this gives us
1. **A0 analog readings** of the scan pattern tell us what the common pin does
2. **GPIO12/13/14 digital reads** tell us when buttons are physically pressed
3. **GPIO0/2 I2C sniff** captures the key codes the HW650EP sends to the STB
4. Combining all three, we learn exactly which key code corresponds to which button

## Summary of all pins after full wiring
| GPIO | NodeMCU | Function |
|------|---------|----------|
| 4 | D2 | STB DAT (SDA) — to motherboard |
| 5 | D1 | STB CLK (SCL) — to motherboard |
| 0 | D3 | FP DAT (I2C sniff) |
| 2 | D4 | FP CLK (I2C sniff) |
| 12 | D6 | Power button (parallel tap) |
| 13 | D7 | CH+ button (parallel tap) |
| 14 | D5 | CH- button (parallel tap) |
| A0 | A0 | Button common/scan line (analog monitor) |
