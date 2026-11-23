# Printing to an F2_Pro thermal printer from Windows (no phone app)

Status: **working and verified** on one printer: Bluetooth name `F2_Pro_xxxx`, model string `C23`, firmware `2.0.2`,
576-dot (receipt-width) head. Text, graphics, dithered gradients and a 1,879-dot-tall page all printed cleanly.

This documents how it was made to work, so another Claude Code session (or person) can reuse it. The vendor protocol
is undocumented; everything here was recovered from the vendor's Android app (`com.lihu.fyhd`, "sharpfox" v1.7.3).

## Files in this folder

| File | Purpose |
|---|---|
| `f2print.py` | The whole thing: render text/images, encode, send, query printer. Also a small CLI. |
| `sbox.json` | 256-entry byte lookup table required by the image encoding (see "The trap" below). **Must sit next to `f2print.py`.** |
| `README.md` | This file. |

## Quick start

1. **Pair the printer in Windows** (Settings, Bluetooth & devices, Add device). Turn off Bluetooth on any phone that has
   used it: the printer talks to **one host at a time**. PIN, if asked, is typically `0000` or `1234`.
2. **Find its COM port**: More Bluetooth settings, COM Ports tab, use the **Outgoing** port named after the printer
   (it was `COM4` here; it will differ on another PC). If there is no outgoing port, click Add, Outgoing, pick the printer.
   Note: the plain "Connect" button in Windows connects for a couple of seconds and drops. That is normal, because
   nothing has opened the serial port. Opening the COM port from a program keeps it connected.
3. `pip install pyserial pillow`
4. Try it, in this order:
   ```
   python f2print.py COM4 status          # harmless: prints model, firmware, serial, battery
   python f2print.py COM4 text "Hello"    # first print
   ```
5. From code:
   ```python
   from f2print import render, print_image
   img = render([("DAILY UPDATE", 52, True), ("[ ] 9:00 Stand-up", 28, False)])   # (text, size, bold)
   print_image(img, "COM4")
   ```
   Any PIL image works with `print_image`; it should be 576 pixels wide (that is `WIDTH`).

## The protocol (serial, 115200 8N1 on the Bluetooth COM port)

All values hex. This is exactly the sequence the vendor app sends for this printer model.

| Step | Bytes | Notes |
|---|---|---|
| Wake up | `00` x 12 | Send first; the printer may be asleep. |
| Start job | `10 FF F1 01` | |
| Paper type 0 | `1F 80 01 10` | |
| Position adjust | `1F 11 51` | |
| **Image** | `1F 10` + `wb` (2 B, big-endian) + `h` (2 B) + `len` (4 B) + data | `wb` = row width in **bytes** (72 for 576 dots), `h` = rows, `len` = byte length of `data`. |
| Feed | `1B 4A n` | ESC J, feeds `n` dots (max 255 per command). App feeds 120 dots after an image. |
| Position adjust | `1F 11 50` | |
| **Stop job** | `10 FF F1 45` | **Always send.** Printer replies `AA`. See pitfalls. |

Image `data` = `zlib( TABLE[ packed_pixels ] )` where:
- `packed_pixels`: 1 bit per pixel, MSB first, black = 1, rows padded to whole bytes.
- `TABLE`: the fixed 256-entry substitution in `sbox.json` (`out[i] = TABLE[in[i]]`, byte by byte, position-independent).
- zlib: standard zlib stream (header `28 91`), **window size 10 bits (1 KB)**, default level, memLevel 8. Python:
  `zlib.compressobj(-1, zlib.DEFLATED, 10, 8, zlib.Z_DEFAULT_STRATEGY)`.

Queries (no printing; reply arrives within ~1 s):

| Command | Bytes | Reply seen |
|---|---|---|
| status | `10 FF 40` | `00` (assumed OK) |
| model | `10 FF 20 F0` | ASCII `C23` |
| firmware | `10 FF 20 F1` | ASCII `2.0.2` |
| serial | `10 FF 20 F2` | ASCII |
| battery | `10 FF 50 F1` | 2 bytes, e.g. `00 44` = 68 (looks like percent; it rose while charging) |
| density | `10 FF 10 00 n` (n = 0 low, 1 medium, 2 high) | `4F 4B` ("OK"); persists in the printer |

Other commands found in the app but not needed: self-test `10 FF F9` (did not print on this unit), MAC `10 FF 30 12`.

## The trap that cost most of the time

The vendor app has **several image encodings**, picked by Bluetooth name prefix. Only one is right for `F2_Pro`:

- Plain ESC/POS raster `1D 76 30 00 ...`: the `F2_Pro` printed it **as vertical stripes** (the box outline survived, text did not).
- `1F 10` header with **plain zlib** or with the **`codeESC`** variant (zlib, 10-bit window, *no* substitution): the printer
  replied `ER` or printed stripes.
- `1F 10` header with **`codeLihu`** = substitution table **then** 10-bit zlib: **correct**.

How it was found: `PrinterPort_A80` (used by the app's print screen) extends `PrinterPort_MiniBase`, whose `printBitmap`
falls through to `a.b(Bitmap)` for names not on its special-case list, and that calls the native `Compress.codeLihu`.
I had earlier copied a different class (`PrinterPort_F2`, `codeESC`) because the name looked right. The lesson: trace
from the app's actual print button down, not from the class whose name matches.

If you adapt this to **another printer model**, check `PrinterPort_MiniBase.printBitmap` in the decompiled app:
name prefixes `FlashToy`, `L11_`, `L12_`, `F10_`, `Y12_`, `Y12C_`, `C17`, `LP-1201W`, `S1` take other paths
(plain raster, optionally a second native step `F2Function.F2Data`). Print width also differs per model
(`BitmapFlex` in the app: `0x240` = 576 for F2_Pro, `0x180` = 384 for others).

## Pitfalls (all hit during development)

1. **Always send the stop-job `10 FF F1 45`**, even if the print fails midway. If a job is left open the printer stops
   reading, and the next write raises `SerialTimeoutException: Write timeout`. Sending stop-job (reply `AA`) recovers it.
   `print_image` does this in a `finally` block.
2. **Never send data under a `1F 10` header that isn't a valid compressed payload.** An uncompressed 14 KB payload put the
   printer into a runaway feed that ignored reset commands; power-cycling it was the only fix.
3. **Send a test with a tiny image first.** A wrong guess costs paper; a long one costs a lot.
4. The printer replies `ER` when the compressed payload is invalid. Read the reply after a test print to catch this.
5. Low battery can make prints faint or patchy. Charge it while testing (battery byte was `1E` = 30 at the worst point).
6. Small writes (256 B) with a 20 ms pause were reliable. Windows' Bluetooth COM port has no hardware flow control.
7. Pillow's default font is about 10 px tall, i.e. unreadable at 576 dots wide. Use a TrueType font at 24 px or larger.
8. Another host (phone) connected to the printer will make COM opening fail or the link drop.

## How the protocol was recovered (so you can repeat it for another model)

1. `apktool d sharpfox.apk` gave Smali + the native `libCompress.so` (ARM64). Smali is slow to read.
2. `jadx` (v1.4.7 works on Java 8) turned the APK into readable Java: the print path is in
   `com/lihu/fyhd/views/preprint/PrePrintImgActivity.printImg` -> `PrinterPort_A80` / `PrinterPort_MiniBase` ->
   `com/printerKit/command/a.java` (byte sequences) -> `com/communication/bt/*` (transport: classic Bluetooth SPP,
   UUID `00001101-...`, never BLE for this model).
3. The native compressor `Compress.codeLihu` has obfuscated (control-flow-flattened) code. Instead of reading it, the `.so`
   was **run under the Unicorn ARM64 emulator** with a fake JNI environment and a few fake libc calls, then probed with
   crafted inputs. Findings: it calls `changeBmpData` (a byte substitution; a single call with the bytes `0..255` yields the
   whole table) and `compress4` -> `compress3` (zlib `deflateInit2_` with windowBits = 10). The Python port was checked
   byte-for-byte against the emulated native output on random rasters of several sizes.
4. Verified on hardware: status/model/battery queries, then test images, then text pages and a long page.

## Not verified / assumptions

- Meaning of the status byte (`00` assumed OK) and that the battery byte is percent.
- Paper-out / cover-open behaviour is unknown; the code does not read errors during a print.
- Only a single `F2_Pro` unit was tested.
- Fonts path is Windows-only (`C:\Windows\Fonts\arial.ttf`); change `FONT_REG` / `FONT_BOLD` elsewhere.
