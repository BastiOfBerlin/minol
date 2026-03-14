"""Tests for minol._utils: parse_forms() and parse_sap_ticket()."""

import base64
import struct
import unittest
from datetime import datetime

from minol._utils import parse_forms, parse_sap_ticket


def _build_ticket(portal_identity: bytes = b"PORTAL_ID",
                  timestamp: str = "202503141200",
                  valid_hours: int = 8) -> str:
    """Build a minimal valid MYSAPSSO2 ticket for testing."""
    # Header: version(1) + codepage(4 ASCII) + space(1)
    header = bytes([2]) + b"4100" + b" "
    # Field 0: 2-byte BE length + value (no ID byte)
    field0 = struct.pack(">H", len(portal_identity)) + portal_identity
    # Field 0x04: ID + 2-byte BE length + timestamp (12 ASCII chars)
    ts_bytes = timestamp.encode("ascii")
    field04 = bytes([0x04]) + struct.pack(">H", len(ts_bytes)) + ts_bytes
    # Field 0x05: ID + 2-byte BE length + 4-byte BE uint
    field05 = bytes([0x05]) + struct.pack(">H", 4) + struct.pack(">I", valid_hours)
    raw = header + field0 + field04 + field05
    return base64.b64encode(raw).decode()


class TestParseForms(unittest.TestCase):

    def test_single_form_action_and_inputs(self):
        html = '''<form action="/submit">
            <input name="user" value="alice"/>
            <input name="token" value="abc123"/>
        </form>'''
        forms = parse_forms(html)
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["action"], "/submit")
        self.assertEqual(forms[0]["fields"], {"user": "alice", "token": "abc123"})

    def test_multiple_forms(self):
        html = '''
        <form action="/first"><input name="a" value="1"/></form>
        <form action="/second"><input name="b" value="2"/></form>
        '''
        forms = parse_forms(html)
        self.assertEqual(len(forms), 2)
        self.assertEqual(forms[0]["action"], "/first")
        self.assertEqual(forms[1]["action"], "/second")

    def test_value_before_name_attribute_ordering(self):
        html = '<form action="/go"><input value="xyz" name="key"/></form>'
        forms = parse_forms(html)
        self.assertEqual(forms[0]["fields"], {"key": "xyz"})

    def test_html_escaped_values_unescaped(self):
        html = '<form action="/go?a=1&amp;b=2"><input name="msg" value="Hello &amp; World"/></form>'
        forms = parse_forms(html)
        self.assertEqual(forms[0]["action"], "/go?a=1&b=2")
        self.assertEqual(forms[0]["fields"]["msg"], "Hello & World")

    def test_form_with_no_inputs(self):
        html = '<form action="/empty"></form>'
        forms = parse_forms(html)
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["fields"], {})

    def test_input_missing_name_skipped(self):
        html = '<form action="/x"><input value="orphan"/></form>'
        forms = parse_forms(html)
        self.assertEqual(forms[0]["fields"], {})

    def test_input_missing_value_skipped(self):
        html = '<form action="/x"><input name="novalue"/></form>'
        forms = parse_forms(html)
        self.assertEqual(forms[0]["fields"], {})

    def test_no_forms_returns_empty_list(self):
        self.assertEqual(parse_forms("<html><body>no forms here</body></html>"), [])

    def test_saml_response_form(self):
        saml_val = "base64encodedsamlresponse=="
        html = (
            f'<form method="POST" action="https://sap.example.com/saml/acs">'
            f'<input type="hidden" name="SAMLResponse" value="{saml_val}"/>'
            f'<input name="RelayState" value="token123"/>'
            f'</form>'
        )
        forms = parse_forms(html)
        self.assertEqual(len(forms), 1)
        self.assertIn("SAMLResponse", forms[0]["fields"])
        self.assertEqual(forms[0]["fields"]["SAMLResponse"], saml_val)


class TestParseSapTicket(unittest.TestCase):

    def test_valid_ticket_returns_dict(self):
        ticket = _build_ticket(timestamp="202503141200", valid_hours=8)
        result = parse_sap_ticket(ticket)
        self.assertIsNotNone(result)
        self.assertEqual(result["created_at"], datetime(2025, 3, 14, 12, 0))
        self.assertEqual(result["valid_hours"], 8)

    def test_url_encoded_ticket_with_exclamation_for_plus(self):
        # Build a ticket that, when base64-encoded, would contain + signs
        # We replace + with ! to simulate SAP cookie encoding
        ticket = _build_ticket(timestamp="202503141200", valid_hours=8)
        encoded_with_bang = ticket.replace("+", "!")
        result = parse_sap_ticket(encoded_with_bang)
        self.assertIsNotNone(result)
        self.assertEqual(result["valid_hours"], 8)

    def test_invalid_base64_returns_none(self):
        result = parse_sap_ticket("!!!not-valid-base64!!!")
        self.assertIsNone(result)

    def test_too_short_data_returns_none(self):
        # < 8 bytes after decode
        short = base64.b64encode(b"tiny").decode()
        result = parse_sap_ticket(short)
        self.assertIsNone(result)

    def test_missing_timestamp_field_returns_none(self):
        # Build ticket with only field 0x05 (no 0x04)
        header = bytes([2]) + b"4100" + b" "
        field0 = struct.pack(">H", 5) + b"IDENT"
        field05 = bytes([0x05]) + struct.pack(">H", 4) + struct.pack(">I", 8)
        raw = header + field0 + field05
        ticket = base64.b64encode(raw).decode()
        result = parse_sap_ticket(ticket)
        self.assertIsNone(result)

    def test_missing_validity_field_returns_none(self):
        # Build ticket with only field 0x04 (no 0x05)
        header = bytes([2]) + b"4100" + b" "
        field0 = struct.pack(">H", 5) + b"IDENT"
        ts_bytes = b"202503141200"
        field04 = bytes([0x04]) + struct.pack(">H", len(ts_bytes)) + ts_bytes
        raw = header + field0 + field04
        ticket = base64.b64encode(raw).decode()
        result = parse_sap_ticket(ticket)
        self.assertIsNone(result)

    def test_ff_unit_stops_parsing(self):
        # 0xFF before 0x04/0x05 → both missing → None
        header = bytes([2]) + b"4100" + b" "
        field0 = struct.pack(">H", 5) + b"IDENT"
        field_ff = bytes([0xFF]) + struct.pack(">H", 0)
        raw = header + field0 + field_ff
        ticket = base64.b64encode(raw).decode()
        result = parse_sap_ticket(ticket)
        self.assertIsNone(result)

    def test_strips_and_repads_base64(self):
        # Ticket with padding stripped should still parse
        ticket = _build_ticket(timestamp="202501010900", valid_hours=24)
        stripped = ticket.rstrip("=")
        result = parse_sap_ticket(stripped)
        self.assertIsNotNone(result)
        self.assertEqual(result["valid_hours"], 24)


if __name__ == "__main__":
    unittest.main()
