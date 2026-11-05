"""Golden packet tests for the PixelBar HID protocol.

Each test verifies the exact byte sequence of a 64-byte HID report.
Golden values derived from PROTOCOL_NOTES.md (halo-pixelbar-mcp) and the
working implementation (claude-edifier-pixelbar-usage), both community-validated
against real hardware (VID 0x2D99 / PID 0xA106).
"""

import pytest

from render.sinks.pixelbar import protocol as px


class TestBuildReport:
    """Frame construction: header, length, CRC, padding."""

    def test_frame_is_always_64_bytes(self) -> None:
        assert len(px.build_report(0xE8, [0x01, 0x03, 0xE4, 0xB1])) == 64

    def test_header_magic(self) -> None:
        frame = px.build_report(0xE8, [0x00])
        assert frame[0] == 0x2E  # COMMON_OUT
        assert frame[1] == 0xAA  # HEADER
        assert frame[2] == 0xEC  # DEVICE_TYPE (working code value)

    def test_command_and_length(self) -> None:
        payload = [0x20, 0x01, 0x66, 0xAF, 0xFF, 0x64, 0x08]
        frame = px.build_report(0x6B, payload)
        assert frame[3] == 0x6B  # command
        assert frame[4] == 0x00  # length high
        assert frame[5] == len(payload)  # length low

    def test_crc_is_sum_from_aa(self) -> None:
        frame = px.build_report(0xE8, [0x00, 0x03, 0xE4, 0xB1])
        crc_index = 6 + 4  # after payload
        expected_crc = sum(frame[1:crc_index]) & 0xFF
        assert frame[crc_index] == expected_crc

    def test_padding_is_zero(self) -> None:
        frame = px.build_report(0xE8, [0x00])
        assert all(b == 0 for b in frame[8:])  # after header+payload+crc

    def test_empty_payload(self) -> None:
        frame = px.build_report(0x66, [])
        assert frame[4] == 0x00
        assert frame[5] == 0x00
        assert frame[6] == sum(frame[1:6]) & 0xFF  # crc immediately after

    def test_payload_too_large_raises(self) -> None:
        with pytest.raises(ValueError, match="too large"):
            px.build_report(0xE8, [0x00] * 58)


class TestGoldenText:
    """Text set command (0xE8) golden bytes."""

    def test_set_text_hi(self) -> None:
        """[0, 3, 0xE4, 0xBD, 0xA0] = '你' in UTF-8 (3 bytes)."""
        payload = px.payload_text_set("你")
        assert payload == [0x00, 0x03, 0xE4, 0xBD, 0xA0]

        frame = px.build_report(0xE8, payload)
        assert frame[:7] == [0x2E, 0xAA, 0xEC, 0xE8, 0x00, 0x05, 0x00]
        assert frame[7:11] == [0x03, 0xE4, 0xBD, 0xA0]
        # CRC = sum(frame[1:11]) & 0xFF
        assert frame[11] == sum(frame[1:11]) & 0xFF

    def test_text_limits(self) -> None:
        with pytest.raises(ValueError, match="too long"):
            px.payload_text_set("a" * 17)  # 17 chars > 16
        with pytest.raises(ValueError, match="too long"):
            px.payload_text_set("你" * 17)  # 51 bytes > 48


class TestGoldenMoodLight:
    """Mood light (0x6A/0x6B/0x72) golden bytes (vendor RE, 2026-10-18).

    Payload = [light_array_index, light_index, R, G, B, brightness, speed].
    PID 0xA160 answers light_array_index 0x20; 0x13 (community notes) is
    silently ignored by the hardware.
    """

    def test_set_blue_static(self) -> None:
        """20 01 66 AF FF 64 FF = element 1, #66AFFF, brightness 100, keep speed."""
        payload = px.payload_mood_set(0x20, px.MOOD_INDEX_MAIN, 0x66, 0xAF, 0xFF)
        assert payload == [0x20, 0x01, 0x66, 0xAF, 0xFF, 0x64, 0xFF]

        frame = px.build_report(px.CMD_MOOD_SET, payload)
        assert frame[3] == 0x6B
        assert frame[4] == 0x00
        assert frame[5] == 0x07
        assert frame[6:13] == payload
        assert frame[13] == sum(frame[1:13]) & 0xFF

    def test_vendor_app_effect_payloads(self) -> None:
        """Reproduce the exact 0x6B payloads TempoHub 1.3.1 sent (USBPcap)."""
        for effect, vendor in px.MOOD_VENDOR_PAYLOADS.items():
            zone, code = px.MOOD_EFFECTS[effect]
            assert vendor[1] == zone, effect
            if code is None:
                color = tuple(vendor[2:5])
                payload = px.payload_mood_effect(
                    0x20, effect, brightness=vendor[5], color=color, speed=vendor[6])
            else:
                assert vendor[2:5] == [code, 0x00, 0x00], effect
                payload = px.payload_mood_effect(
                    0x20, effect, brightness=vendor[5], speed=vendor[6])
            assert payload == vendor, effect

    def test_vendor_app_frames_have_matching_crc(self) -> None:
        """Our frames are byte-identical to the captured TempoHub frames."""
        # [payload, expected frame byte 13 (CRC)]
        captured = [
            (px.MOOD_VENDOR_PAYLOADS["breathing"], 0xA8),
            (px.MOOD_VENDOR_PAYLOADS["tide"], 0x97),
            (px.MOOD_VENDOR_PAYLOADS["solid"], 0x8F),
            (px.MOOD_VENDOR_PAYLOADS["ripple"], 0x8F),
            (px.MOOD_VENDOR_PAYLOADS["flow"], 0x91),
            (px.MOOD_VENDOR_PAYLOADS["shadow"], 0x91),
        ]
        for payload, crc in captured:
            frame = px.build_report(px.CMD_MOOD_SET, payload)
            assert frame[:7] == [0x2E, 0xAA, 0xEC, 0x6B, 0x00, 0x07, 0x20]
            assert frame[7:14] == payload[1:] + [crc]
            assert len(frame) == px.REPORT_LEN

    def test_effect_rejects_unknown_name(self) -> None:
        with pytest.raises(ValueError, match="unknown mood effect"):
            px.payload_mood_effect(0x20, "nope")

    def test_solid_colour_matches_vendor_capture(self) -> None:
        """Zone 3 (纯色静光) takes real RGB; code 1 = select with stored colour."""
        assert px.payload_mood_effect(
            0x20, "solid", brightness=100, color=(255, 0x2F, 0x15)
        ) == px.MOOD_VENDOR_COLOR_PAYLOADS["orange"]
        assert px.payload_mood_effect(
            0x20, "solid", brightness=100, color=(0, 0xFF, 0x74)
        ) == px.MOOD_VENDOR_COLOR_PAYLOADS["green"]
        assert px.payload_mood_effect(
            0x20, "solid", brightness=10, color=(0, 0xFF, 0x74)
        ) == px.MOOD_VENDOR_COLOR_PAYLOADS["green_low"]

        # Same frame layout as the capture, CRC included (0xD1 / 0x01 / 0xA7).
        for key, crc in (("orange", 0xD1), ("green", 0x01), ("green_low", 0xA7)):
            payload = px.MOOD_VENDOR_COLOR_PAYLOADS[key]
            frame = px.build_report(px.CMD_MOOD_SET, payload)
            assert frame[7:14] == payload[1:] + [crc], key

    def test_colour_only_on_colour_effects(self) -> None:
        with pytest.raises(ValueError, match="does not take a colour"):
            px.payload_mood_effect(0x20, "flow", color=(255, 0, 0))
        with pytest.raises(ValueError, match="needs a colour"):
            px.payload_mood_effect(0x20, "breathing")

    def test_breathing_is_colour_driven(self) -> None:
        """Zone 1 carries the colour itself (no effect code)."""
        assert px.MOOD_EFFECTS["breathing"] == (px.MOOD_ZONE_BREATHING, None)
        payload = px.payload_mood_effect(
            0x20, "breathing", brightness=100, color=(28, 255, 248),
            speed=px.MOOD_BREATH_SPEED)
        assert payload == px.MOOD_VENDOR_PAYLOADS["breathing"]

    def test_set_with_mode_byte(self) -> None:
        assert px.payload_mood_set(0x20, 1, 0, 0, 0, 100, 8, mode=2) == [
            0x20, 0x01, 0x00, 0x00, 0x00, 0x64, 0x08, 0x02]

    def test_set_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            px.payload_mood_set(0x20, 1, 300, 0, 0)

    def test_switch_uses_index7_and_keep(self) -> None:
        """20 07 00 00 01 FF FF = master switch on (B carries the flag)."""
        assert px.payload_mood_switch(0x20, True) == [0x20, 0x07, 0x00, 0x00, 0x01, 0xFF, 0xFF]
        assert px.payload_mood_switch(0x20, False) == [0x20, 0x07, 0x00, 0x00, 0x00, 0xFF, 0xFF]

    def test_reset(self) -> None:
        """72 20 01 = reset light array 0x20, element 1."""
        payload = px.payload_mood_reset(0x20, 1)
        assert payload == [0x20, 0x01]
        frame = px.build_report(px.CMD_MOOD_RESET, payload)
        assert frame[3] == 0x72
        assert frame[5] == 0x02
        assert frame[6:8] == payload

    def test_write_repeat_is_documented(self) -> None:
        assert px.MOOD_WRITE_REPEAT == 3
        assert px.MOOD_WRITE_DELAY == 0.03

    def test_parse_mood_state(self) -> None:
        """Live 0x6A reply from PID 0xA160 (2026-10-18)."""
        raw = [0x20, 0x01,
               0x01, 0xFF, 0x00, 0x00, 0x64, 0x08,
               0x02, 0x01, 0x00, 0x00, 0x64, 0x08,
               0x03, 0xFF, 0xFF, 0xFF, 0x64, 0xFF,
               0x07, 0x00, 0x00, 0x01, 0xFF, 0xFF]
        state = px.parse_mood_state(raw)
        assert state["light_array_index"] == 0x20
        assert state["current_light_index"] == 1
        assert state["on"] is True
        assert state["groups"][0] == {"index": 1, "R": 255, "G": 0, "B": 0,
                                      "Brightness": 100, "Speed": 8}
        assert len(state["groups"]) == 4

    def test_parse_mood_state_switch_off(self) -> None:
        state = px.parse_mood_state([0x20, 0x07, 0x07, 0x00, 0x00, 0x00, 0xFF, 0xFF])
        assert state["on"] is False
        assert state["current_light_index"] == 7

    def test_parse_mood_state_short_reply(self) -> None:
        state = px.parse_mood_state([])
        assert state["light_array_index"] is None
        assert state["groups"] == []


class TestGoldenPixel:
    """Pixel screen commands (0xEF) golden bytes."""

    def test_theme_color_blue(self) -> None:
        """03 66 AF FF 00 00 FF FF FF."""
        payload = px.payload_pixel_theme_color(0x66, 0xAF, 0xFF)
        assert payload == [0x03, 0x66, 0xAF, 0xFF, 0x00, 0x00, 0xFF, 0xFF, 0xFF]

    def test_static_text_center(self) -> None:
        """01 66 AF FF 00 02 00 01 FF (alignment=1 center)."""
        payload = px.payload_text_static(0x66, 0xAF, 0xFF, 1)
        assert payload == [0x01, 0x66, 0xAF, 0xFF, 0x00, 0x02, 0x00, 0x01, 0xFF]

    def test_dynamic_text(self) -> None:
        """01 66 AF FF 00 02 01 01 FF (scroll right-to-left)."""
        payload = px.payload_text_dynamic(0x66, 0xAF, 0xFF)
        assert payload == [0x01, 0x66, 0xAF, 0xFF, 0x00, 0x02, 0x01, 0x01, 0xFF]

    def test_scene_cyber(self) -> None:
        """02 66 AF FF 00 01 07 00 FF (cyber category=7, template=0)."""
        payload = px.payload_scene(0x66, 0xAF, 0xFF, px.SCENE_CYBER, 0)
        assert payload == [0x02, 0x66, 0xAF, 0xFF, 0x00, 0x01, 0x07, 0x00, 0xFF]

    def test_clock_blue(self) -> None:
        """01 66 AF FF 00 01 00 00 FF."""
        payload = px.payload_clock(0x66, 0xAF, 0xFF)
        assert payload == [0x01, 0x66, 0xAF, 0xFF, 0x00, 0x01, 0x00, 0x00, 0xFF]


class TestGoldenVolume:
    """Volume set (0x67) golden bytes."""

    def test_set_volume_8(self) -> None:
        assert px.payload_volume_set(8) == [0x08]
        frame = px.build_report(0x67, [0x08])
        assert frame[3] == 0x67
        assert frame[6] == 0x08

    def test_volume_range(self) -> None:
        with pytest.raises(ValueError):
            px.payload_volume_set(17)
        with pytest.raises(ValueError):
            px.payload_volume_set(-1)


class TestGoldenFilePush:
    """File push (0x17) golden bytes for bitmap frames."""

    def test_start_payload(self) -> None:
        """mode=0, size=8192; state 0 = host announces device receive."""
        payload = px.payload_file_start(0, 8192)
        assert payload == [0x00, 0x00, 0x00, 0x00, 0x20, 0x00]

    def test_data_payload(self) -> None:
        chunk = b"\x01\x02\x03"
        payload = px.payload_file_data(0, 5, chunk)
        assert payload == [0x00, 0x02, 0x00, 0x05, 0x01, 0x02, 0x03]

    def test_end_payload(self) -> None:
        assert px.payload_file_end(0) == [0x00, 0x04]

    def test_full_push_sequence_shape(self) -> None:
        """Start + N data + end, all wrapped in 0x17 reports."""
        start = px.build_report(0x17, px.payload_file_start(0, 100))
        data = px.build_report(0x17, px.payload_file_data(0, 0, b"\xAA" * 50))
        end = px.build_report(0x17, px.payload_file_end(0))
        for frame in [start, data, end]:
            assert len(frame) == 64
            assert frame[0:3] == [0x2E, 0xAA, 0xEC]
            assert frame[3] == 0x17


class TestKnownValues:
    """Cross-validated values from PROTOCOL_NOTES.md."""

    def test_device_constants(self) -> None:
        assert px.VID == 0x2D99
        assert px.PID == 0xA106
        assert px.HID_INTERFACE == 4
        assert px.USAGE_PAGE == 0xFF14

    def test_report_length(self) -> None:
        assert px.REPORT_LEN == 64
        assert px.MAX_PAYLOAD == 57

    def test_mood_write_needs_triple_write(self) -> None:
        """PROTOCOL_NOTES: 'A lighting set must be transmitted three times with
        about 30 ms between writes.' Enforced by the sink (MOOD_WRITE_REPEAT)."""
        assert px.MOOD_WRITE_REPEAT == 3
        assert px.MOOD_WRITE_DELAY == 0.03
