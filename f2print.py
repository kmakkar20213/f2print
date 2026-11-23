"""Print text/images on an F2_Pro thermal printer (Bluetooth name F2_Pro_xxxx, reports model "C23")
from Windows, over the printer's Bluetooth *outgoing COM port*. No phone, no vendor app.

Needs: Python 3, `pip install pyserial pillow`, and `sbox.json` next to this file.
See README.md for how the protocol was worked out and the pitfalls.

    python f2print.py COM4 status            # query status/battery/model/version/serial
    python f2print.py COM4 text "Hello"      # print a line of text
    python f2print.py COM4 image file.png    # print an image (scaled to 576 dots wide)
"""
import json
import os
import sys
import time
import zlib

import serial
from PIL import Image, ImageDraw, ImageFont

WIDTH = 576          # printable dots across for F2_Pro (the app uses 0x240)
CHUNK = 256          # write size; small chunks + a short pause kept the printer happy
FONT_REG = r"C:\Windows\Fonts\arial.ttf"
FONT_BOLD = r"C:\Windows\Fonts\arialbd.ttf"

_SBOX = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sbox.json")))


# ----------------------------------------------------------------------------- rendering
def font(size, bold=False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)


def _wrap(draw, text, fnt, max_w):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            trial = (cur + " " + word).strip()
            if draw.textlength(trial, font=fnt) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def render(blocks, margin=12):
    """blocks: list of (text, font_size, bold). Returns a WIDTH-wide greyscale image with word wrap."""
    probe = ImageDraw.Draw(Image.new("L", (WIDTH, 10)))
    rows, total = [], margin
    for text, size, bold in blocks:
        f = font(size, bold)
        for line in _wrap(probe, text, f, WIDTH - 2 * margin):
            rows.append((line, f, total))
            total += int(size * 1.25)
    img = Image.new("L", (WIDTH, total + margin), 255)
    d = ImageDraw.Draw(img)
    for line, f, y in rows:
        d.text((margin, y), line, font=f, fill=0)
    return img


# ----------------------------------------------------------------------------- encoding
def pack_1bpp(img):
    """1 bit per pixel, MSB first, black = 1. Threshold 200 is what was used for every verified print
    (the app's own packer uses 128; both work, 200 gives slightly bolder text)."""
    img = img.convert("L")
    w, h = img.size
    wb = (w + 7) // 8
    px = img.load()
    out = bytearray()
    for y in range(h):
        for bx in range(wb):
            b = 0
            for bit in range(8):
                x = bx * 8 + bit
                if x < w and px[x, y] < 200:
                    b |= 0x80 >> bit
            out.append(b)
    return wb, h, bytes(out)


def code_lihu(raw):
    """Python port of the app's native Compress.codeLihu (libCompress.so):
    1) substitute every byte through a fixed 256-entry table (changeBmpData),
    2) zlib-deflate with a 10-bit (1 KB) window, default level, memLevel 8."""
    sub = bytes(_SBOX[b] for b in raw)
    c = zlib.compressobj(-1, zlib.DEFLATED, 10, 8, zlib.Z_DEFAULT_STRATEGY)
    return c.compress(sub) + c.flush()


def build_job(img, feed_dots=120):
    """Return the list of byte strings the app sends, in order, to print `img`."""
    wb, h, raw = pack_1bpp(img)
    comp = code_lihu(raw)
    hdr = bytes([0x1F, 0x10, wb >> 8, wb & 255, h >> 8, h & 255]) + len(comp).to_bytes(4, "big")
    parts = [
        bytes(12),                        # printerWakeup
        bytes([0x10, 0xFF, 0xF1, 0x01]),  # startPrintJob
        bytes([0x1F, 0x80, 0x01, 0x10]),  # setPaperType(0)
        bytes([0x1F, 0x11, 81]),          # adjustPositionAuto(81)
        hdr + comp,                       # printBitmap: 1F 10 wb(2) h(2) len(4) + data
    ]
    n = feed_dots
    while n > 0:                          # printLinedots(120): ESC J n (feed n dots)
        k = min(n, 255)
        parts.append(bytes([0x1B, 0x4A, k]))
        n -= k
    parts.append(bytes([0x1F, 0x11, 80])) # adjustPositionAuto(80)
    return parts


STOP_JOB = bytes([0x10, 0xFF, 0xF1, 0x45])


def _send(s, data):
    for i in range(0, len(data), CHUNK):
        s.write(data[i:i + CHUNK])
        s.flush()
        time.sleep(0.02)


def print_image(img, port, feed_dots=120, dry_run=False):
    parts = build_job(img, feed_dots)
    if dry_run:
        return parts + [STOP_JOB]
    with serial.Serial(port, 115200, timeout=1, write_timeout=15) as s:
        try:
            for i, p in enumerate(parts):
                _send(s, p)
                if i == 0:
                    time.sleep(0.3)
        finally:
            # ALWAYS close the job: if it is left open the printer stops reading and writes time out.
            try:
                s.write_timeout = 3
                s.write(STOP_JOB)
            except Exception:
                pass
        time.sleep(2)
        return s.read(s.in_waiting)


# ----------------------------------------------------------------------------- queries (no printing)
QUERIES = {
    "status":  bytes([0x10, 0xFF, 0x40]),        # 00 = ok
    "model":   bytes([0x10, 0xFF, 0x20, 0xF0]),  # ASCII, e.g. "C23"
    "version": bytes([0x10, 0xFF, 0x20, 0xF1]),  # ASCII firmware, e.g. "2.0.2"
    "serial":  bytes([0x10, 0xFF, 0x20, 0xF2]),  # ASCII
    "battery": bytes([0x10, 0xFF, 0x50, 0xF1]),  # 2 bytes; second byte looks like percent
}


def query_all(port):
    out = {}
    with serial.Serial(port, 115200, timeout=1, write_timeout=5) as s:
        s.write(bytes(12))
        s.flush()
        time.sleep(0.5)
        s.reset_input_buffer()
        for name, cmd in QUERIES.items():
            s.write(cmd)
            s.flush()
            time.sleep(0.8)
            out[name] = s.read(s.in_waiting)
    return out


def set_density(port, level):
    """0 = low, 1 = medium, 2 = high. Persists in the printer. Printer replies b'OK'."""
    with serial.Serial(port, 115200, timeout=1, write_timeout=5) as s:
        s.write(bytes(12))
        s.flush()
        time.sleep(0.5)
        s.reset_input_buffer()
        s.write(bytes([0x10, 0xFF, 0x10, 0x00, level]))
        s.flush()
        time.sleep(1)
        return s.read(s.in_waiting)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    port, cmd = sys.argv[1], sys.argv[2]
    if cmd == "status":
        for k, v in query_all(port).items():
            print(f"{k:8} {v.hex(' ') or '(no reply)':20} {v!r}")
    elif cmd == "text":
        print_image(render([(" ".join(sys.argv[3:]), 40, False)]), port)
    elif cmd == "image":
        im = Image.open(sys.argv[3]).convert("L")
        im = im.resize((WIDTH, max(1, int(im.height * WIDTH / im.width))))
        print_image(im, port)
    else:
        print(__doc__)
