"""Pure utility functions: HTML form parsing and SAP ticket parsing."""

import re
import base64
import struct
import logging
from html import unescape
from urllib.parse import unquote
from datetime import datetime

__all__ = ["parse_forms", "parse_sap_ticket"]

log = logging.getLogger(__name__)


def parse_forms(html: str) -> list[dict]:
    """
    Extract <form> elements with their action and hidden inputs.
    Returns a list of {"action": str, "fields": {name: value, ...}}.
    """
    forms = []
    for form_match in re.finditer(
        r'<form[^>]*action=["\']([^"\']*)["\'][^>]*>(.*?)</form>',
        html, re.DOTALL | re.IGNORECASE
    ):
        action = unescape(form_match.group(1))
        body = form_match.group(2)
        fields = {}
        for inp in re.finditer(r'<input\b[^>]*/?>', body, re.IGNORECASE):
            attrs = dict(re.findall(r'([a-zA-Z][\w-]*)=["\']([^"\']*)["\']',
                                    inp.group(0), re.IGNORECASE))
            if 'name' in attrs and 'value' in attrs:
                fields[unescape(attrs['name'])] = unescape(attrs['value'])
        forms.append({"action": action, "fields": fields})
    return forms


def parse_sap_ticket(mysapsso2: str) -> dict | None:
    """
    Parse a MYSAPSSO2 (SAP Logon Ticket v2) to extract creation time and
    validity period.

    Token binary layout:
        Header:  version (1 byte) + codepage (4 bytes ASCII) + space (1 byte)
        Field 0: 2-byte BE length + value (portal identity, no ID byte)
        Field N: ID (1 byte) + 2-byte BE length + value

    Relevant info unit IDs:
        0x04 = Creation timestamp (ASCII, "YYYYMMddHHmm", 12 chars)
        0x05 = Validity period in hours (4-byte big-endian unsigned int)
        0xFF = Signature (marks the end of info units)

    The cookie value may be URL-encoded (e.g. %3D for =).

    Returns a dict with "created_at" (datetime) and "valid_hours" (int),
    or None if the ticket could not be parsed.
    """
    try:
        decoded_value = unquote(mysapsso2)
        # SAP replaces + with ! in cookie values (+ is not cookie-safe per RFC 6265)
        decoded_value = decoded_value.replace("!", "+")
        # Strip any existing padding, then re-pad to a valid length
        stripped = decoded_value.rstrip("=")
        decoded_value = stripped + "=" * (-len(stripped) % 4)
        raw = base64.b64decode(decoded_value)
        log.debug(f"  SAP ticket: {len(raw)} bytes after base64 decode")
    except Exception as e:
        log.debug(f"  SAP ticket: base64 decode failed: {e}")
        return None

    if len(raw) < 8:
        log.debug(f"  SAP ticket: too short ({len(raw)} bytes)")
        return None

    # Header: version(1) + codepage(4) + space(1) = 6 bytes
    version = raw[0]
    codepage = raw[1:5]
    log.debug(f"  SAP ticket: version={version}, codepage={codepage!r}, "
              f"first 20 bytes={raw[:20].hex()}")
    pos = 6

    # First field has no ID byte: 2-byte BE length + value (portal identity)
    if pos + 2 > len(raw):
        log.debug("  SAP ticket: too short for first field length")
        return None
    first_len = struct.unpack(">H", raw[pos : pos + 2])[0]
    first_val = raw[pos + 2 : pos + 2 + first_len]
    log.debug(f"  SAP ticket: portal identity len={first_len}, "
              f"val={first_val.decode('ascii', errors='replace')!r}")
    pos += 2 + first_len

    # Remaining fields: ID(1) + length(2 BE) + value
    created_at = None
    valid_hours = None

    while pos < len(raw):
        if pos + 3 > len(raw):
            break
        unit_id = raw[pos]
        length = struct.unpack(">H", raw[pos + 1 : pos + 3])[0]
        pos += 3

        if pos + length > len(raw):
            log.debug(f"  SAP ticket: unit 0x{unit_id:02x} len={length} exceeds "
                      f"remaining {len(raw) - pos} bytes")
            break

        value = raw[pos : pos + length]
        pos += length

        try:
            val_repr = value.decode("ascii")
        except (ValueError, UnicodeDecodeError):
            hex_str = value.hex()
            val_repr = f"(hex) {hex_str[:40]}{'...' if len(hex_str) > 40 else ''}"
        log.debug(f"  SAP ticket: unit 0x{unit_id:02x} len={length} val={val_repr!r}")

        if unit_id == 0x04:
            # Creation timestamp: "YYYYMMddHHmm" (12 chars, no seconds)
            try:
                created_at = datetime.strptime(value.decode("ascii"), "%Y%m%d%H%M")
            except (ValueError, UnicodeDecodeError) as e:
                log.debug(f"  SAP ticket: failed to parse timestamp: {e}")
        elif unit_id == 0x05 and length == 4:
            # Validity period in hours (4-byte big-endian unsigned integer)
            valid_hours = struct.unpack(">I", value)[0]
        elif unit_id == 0xFF:
            break  # Signature, stop parsing

    log.debug(f"  SAP ticket: created_at={created_at}, valid_hours={valid_hours}")
    if created_at is not None and valid_hours is not None:
        return {"created_at": created_at, "valid_hours": valid_hours}
    return None
