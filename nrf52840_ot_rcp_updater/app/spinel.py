"""Minimal, bounded Spinel-over-HDLC client for a dedicated OpenThread RCP."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from time import monotonic, sleep

try:
    import serial
except ImportError:  # pragma: no cover - the container provides pyserial.
    serial = None

from .models import NcpVersion

_HDLC_FLAG = 0x7E
_HDLC_ESCAPE = 0x7D
_HDLC_ESCAPE_XOR = 0x20
_HDLC_ESCAPED = {_HDLC_FLAG, _HDLC_ESCAPE, 0x11, 0x13}
_HDLC_GOOD_FCS = 0xF0B8
_HDLC_INITIAL_FCS = 0xFFFF
_MAX_FRAME_BYTES = 4096

_SPINEL_HEADER_FLAG = 0x80
_SPINEL_CMD_RESET = 1
_SPINEL_CMD_PROP_VALUE_GET = 2
_SPINEL_CMD_PROP_VALUE_IS = 6
_SPINEL_PROP_NCP_VERSION = 2
_SPINEL_RESET_BOOTLOADER = 3

_TOKEN_VALUE = r"(?P<{}>[A-Za-z0-9._+-]+)"
_NCP_FIELDS = {
    "hardware": (
        # Current builds place the board directly after the conventional SoC.
        re.compile(
            r"(?:^|[\s;])NRF52840\s+"
            + _TOKEN_VALUE.format("hardware")
            + r"(?:$|[\s;])"
        ),
        # Retain parsing of project firmware built before the compact format.
        re.compile(
            r"(?:^|[\s;])HW/" + _TOKEN_VALUE.format("hardware") + r"(?:$|[\s;])"
        ),
    ),
    "ncs_version": (
        re.compile(
            r"(?:^|[\s;])(?:NCS|N)/"
            + _TOKEN_VALUE.format("ncs_version")
            + r"(?:$|[\s;])"
        ),
    ),
    "zephyr_version": (
        re.compile(
            r"(?:^|[\s;])(?:ZEPHYR|Z)/"
            + _TOKEN_VALUE.format("zephyr_version")
            + r"(?:$|[\s;])"
        ),
    ),
}


class SpinelError(RuntimeError):
    """The serial RCP did not complete a valid standard Spinel exchange."""


def _fcs(data: bytes) -> int:
    fcs = _HDLC_INITIAL_FCS
    for byte in data:
        fcs ^= byte
        for _ in range(8):
            fcs = (fcs >> 1) ^ (0x8408 if fcs & 1 else 0)
    return fcs & 0xFFFF


def hdlc_encode(payload: bytes) -> bytes:
    """Encode a bounded Spinel payload as an OpenThread HDLC frame."""

    if not payload or len(payload) > _MAX_FRAME_BYTES:
        raise SpinelError("invalid Spinel payload length")
    fcs = _fcs(payload) ^ 0xFFFF
    framed = payload + fcs.to_bytes(2, "little")
    encoded = bytearray([_HDLC_FLAG])
    for byte in framed:
        if byte in _HDLC_ESCAPED:
            encoded.extend((_HDLC_ESCAPE, byte ^ _HDLC_ESCAPE_XOR))
        else:
            encoded.append(byte)
    encoded.append(_HDLC_FLAG)
    return bytes(encoded)


class HdlcDecoder:
    """Incrementally decodes valid HDLC frames and discards malformed input."""

    def __init__(self) -> None:
        self._frame = bytearray()
        self._in_frame = False
        self._escaped = False

    def feed(self, data: bytes) -> Iterator[bytes]:
        for byte in data:
            if byte == _HDLC_FLAG:
                if self._in_frame and len(self._frame) >= 3 and _fcs(bytes(self._frame)) == _HDLC_GOOD_FCS:
                    yield bytes(self._frame[:-2])
                self._frame.clear()
                self._in_frame = True
                self._escaped = False
                continue
            if not self._in_frame:
                continue
            if byte == _HDLC_ESCAPE:
                self._escaped = True
                continue
            if self._escaped:
                byte ^= _HDLC_ESCAPE_XOR
                self._escaped = False
            if len(self._frame) >= _MAX_FRAME_BYTES + 2:
                self._frame.clear()
                self._in_frame = False
                self._escaped = False
                continue
            self._frame.append(byte)


def pack_uint(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise SpinelError("Spinel packed integer is out of range")
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        encoded.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(encoded)


def unpack_uint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(5):
        if offset >= len(data):
            raise SpinelError("truncated Spinel packed integer")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << (index * 7)
        if not byte & 0x80:
            return value, offset
    raise SpinelError("Spinel packed integer is too large")


def parse_ncp_version(raw: str) -> NcpVersion:
    """Parse our additive tags from the OpenThread platform-info field.

    New RCPs lead this field with the conventional bare ``NRF52840`` token;
    older project firmware did not. Restrict parsing to that field so both
    forms work without treating package or build-date text as updater tags.
    """

    if not raw or "\x00" in raw or len(raw) > 512:
        raise SpinelError("NCP version string is invalid")
    _prefix, separator, remainder = raw.partition(";")
    platform_info = remainder.partition(";")[0] if separator else raw
    values: dict[str, str | None] = {}
    for field, patterns in _NCP_FIELDS.items():
        match = next((match for pattern in patterns if (match := pattern.search(platform_info))), None)
        values[field] = match.group(field) if match else None
    return NcpVersion(raw=raw, **values)


@dataclass
class SpinelClient:
    """Talk to an RCP only after its OTBR owner has stopped."""

    device: str
    baudrate: int
    timeout: float = 2.0

    def get_ncp_version(self) -> NcpVersion:
        payload = self._request(
            command=_SPINEL_CMD_PROP_VALUE_GET,
            property_id=_SPINEL_PROP_NCP_VERSION,
            value=b"",
        )
        if not payload or payload[0] != (_SPINEL_HEADER_FLAG | 1):
            raise SpinelError("NCP response used an unexpected Spinel header")
        command, offset = unpack_uint(payload, 1)
        property_id, offset = unpack_uint(payload, offset)
        if command != _SPINEL_CMD_PROP_VALUE_IS or property_id != _SPINEL_PROP_NCP_VERSION:
            raise SpinelError("NCP did not return SPINEL_PROP_NCP_VERSION")
        terminator = payload.find(b"\0", offset)
        if terminator == -1:
            raise SpinelError("NCP version string is not NUL terminated")
        try:
            raw = payload[offset:terminator].decode("utf-8", "strict")
        except UnicodeDecodeError as err:
            raise SpinelError("NCP version is not valid UTF-8") from err
        return parse_ncp_version(raw)

    def reset_bootloader(self) -> None:
        """Request the standard Spinel reset type supported by the NCS RCP sample."""

        self._write_only(
            bytes([_SPINEL_HEADER_FLAG | 1])
            + pack_uint(_SPINEL_CMD_RESET)
            + bytes([_SPINEL_RESET_BOOTLOADER])
        )

    def wait_for_ncp_version(self, timeout: float) -> NcpVersion:
        deadline = monotonic() + timeout
        last_error: Exception | None = None
        while monotonic() < deadline:
            try:
                return self.get_ncp_version()
            except SpinelError as err:
                last_error = err
                sleep(1)
        raise SpinelError(f"RCP did not become ready: {last_error}")

    def _request(self, command: int, property_id: int, value: bytes) -> bytes:
        request = bytes([_SPINEL_HEADER_FLAG | 1]) + pack_uint(command) + pack_uint(property_id) + value
        with self._open_serial() as port:
            port.reset_input_buffer()
            port.write(hdlc_encode(request))
            port.flush()
            decoder = HdlcDecoder()
            deadline = monotonic() + self.timeout
            while monotonic() < deadline:
                chunk = port.read(256)
                for response in decoder.feed(chunk):
                    if response and response[0] == (_SPINEL_HEADER_FLAG | 1):
                        return response
        raise SpinelError("timed out waiting for a Spinel response")

    def _write_only(self, payload: bytes) -> None:
        with self._open_serial() as port:
            port.reset_input_buffer()
            port.write(hdlc_encode(payload))
            port.flush()

    def _open_serial(self):
        if serial is None:
            raise SpinelError("pyserial is unavailable in this image")
        try:
            return serial.Serial(
                self.device,
                self.baudrate,
                timeout=0.2,
                write_timeout=self.timeout,
                exclusive=True,
            )
        except (OSError, serial.SerialException) as err:
            raise SpinelError(f"cannot exclusively open RCP serial device {self.device}: {err}") from err
