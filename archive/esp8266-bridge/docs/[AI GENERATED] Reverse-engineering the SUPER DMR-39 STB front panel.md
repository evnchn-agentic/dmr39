# **Reverse-engineering the SUPER DMR-39 STB front panel**

> **Note: This document was AI-generated before hardware testing. Corrections are marked with ~~strikethrough~~ for wrong info and <u>underline (HTML)</u> for corrections.**

**The 5-pin front panel connector on your SUPER DMR-39 almost certainly uses an FD650 or TM1650 LED controller chip communicating via an I2C-like 2-wire protocol on the DAT+CLK lines.** The "88969T" in the board marking is not a chip — it's a PCB design number assigned by the ODM. The actual front panel IC will be a small SOP-16 package near the 7-segment display, likely marked FD650, TM1650, or a compatible clone. <u>The chip is marked "HW650EP 4MCM551" — confirmed as FD650 family from the "650" in the part number.</u> This means off-the-shelf Arduino libraries already exist for full display control and key reading, making ~~ESP32~~ <u>ESP8266</u> integration straightforward once you confirm voltage levels and the specific chip variant.

## **The board marking decoded and the real front panel chip identified**

The naming convention `MSD7816_88969T_608_148mm_NP` follows the standard Chinese STB PCB pattern: **MSD7816** is the main SoC, **88969T** is the board design/model number (assigned by the factory), **608** is a sub-design code, **148mm** is the PCB length, and **NP** likely means "No PVR" or a similar feature flag. V1.01 is the board revision, dated April 2, 2016. No IC with the marking "88969T" exists in any datasheet database — extensive searches across AllDatasheet, Datasheet4U, DatasheetArchive, and component distributor databases returned zero results.

The actual front panel controller will be one of these common Chinese LED driver/keyboard scanner ICs, all in SOP-16 packages:

* **FD650 / FD650B / FD650S** (by Fuda Hisi, Fuzhou) [Datasheet4U](https://datasheet4u.com/datasheet/ETC/FD650-840610) — the most likely candidate based on MStar STB reference designs
* **TM1650** (by Titan Micro Electronics, Shenzhen) [DatasheetCafe](https://www.datasheetcafe.com/tm1650-datasheet-led-controller/) — functionally near-identical to FD650
* **Compatible clones**: AIP650EO, CS650, [Yoycart](https://www.yoycart.com/Product/568828921310/) HD2015, CH455H — all use the same protocol [Elektroda](https://www.elektroda.com/news/news4085556.html)

<u>**Update:** The chip is marked **HW650EP 4MCM551**, confirmed as FD650 family.</u>

The linux-chenxing.org project documents the **KLF7816\_T2\_02/03** reference board (the official MStar reference design for MSD7816) and explicitly confirms it uses a **TM1650** for the 7-segment display, 3 buttons, and power LED. [linux-chenxing](http://linux-chenxing.org/kronus/klf7816_t2_02/) Your SUPER DMR-39 board, being an MSD7816-based design from the same era, almost certainly uses TM1650 or FD650. ~~Open the STB and visually identify the SOP-16 IC near the display to confirm the exact marking.~~ <u>Confirmed: HW650EP (FD650 family).</u>

## **The protocol is I2C-like but not standard I2C**

Both FD650 and TM1650 use a **2-wire serial protocol that resembles I2C but is not fully compliant**. [Mbed](https://os.mbed.com/components/TM1637-LED-controller-32-LEDs-max-Keyboa/)[wordpress](https://allenchak.wordpress.com/2020/11/23/tm1650-fd650/) The DAT line functions as bidirectional SDA (data), and CLK functions as SCL (clock). The protocol uses standard I2C-style START and STOP conditions, MSB-first byte transmission, and receiver-generated ACK bits. [Components101](https://components101.com/ics/tm1650-led-driver-ic)[Bafnadevices](https://bafnadevices.com/wp-content/uploads/2024/04/TM1650_V2.2_EN.pdf) However, these chips do not use a standard I2C slave address — they expose registers directly on the bus, [Utmel](https://www.utmel.com/components/tm1650-led-drive-control-datasheet-pdf-circuit-and-pinout?id=849) which means an `i2cdetect` scan shows them appearing at **multiple addresses simultaneously** (a telltale fingerprint). [DFRobot](https://www.dfrobot.com/forum/topic/319680)

Each transaction consists of exactly two bytes: an address byte selecting the register, followed by a data byte. [GitHub](https://github.com/CarlWilliamsBristol/pxt-tm1650display) The timing runs at **20–100 kHz** clock speed [GitHub](https://github.com/arthur-liberman/linux_openvfd/blob/master/driver/controllers/fd650.c) (the MStar SDK typically bit-bangs at ~100 kHz using SAR GPIO pins on the MSD7816). Here is the complete register map:

| Address (8-bit) | Direction | Function |
| :---- | :---- | :---- |
| **0x48** | Write | Display control — on/off, brightness (8 levels), 7/8-segment mode |
| **0x49** (TM1650) or **0x4F** (FD650) | Read | Key scan — returns pressed key code |
| **0x68** | Write | Digit 1 segment data |
| **0x6A** | Write | Digit 2 segment data |
| **0x6C** | Write | Digit 3 segment data |
| **0x6E** | Write | Digit 4 segment data |

The display control byte at 0x48 encodes: bit 0 = display on/off, bits 6:4 = brightness level (0–7), bit 3 = 7-segment/8-segment mode. [DFRobot](https://www.dfrobot.com/forum/topic/319680)[StudyLib](https://studylib.net/doc/27435653/tm1650-v1.10-1) For example, `0x71` means display ON at maximum brightness in 8-segment mode. Segment data uses standard 7-segment encoding: `{0:0x3F, 1:0x06, 2:0x5B, 3:0x4F, 4:0x66, 5:0x6D, 6:0x7D, 7:0x07, 8:0x7F, 9:0x6F}`. [Keyestudio](https://www.keyestudio.com/blog/how-to-use-tm1650-4-digit-tube-display-251)[GitHub](https://github.com/jinzhifeng/SourceCode_old/blob/master/fd650.c)

The key difference between FD650 and TM1650 is the **key read address**: TM1650 uses 0x49, FD650 uses 0x4F (7-bit equivalents: 0x24 vs 0x27). [GitHub](https://github.com/peter-kutak/FD650) If your sniffer captures key-read transactions, this one byte difference tells you exactly which chip you have.

The MSD7816 SoC communicates with this front panel chip using **GPIO bit-banging** — specifically through SAR (ADC/GPIO) pins SAR1 (DAT) and SAR2 (CLK) on the reference design. [linux-chenxing](http://linux-chenxing.org/kronus/klf7816_t2_02/) The MStar SDK implements a software I2C master, not hardware I2C.

## ~~**VDD is almost certainly 5V — level shifting required for ESP32**~~ <u>**VDD is 3.3V (confirmed by measurement) — direct connection to ESP8266 is safe**</u>

~~The KLF7816\_T2\_02 reference board provides **+5V** on the front panel connector's VDD pin. Both the FD650 and TM1650 operate across **2.8V–5.5V**, but STB designs overwhelmingly use 5V because the LED display segments require higher current drive. The segment drive current exceeds **25mA** and digit drive exceeds **150mA**, which works better at 5V.~~

~~**ESP32 GPIOs are officially rated for 3.3V maximum** (absolute max VDD+0.3V = 3.6V). Connecting 5V signals directly risks damaging the ESP32. Before wiring anything:~~

~~1. **Measure VDD with a multimeter** between VDD and GND on the 5-pin connector while the STB is powered on~~
~~2. If **VDD = 5V** (expected): use a **BSS138-based bidirectional level shifter**~~
~~3. If **VDD = 3.3V** (less common but possible): direct connection is safe, just add **1kΩ series resistors** for protection~~

<u>**Confirmed: VDD = 3.3V.** Direct connection from STB front panel header to ESP8266 GPIOs is safe. No level shifter needed. The FD650/TM1650 operates fine at 3.3V (within its 2.8V–5.5V range).</u>

~~One important nuance: since the MSD7816 SoC itself runs at 3.3V internally, its SAR GPIO outputs are 3.3V. TM1650/FD650 powered at 5V will accept 3.3V as valid logic HIGH (VIH threshold ~2.0V for 5V CMOS). This means the SoC-to-chip direction works without level shifting even in the original design. However, the chip-to-SoC direction (key read data on DAT) outputs at **5V levels**, which is what creates the risk for your ESP32.~~

<u>Since VDD = 3.3V, this whole nuance is moot. Both the STB's SAR GPIOs and the ESP8266 GPIOs operate at 3.3V.</u>

## ~~**The IR pin is a standard 38kHz demodulated receiver output**~~ <u>**IR pin — not connected**</u>

~~The IR pin carries the **demodulated digital output** from an onboard IR receiver module (typically TSOP1738, VS1838B, or equivalent). This is a standard active-low signal: idle HIGH, with LOW pulses encoding the IR data. Chinese STBs almost universally use the **NEC protocol** (38kHz carrier, 32-bit command codes with address + inverse verification).~~

~~Connect this to ESP32 GPIO23 (through a level shifter or voltage divider if 5V) and use the **IRremoteESP8266** library (`crankyoldgit/IRremoteESP8266` on GitHub), which supports ESP32 and decodes 80+ IR protocols including NEC. The library's FAQ explicitly warns that ESP32 pins are not 5V tolerant.~~

<u>**The IR pin is not connected.** The available pin headers were slightly too large to make a reliable connection. Only 4 wires are used: GND, VDD (3.3V), DAT, CLK. IR control is not part of this project.</u>

## ~~**Wiring the ESP32 NodeMCU to the front panel connector**~~ <u>**Wiring the ESP8266 NodeMCU Amica to the STB motherboard**</u>

~~For the ESP32 NodeMCU Amica/DevKit board, use these GPIO assignments (chosen to avoid boot-strapping conflicts on GPIO 0, 2, 5, 12, 15 and flash pins GPIO 6–11):~~

~~STB Front Panel (5-pin)          ESP32 NodeMCU~~
~~DAT ──→ [Level Shifter HV1] ←→ [LV1] ──→ GPIO 19~~
~~CLK ──→ [Level Shifter HV2] ←→ [LV2] ──→ GPIO 18~~
~~IR  ──→ [Level Shifter HV3] ←→ [LV3] ──→ GPIO 23~~
~~VDD ──→ Level Shifter HV power (5V side)~~
~~GND ──→ GND (common ground — CRITICAL)~~

<u>**Actual wiring (ESP8266 NodeMCU Amica, 4 wires, no level shifter):**</u>

| STB Front Panel Header | ESP8266 |
|------------------------|---------|
| GND | GND |
| VDD (3.3V) | 3V3 |
| DAT | D2 / GPIO4 |
| CLK | D1 / GPIO5 |

<u>**Important architectural note:** The front panel board is **removed from the STB**. The ESP8266 connects directly to the STB motherboard's front panel header. The ESP impersonates the HW650EP chip as an I2C slave — it captures display writes from the STB and responds to key read polls with injected key codes.</u>

~~For **passive sniffing only** (read-only, simpler): GPIO 34 and 35 are input-only pins that work well, but still need voltage protection. A quick-and-dirty approach uses **2.2kΩ series + 3.3kΩ to GND** voltage dividers on each line, giving ~3.0V from a 5V signal. This is fine for initial protocol discovery but won't work for bidirectional communication.~~

~~For **injection mode** (bidirectional): use GPIO 18 (CLK) and GPIO 19 (DAT) with the BSS138 bidirectional level shifter. Add **4.7kΩ pull-up resistors** to VDD on both lines if you disconnect the front panel from the STB mainboard and drive it directly from the ESP32.~~

<u>No level shifter or voltage dividers needed — 3.3V throughout. The ESP8266's internal pull-ups on GPIO4/GPIO5 should be sufficient, but external 4.7kΩ pull-ups to 3.3V can be added if the bus is unreliable.</u>

## ~~**Sniffing and injection code for ESP32**~~ <u>**Sniffing and emulation code**</u>

~~**Phase 1 — Passive protocol sniffer (Arduino):**~~

~~This interrupt-driven sniffer captures I2C-like START/STOP conditions and assembles bytes from CLK+DAT transitions. Upload it, press buttons on the STB front panel and remote, and watch the serial monitor:~~

<u>**The sniffer is built into the firmware** as the `SignalAnalyzer` class. Use serial commands `a` (edges), `d` (I2C decode), `f` (frequency), `s` (continuous sniff) — see README.md for details.</u>

~~(ESP32 sniffer code removed — replaced by built-in analyzer)~~

**What to look for in sniffer output:** If you see `[S] 0x48 0x71 [P]` followed by `[S] 0x68 0x3F [P]` patterns, that confirms TM1650/FD650 — address 0x48 is display control, 0x68–0x6E are digit data. [Elektroda](https://www.elektroda.com/rtvforum/topic4052946.html) Key reads appear as `[S] 0x49 0xNN [P]` where 0xNN is the key code.

~~**Phase 2 — Command injection (Arduino):**~~

~~Once confirmed as FD650/TM1650, use the existing **`arkhipenko/TM1650` Arduino library** (available in Arduino Library Manager, works on all architectures including ESP32). It wraps standard Wire (I2C) calls.~~

<u>**Phase 2 — Emulator mode:**</u>

<u>The firmware includes an `HW650Emulator` class that acts as an I2C **slave**, impersonating the HW650EP chip. When the STB writes display data, the emulator captures it. When the STB reads the key register, the emulator responds with whatever key code has been injected via the web UI or serial commands.</u>

<u>**This is fundamentally different from the original AI suggestion** of using the ESP as an I2C master with the TM1650 library. In our architecture (front panel removed, ESP connected directly to STB), the **STB is the master** and the **ESP must be the slave**.</u>

<u>**Known challenge:** The ESP8266 has no hardware I2C slave peripheral. The slave mode is implemented as a best-effort bit-bang in `hw650_emu.cpp`. This may be unreliable, especially with WiFi active. If it doesn't work, consider:</u>
- <u>ESP32 (has hardware I2C, but still limited to 2 slave addresses)</u>
- <u>ATmega328P as I2C slave (has address mask register for multi-address) + ESP8266 for WiFi</u>
- <u>RP2040 (has dedicated multi-address I2C slave support)</u>

~~(ESP32 Wire master injection code removed — wrong direction for our architecture)~~

~~**Note on addressing**: The TM1650 datasheet uses 8-bit I2C addresses (0x48, 0x68–0x6E), but Arduino's Wire library uses 7-bit addresses (shifted right by 1): 0x24 for control, 0x34–0x37 for digits. The OpenVFD Linux driver and peter-kutak/FD650 repos use 7-bit notation.~~

<u>**Note on addressing**: The 8-bit vs 7-bit convention still applies. The ESP8266 emulator works with 8-bit addresses internally (0x48, 0x68, etc.) since that's what appears on the bus. The 7-bit equivalents (0x24, 0x34, etc.) matter if you ever use the Wire library.</u>

~~**MicroPython alternative** (for ESP32):~~

~~(MicroPython code removed — not applicable to ESP8266 Arduino firmware approach)~~

## **Open source projects and community resources worth bookmarking**

The most directly relevant repositories and references for this project are:

* **peter-kutak/FD650** (GitHub) — specifically built for "reuse set-top box with display and keys," [GitHub](https://github.com/peter-kutak/FD650)[GitHub](https://github.com/peter-kutak) includes FD650 datasheets (English + Chinese), TM1650 datasheets, and a Python control script. [GitHub](https://github.com/peter-kutak/FD650) Documents the key difference between FD650 and TM1650 key-read addresses [GitHub](https://github.com/peter-kutak/FD650) and includes `i2cdetect` output showing the characteristic multi-address fingerprint [github](https://github.com/peter-kutak/FD650)
* **arkhipenko/TM1650** (GitHub, [GitHub](https://github.com/microseti/tm1650) 35 stars) — Arduino library [Arduino](https://www.arduino.cc/reference/en/libraries/tm1650/) in the official Library Manager, works on ESP32, provides high-level display and key-reading functions. <u>Note: this library assumes the ESP is the I2C **master** controlling the display. In our architecture the ESP is the **slave**. This library would only be useful if the front panel were kept and the ESP drove it directly.</u>
* **arthur-liberman/linux\_openvfd** (GitHub) — Linux kernel driver for FD628/FD650/FD655, [github](https://github.com/arthur-liberman/linux_openvfd)[GitHub](https://github.com/LibreELEC/linux_openvfd) contains a production-quality bit-banged I2C implementation in `driver/controllers/fd650.c` [GitHub](https://github.com/arthur-liberman/linux_openvfd/blob/master/driver/controllers/fd650.c) that can serve as a protocol reference. Supports 20kHz and 100kHz clock modes [GitHub](https://github.com/arthur-liberman/linux_openvfd/blob/master/driver/controllers/fd650.c)
* **jefflessard/tm16xx-display** (GitHub) — comprehensive Linux kernel driver supporting TM1650, FD650, FD655, FD6551, AIP650, and 8 other compatible chips [github](https://github.com/jefflessard/tm16xx-display) via device tree configuration [GitHub](https://github.com/jefflessard/tm16xx-display)
* **linux-chenxing.org/kronus/klf7816\_t2\_02/** — official community documentation for the MSD7816 reference board, confirms TM1650 as the front panel controller, includes schematics for the KLF7816\_T2\_03 variant [linux-chenxing](http://linux-chenxing.org/kronus/klf7816_t2_02/)
* **WhitehawkTailor/I2C-sniffer** (GitHub) — well-tested ESP32-based passive I2C bus sniffer using interrupt-driven capture at 240MHz, directly applicable for this project's sniffing phase [GitHub](https://github.com/WhitehawkTailor/I2C-sniffer)

No specific results were found for "SUPER DMR-39" on satdreamgr.com, satfreaks.net, or other satellite forums — the device appears to be a generic white-label OEM product. However, the AllAboutCircuits forum thread on CT1642/SM1642 STB front panel reverse engineering (by user arkroan) [All About Circuits](https://forum.allaboutcircuits.com/threads/how-to-use-an-ct1642-or-sm1642-led-driver-with-key-scan-arduino-project.116955/) and the Elektroda.com thread on FD650 reuse [Elektroda](https://www.elektroda.com/news/news4085556.html) provide excellent practical guidance for similar projects.

## **Conclusion**

Your front panel uses an **~~FD650 or TM1650~~ <u>HW650EP (FD650 family)</u>** talking an I2C-like protocol at ~100kHz — confirmed by the MSD7816 reference design, the DAT+CLK+IR+VDD+GND pinout, and the overwhelming prevalence of these chips in Chinese STBs of this era. ~~The single most important first step is **measuring VDD with a multimeter**: if it reads 5V, you need a BSS138 bidirectional level shifter before connecting to the ESP32.~~ <u>VDD = 3.3V (confirmed). Direct connection to ESP8266 is safe.</u> ~~Open the STB and visually identify the SOP-16 IC near the display to confirm whether it's FD650 or TM1650~~ <u>Confirmed: HW650EP 4MCM551 (FD650 family).</u> — the protocol is identical except for the key-read address (0x4F vs 0x49). ~~Run the sniffer sketch first with the STB operating normally to verify the protocol in-situ, then switch to the TM1650 Arduino library for full bidirectional control.~~ <u>Run the built-in analyzer first (`f`, then `d`), then switch to emulator mode (`n`) where the ESP acts as an I2C slave impersonating the chip.</u> ~~The entire signal chain — from passive sniffing through to display injection and key emulation — is well-covered by existing open-source code, requiring only voltage-level adaptation for your ESP32.~~ <u>The signal chain from sniffing to emulation is covered by custom code in this firmware. The I2C slave implementation is a head-start and needs hardware testing.</u>
