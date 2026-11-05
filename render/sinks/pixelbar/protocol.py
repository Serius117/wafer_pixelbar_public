"""EDIFIER Halo PixelBar HID protocol implementation.

Derived from community reverse engineering (golden packet tests validated
against the protocol notes of halo-pixelbar-mcp and the working
implementation of claude-edifier-pixelbar-usage; both MIT licensed).
Command IDs and the mood-light payload layout were confirmed against the
vendor app (TempoHub 1.3.1, PyInstaller) on 2026-10-18 — see
docs/PIXELBAR_GUIDE.md.

Frame format (host -> device, 64 bytes):
  2E AA EC <cmd> <len_hi> <len_lo> <payload...> <crc> <zero padding...>

CRC = sum of bytes from index 1 (0xAA) onward, modulo 256.

NOTE: PROTOCOL_NOTES.md documents the third byte as 0xED while the working
code uses 0xEC (DEVICE_TYPE_EC). We follow the working code; both community
projects target the same device (VID 0x2D99 / PID 0xA106).
"""

from __future__ import annotations

# --- Device identification -------------------------------------------------
VID = 0x2D99
PID = 0xA106
HID_INTERFACE = 4
USAGE_PAGE = 0xFF14

# --- Frame constants --------------------------------------------------------
COMMON_OUT = 0x2E
HEADER = 0xAA
DEVICE_TYPE = 0xEC  # working code value; notes say 0xED (see module docstring)
REPORT_LEN = 64
MAX_PAYLOAD = 57  # 64 - 6 (header) - 1 (crc)

# --- Commands (PROTOCOL_NOTES.md + TempoHub 1.3.1 driver RE, 2026-10-18) -----
CMD_MOOD_QUERY = 0x6A
CMD_MOOD_SET = 0x6B
CMD_MOOD_RESET = 0x72
CMD_VOLUME_QUERY = 0x66
CMD_VOLUME_SET = 0x67
CMD_TEXT_QUERY = 0xE7
CMD_TEXT_SET = 0xE8
CMD_PIXEL_STATE = 0xEE
CMD_PIXEL_SET = 0xEF
CMD_PIXEL_FILE = 0x17
# Mapping only (no payload builder yet), recovered from the vendor driver:
CMD_SCREEN_RESET = 0x2B
CMD_TIME_CALIBRATION = 0x77

# --- File push states (from claude-usage implementation, verified 2026-10-17) --
# Host announces, device requests, host sends, device ACKs every chunk.
STATE_NOTIFY_DEVICE_RECEIVE = 0x00
STATE_NOTIFY_APP_SEND_DATA = 0x01
STATE_SEND_DATA_TO_DEVICE = 0x02
STATE_NOTIFY_APP_SEND_NEXT_DATA = 0x03
STATE_NOTIFY_DEVICE_SEND_DATA_END = 0x04
STATE_NOTIFY_APP_CHECK_FINISH = 0x05
STATE_NOTIFY_APP_CURSOR_ERROR = 0x06
STATE_NOTIFY_APP_CHECK_ERROR = 0x07
STATE_NOTIFY_APP_OTHER_ERROR = 0x08

# --- Mono bitmap file layout (256x32 display; 0x17 mode 0) --------------------
MONO_WIDTH = 256
MONO_HEIGHT = 32
MONO_UNIT_ROWS = 16
MONO_PAGE_COLS = 128
MONO_PAGE_BYTES = MONO_PAGE_COLS
MONO_UNIT_HEADER = 20
MONO_FILE_SIZE = 2 * (MONO_UNIT_HEADER + 4 * MONO_PAGE_BYTES)  # 1064
PIXEL_FILE_CHUNK = MAX_PAYLOAD - 4  # mode + state + 2-byte index

# Panel wiring quirk (measured 2026-10-17 on PID 0xA160):
# the second page row of each unit (physical rows 8-15 and 24-31) renders
# 10 columns left of its logical position (user-counted 10; the 8px-periodic
# grid only showed 10 mod 8 = 2). Pre-shift those rows right by 10 columns;
# their last 10 columns are unreachable.
PAGE_ROW_PRESHIFT = (0, 0, 10, 10)

# Whole-image calibration: content on this panel renders 10 columns right of
# its logical position, so the canvas is shifted left by 10 before encoding
# (user-verified 2026-10-17). The leftmost 10 canvas columns fall off-screen.
GLOBAL_X_SHIFT = 10

# --- Text limits ---------------------------------------------------------------
TEXT_MAX_CHARS = 16
TEXT_MAX_UTF8_BYTES = 48

# --- Mood light (0x6A query / 0x6B set / 0x72 reset) ------------------------
# Payload: [light_array_index, light_index, R, G, B, brightness, speed, (mode)]
# - light_array_index is device-specific and reported by the 0x6A query
#   (PID 0xA160 answers 0x20). A wrong value is ACKed but silently ignored
#   (measured 2026-10-18: 0x13 from community notes does nothing).
# - light_index selects the light element; it is the effect selector, because
#   the vendor app gives every effect its own element (USBPcap capture of
#   TempoHub 1.3.1 on PID 0xA160, 2026-10-18; Chinese effect names in GUIDE §5.1):
#     zone 1 -> breathing: R/G/B = colour to breathe, speed = breathing rate
#     zone 2 -> tide: R = 1        zone 3 -> solid: R = 1
#     zone 4 -> ripple: R = 0      zone 5 -> flow: R = 1
#     zone 6 -> shadow: R = 0
#   index 7 is the master switch, encoded in B (1=on, 0=off) with
#   brightness/speed = 255 ("keep").
# - brightness is 0-100 as the vendor app sends it (the device itself accepts
#   up to 250 and clamps 255 to 250, but 100 is the vendor UI maximum).
# - speed 255 = keep the stored value; the vendor app sent it for every effect
#   it did not want to retime (it only sent 8 for zones 1-2).
# - the vendor app writes each change exactly once; writing MOOD_WRITE_REPEAT
#   times is harmless on real hardware (measured 2026-10-18).
MOOD_LIGHT_ARRAY_INDEX_PIXELBAR = 0x20
MOOD_ZONE_BREATHING = 1
MOOD_ZONE_TIDE = 2
MOOD_ZONE_SOLID = 3
MOOD_ZONE_RIPPLE = 4
MOOD_ZONE_FLOW = 5
MOOD_ZONE_SHADOW = 6
MOOD_INDEX_SWITCH = 7
MOOD_INDEX_MAIN = MOOD_ZONE_BREATHING
MOOD_BRIGHTNESS_MAX = 100
MOOD_KEEP = 255
MOOD_SPEED_DEFAULT = MOOD_KEEP
MOOD_BREATH_SPEED = 8
MOOD_WRITE_REPEAT = 3
MOOD_WRITE_DELAY = 0.03

# Effect name -> (light zone, effect code in R). A code of None means the zone
# takes a colour in R/G/B instead of a code (breathing: the colour is the effect).
MOOD_EFFECTS: dict[str, tuple[int, int | None]] = {
    "breathing": (MOOD_ZONE_BREATHING, None),
    "tide": (MOOD_ZONE_TIDE, 1),
    "solid": (MOOD_ZONE_SOLID, 1),
    "ripple": (MOOD_ZONE_RIPPLE, 0),
    "flow": (MOOD_ZONE_FLOW, 1),
    "shadow": (MOOD_ZONE_SHADOW, 0),
}

# Effects whose zone accepts a real RGB write (the device keeps the colour and
# shows it as the effect). Other zones take the effect code only.
MOOD_COLOUR_EFFECTS = frozenset({"breathing", "solid"})

# Raw payloads TempoHub 1.3.1 sent for each effect (brightness 100), kept as
# the byte-exact reference for tests and for GUIDE §5.1.
MOOD_VENDOR_PAYLOADS: dict[str, list[int]] = {
    "breathing": [0x20, 0x01, 0x1C, 0xFF, 0xF8, 0x64, 0x08],
    "tide": [0x20, 0x02, 0x01, 0x00, 0x00, 0x64, 0x08],
    "solid": [0x20, 0x03, 0x01, 0x00, 0x00, 0x64, 0xFF],
    "ripple": [0x20, 0x04, 0x00, 0x00, 0x00, 0x64, 0xFF],
    "flow": [0x20, 0x05, 0x01, 0x00, 0x00, 0x64, 0xFF],
    "shadow": [0x20, 0x06, 0x00, 0x00, 0x00, 0x64, 0xFF],
}

# Same capture, colour writes to zone 3 (纯色静光): real RGB, brightness byte
# 0x0A/0x64 follows the app slider (USBPcap 2026-10-18, third capture).
MOOD_VENDOR_COLOR_PAYLOADS: dict[str, list[int]] = {
    "orange": [0x20, 0x03, 0xFF, 0x2F, 0x15, 0x64, 0xFF],
    "green": [0x20, 0x03, 0x00, 0xFF, 0x74, 0x64, 0xFF],
    "green_low": [0x20, 0x03, 0x00, 0xFF, 0x74, 0x0A, 0xFF],
}

# --- Scene categories ----------------------------------------------------------
SCENE_GAME = 1
SCENE_WORK = 2
SCENE_READING = 3
SCENE_CATS = 4
SCENE_DOGS = 5
SCENE_MEMES = 6
SCENE_CYBER = 7


# --- Frame construction ---------------------------------------------------------


def add_crc(frame: list[int]) -> list[int]:
    """Append checksum: sum of frame[1:], mod 256."""
    return frame + [sum(frame[1:]) & 0xFF]


def build_report(command: int, payload: list[int]) -> list[int]:
    """Build a 64-byte HID report for the given command and payload."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"Payload too large: {len(payload)} > {MAX_PAYLOAD}")

    frame = [
        COMMON_OUT,
        HEADER,
        DEVICE_TYPE,
        command & 0xFF,
        (len(payload) >> 8) & 0xFF,
        len(payload) & 0xFF,
        *payload,
    ]
    frame = add_crc(frame)
    return frame + [0] * (REPORT_LEN - len(frame))


# --- Payload builders -------------------------------------------------------------


def payload_mood_set(
    light_array_index: int,
    index: int,
    r: int,
    g: int,
    b: int,
    brightness: int = MOOD_BRIGHTNESS_MAX,
    speed: int = MOOD_SPEED_DEFAULT,
    mode: int | None = None,
) -> list[int]:
    """Mood light set payload (0x6B).

    Layout: [light_array_index, light_index, R, G, B, brightness, speed, mode?]
    The optional trailing `mode` byte is only sent when given (the vendor app
    uses it to switch colour mode: 1 = on, 2 = off).
    """
    for name, value in (("r", r), ("g", g), ("b", b),
                        ("brightness", brightness), ("speed", speed)):
        if not 0 <= value <= 255:
            raise ValueError(f"{name} out of range: {value} (0-255)")
    payload = [light_array_index & 0xFF, index & 0xFF, r, g, b, brightness, speed]
    if mode is not None:
        payload.append(mode & 0xFF)
    return payload


def payload_mood_effect(
    light_array_index: int,
    effect: str,
    brightness: int = MOOD_BRIGHTNESS_MAX,
    color: tuple[int, int, int] | None = None,
    speed: int = MOOD_SPEED_DEFAULT,
) -> list[int]:
    """Mood light set payload (0x6B) for one of the vendor app effects.

    Without `color` the effect code selects the stored effect. With `color` the
    effect's zone is written as a static colour instead -- only the colour-driven
    effects (breathing, solid) accept that; the vendor app does the same when a
    colour is picked (USBPcap 2026-10-18: zone 3 gets real RGB, e.g. FF 2F 15).
    """
    try:
        zone, code = MOOD_EFFECTS[effect]
    except KeyError:
        raise ValueError(f"unknown mood effect: {effect!r}") from None
    if color is not None:
        if effect not in MOOD_COLOUR_EFFECTS:
            raise ValueError(f"mood effect {effect!r} does not take a colour")
        r, g, b = color
        return payload_mood_set(light_array_index, zone, r, g, b, brightness, speed)
    if code is None:
        raise ValueError(f"mood effect {effect!r} needs a colour")
    return payload_mood_set(light_array_index, zone, code, 0x00, 0x00,
                            brightness, speed)


def payload_mood_switch(light_array_index: int, on: bool) -> list[int]:
    """Mood light master switch (0x6B, light index 7): B carries the flag."""
    return [light_array_index & 0xFF, MOOD_INDEX_SWITCH, 0x00, 0x00,
            0x01 if on else 0x00, MOOD_KEEP, MOOD_KEEP]


def payload_mood_reset(
    light_array_index: int, index: int = MOOD_INDEX_MAIN
) -> list[int]:
    """Mood light reset to factory effect (0x72): [light_array_index, index]."""
    return [light_array_index & 0xFF, index & 0xFF]


def payload_text_set(text: str) -> list[int]:
    """Text set payload: [0, utf8_len, utf8_bytes...]. Max 16 chars / 48 bytes."""
    utf8 = text.encode("utf-8")
    if len(text) > TEXT_MAX_CHARS or len(utf8) > TEXT_MAX_UTF8_BYTES:
        raise ValueError(
            f"Text too long: {len(text)} chars / {len(utf8)} bytes "
            f"(max {TEXT_MAX_CHARS} chars / {TEXT_MAX_UTF8_BYTES} bytes)"
        )
    return [0, len(utf8), *utf8]


def payload_pixel_theme_color(r: int, g: int, b: int) -> list[int]:
    """Pixel theme color: 03 <R> <G> <B> 00 00 FF FF FF."""
    return [0x03, r, g, b, 0x00, 0x00, 0xFF, 0xFF, 0xFF]


def payload_text_static(r: int, g: int, b: int, alignment: int) -> list[int]:
    """Static text: 01 <R> <G> <B> 00 02 00 <alignment> FF.

    Alignment: 0=left, 1=center, 2=right, 3=justified.
    """
    return [0x01, r, g, b, 0x00, 0x02, 0x00, alignment, 0xFF]


def payload_text_dynamic(r: int, g: int, b: int) -> list[int]:
    """Dynamic text (right-to-left scrolling): 01 <R> <G> <B> 00 02 01 01 FF."""
    return [0x01, r, g, b, 0x00, 0x02, 0x01, 0x01, 0xFF]


def payload_scene(r: int, g: int, b: int, category: int, template: int) -> list[int]:
    """Scene selection: 02 <R> <G> <B> 00 01 <category> <template> FF."""
    return [0x02, r, g, b, 0x00, 0x01, category, template, 0xFF]


def payload_scene_category(
    r: int, g: int, b: int, category: int, group: int = 0
) -> list[int]:
    """Select a scene category slot (template placeholder FF FF)."""
    return [0x02, r, g, b, group & 0xFF, 0x01, category & 0xFF, 0xFF, 0xFF]


def payload_scene_option(
    r: int, g: int, b: int, category: int, option: int, group: int = 0
) -> list[int]:
    """Select one template option inside a scene category."""
    return [0x01, r, g, b, group & 0xFF, 0x01, category & 0xFF, option & 0xFF, 0xFF]


def payload_clock(r: int, g: int, b: int) -> list[int]:
    """Clock scene: 01 <R> <G> <B> 00 01 00 00 FF."""
    return [0x01, r, g, b, 0x00, 0x01, 0x00, 0x00, 0xFF]


# --- File push (bitmap frames) ------------------------------------------------


def payload_file_start(mode: int, size: int) -> list[int]:
    """File push start: <mode> 01 <size_3> <size_2> <size_1> <size_0>."""
    return [
        mode & 0xFF,
        STATE_NOTIFY_DEVICE_RECEIVE,
        (size >> 24) & 0xFF,
        (size >> 16) & 0xFF,
        (size >> 8) & 0xFF,
        size & 0xFF,
    ]


def payload_file_data(mode: int, package_index: int, chunk: bytes) -> list[int]:
    """File push data: <mode> 02 <index_hi> <index_lo> <chunk...>."""
    return [
        mode & 0xFF,
        STATE_SEND_DATA_TO_DEVICE,
        (package_index >> 8) & 0xFF,
        package_index & 0xFF,
        *chunk,
    ]


def payload_file_end(mode: int) -> list[int]:
    """File push end: <mode> 03."""
    return [mode & 0xFF, STATE_NOTIFY_DEVICE_SEND_DATA_END]


# --- Volume -----------------------------------------------------------------------


def payload_volume_set(target: int) -> list[int]:
    """Volume set: <target>. Range 0-16."""
    if not 0 <= target <= 16:
        raise ValueError(f"Volume out of range: {target} (0-16)")
    return [target]


# --- Device responses -------------------------------------------------------------


def parse_response(raw: bytes | list[int]) -> tuple[int, list[int]] | None:
    """Parse one 64-byte device report into (command, payload).

    Device frame: 2F BB EC <cmd> <len_hi> <len_lo> <payload...> <crc> ...
    Returns None for unparseable/short reports.
    """
    data = [int(x) & 0xFF for x in raw]
    if len(data) < 6 or data[0] != 0x2F:
        return None
    command = data[3]
    length = (data[4] << 8) | data[5]
    payload = data[6:6 + length]
    return command, payload


def parse_mood_state(payload: list[int]) -> dict:
    """Parse a 0x6A/0x72 mood-light reply.

    Layout: [light_array_index, current_light_index, <group>*] where each
    group is 6 bytes: [index, R, G, B, Brightness, Speed]. Verified against
    the vendor parser (TempoHub interface/client/ec_parse.py: mood_lighting_parse)
    and live hardware on 2026-10-18 (PID 0xA160).
    """
    data = [int(x) & 0xFF for x in payload]
    groups = []
    for offset in range(2, len(data) - 5, 6):
        chunk = data[offset:offset + 6]
        groups.append({
            "index": chunk[0],
            "R": chunk[1],
            "G": chunk[2],
            "B": chunk[3],
            "Brightness": chunk[4],
            "Speed": chunk[5],
        })
    return {
        "light_array_index": data[0] if data else None,
        "current_light_index": data[1] if len(data) > 1 else None,
        "on": next((g["B"] == 1 for g in groups if g["index"] == MOOD_INDEX_SWITCH),
                   None),
        "groups": groups,
    }


# --- Mono bitmap encoding --------------------------------------------------------


def encode_mono_bitmap(
    pixels: bytes, width: int = MONO_WIDTH, height: int = MONO_HEIGHT,
    threshold: int = 32,
) -> bytes:
    """Encode an RGB888 frame as the device's 256x32 mono 0x17 file.

    Layout (verified 2026-10-17): two units (rows 0-15, 16-31), each a
    20-byte header plus four 128x8 pages (page 0=left/top, 1=right/top,
    2=left/bottom, 3=right/bottom). Byte i of a page is column i; bit k
    (LSB first) is row k of that page. The bottom page row of each unit is
    pre-shifted right by PAGE_ROW_PRESHIFT to cancel the panel's 2px wiring
    offset.
    """
    if width != MONO_WIDTH or height != MONO_HEIGHT:
        raise ValueError(
            f"frame must be {MONO_WIDTH}x{MONO_HEIGHT}, got {width}x{height}")
    if len(pixels) < width * height * 3:
        raise ValueError("pixels shorter than width*height*3")

    lit = bytearray(width * height)
    for index in range(width * height):
        base = index * 3
        if max(pixels[base], pixels[base + 1], pixels[base + 2]) > threshold:
            lit[index] = 1

    out = bytearray()
    for unit in range(2):
        ybase = unit * MONO_UNIT_ROWS
        out += bytes([0x02, 0x00, (unit * 4) & 0xFF, 0xFF, 0xFF] + [0] * 15)
        for page in range(4):
            xbase = 0 if page in (0, 2) else MONO_PAGE_COLS
            page_y = ybase + (0 if page in (0, 1) else 8)
            source_offset = GLOBAL_X_SHIFT - PAGE_ROW_PRESHIFT[page]
            for x in range(MONO_PAGE_COLS):
                byte = 0
                source_x = x + source_offset
                if 0 <= source_x < MONO_PAGE_COLS:
                    row_base = page_y * width + xbase + source_x
                    for bit in range(8):
                        if lit[row_base + bit * width]:
                            byte |= 1 << bit
                out.append(byte)
    return bytes(out)

