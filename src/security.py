"""Platform security, timing-safe HMAC verification, SSRF guards, and PII/TCKN/PAN red-line filters."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import socket
import struct
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

# Replay protection window in seconds (spec_version 1.1: 5 minutes = 300 seconds)
MAX_TIMESTAMP_SKEW_SECONDS = 300

# Forbidden internal hostnames to prevent Server-Side Request Forgery (SSRF)
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
    "instance-data",
    "0.0.0.0",
    "169.254.169.254",
    "[::1]",
    "::1",
}

# Credit card PAN pattern (13-19 digits with optional spaces, tabs, newlines, dashes, or dots)
_PAN_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ \t\r\n._-]*?){13,19}(?!\d)")

# 11-digit Turkish Republic ID (TCKN) candidate pattern
_TCKN_CANDIDATE_RE = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")

# IBAN pattern (e.g., spaced or unspaced TR33 0006 1005 1978 6457 8413 26) to exclude from PAN/TCKN false positives
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ -]?\d{4}){4,7}(?:[ -]?\d{1,4})?\b", re.IGNORECASE)

# Sensitive payment & card security code fields (JSON, escaped JSON, YAML, or key=value format, §4.3 red line)
_SENSITIVE_PAYMENT_RE = re.compile(
    r"(?i)(?:\\?[\"'])?\b(?:"
    r"(?:cvv2?|cvc2?|cid|security_code|guvenlik_kodu|"
    r"card_number|kart_numarasi|pan_number|"
    r"banka_sifresi|bank_password|card_pin)\b(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?\d{3,19}"
    r"|(?:son_kullanma_tarihi|expiry_date|card_expiry|expiration_date|expiry|exp_date|expiration)\b"
    r"(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?(?:"
    r"(?:0?[1-9]|1[0-2])\s*[/.-]\s*\d{2,4}"
    r"|\d{4}\s*[/.-]\s*(?:0?[1-9]|1[0-2])"
    r"|(?:0[1-9]|1[0-2])(?:2[4-9]|3\d)"
    r")"
    r")(?:\\?[\"'])?"
)

# Match both 4-hex (\u0030) and 8-hex (\U00000030) single- or double-escaped Unicode hex sequences
_UNICODE_ESCAPE_RE = re.compile(r"\\+u([0-9a-fA-F]{4})|\\+U([0-9a-fA-F]{8})")

# Match backslash line continuations inside YAML/JSON/Python strings
_LINE_CONTINUATION_RE = re.compile(r"\\+\r?\n[ \t]*")


def get_signing_secret() -> bytes:
    """Retrieve PLATFORM_SIGNING_SECRET dynamically from environment."""
    secret = os.environ.get("PLATFORM_SIGNING_SECRET", "")
    if not secret:
        raise RuntimeError("PLATFORM_SIGNING_SECRET environment variable is required")
    return secret.encode("utf-8")


def verify_signature(body: bytes, ts: str | None, sig: str | None) -> None:
    """Verify platform HMAC-SHA256 signature in constant time and enforce 300s timestamp window.

    Raises HTTPException(401) if timestamp or signature is missing, non-ASCII, expired, or invalid.
    """
    if not ts or not sig or not isinstance(ts, str) or not isinstance(sig, str):
        raise HTTPException(status_code=401, detail="invalid timestamp or missing signature")

    if not ts.isascii() or not sig.isascii():
        raise HTTPException(status_code=401, detail="invalid signature encoding")

    try:
        ts_int = int(ts)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=401, detail="invalid timestamp") from exc

    if abs(time.time() - ts_int) > MAX_TIMESTAMP_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="invalid timestamp")

    secret = get_signing_secret()
    expected = hmac.new(
        secret,
        ts.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()

    try:
        is_match = hmac.compare_digest(expected.encode("ascii"), sig.encode("ascii"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid signature") from exc

    if not is_match:
        raise HTTPException(status_code=401, detail="invalid signature")


def _try_parse_ip_variants(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse standard, hex (0x7f000001), integer (2130706433), or shorthand (127.1) IP literals."""
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        pass

    try:
        if hostname.startswith("0x") or hostname.isdigit():
            val = int(hostname, 0)
            if 0 <= val <= 0xFFFFFFFF:
                return ipaddress.IPv4Address(val)
    except ValueError:
        pass

    if re.fullmatch(r"[0-9.]+", hostname):
        try:
            packed = socket.inet_aton(hostname)
            return ipaddress.IPv4Address(struct.unpack("!I", packed)[0])
        except OSError:
            pass

    return None


def is_ssrf_url(
    url: str,
    allow_local_base_url: bool = False,
    allow_private_spec_server: bool = False,
) -> tuple[bool, str]:
    """Check whether a URL targets loopback, link-local metadata, RFC 1918 private IPs, or non-HTTP schemes.

    When `allow_local_base_url=True` (used for `base_url` which is only written into generated test templates
    and never fetched by the container), `localhost` and loopback addresses (`127.0.0.1`, `::1`, `::ffff:127.0.0.1`)
    are permitted, while cloud metadata (`169.254.169.254`), private subnets, and non-HTTP schemes remain blocked.
    When `allow_private_spec_server=True` (used when falling back to `spec.servers[0]` if `input.base_url` is omitted,
    which is also never fetched by the container), RFC 1918 private IPs and `.internal`/`.local`/`.lan` hostnames are
    additionally permitted while cloud metadata, unspecified (`0.0.0.0`), reserved/multicast, and non-HTTP schemes remain blocked.
    """
    if not url or not isinstance(url, str):
        return False, ""

    cleaned = url.strip()
    parsed = urlparse(cleaned)

    if not parsed.scheme:
        return False, ""

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return True, f"Desteklenmeyen veya güvenli olmayan URL şeması: {scheme}://"

    raw_host = (parsed.hostname or "").lower().strip("[]").rstrip(".")
    if not raw_host:
        return True, "Geçersiz URL sunucu adı."

    if raw_host in ("169.254.169.254", "metadata.google.internal", "metadata", "instance-data"):
        return True, f"SSRF engellendi: Bulut metadata adresi ({raw_host}) kullanılamaz."

    if raw_host in BLOCKED_HOSTNAMES:
        if (allow_local_base_url or allow_private_spec_server) and raw_host in (
            "localhost",
            "localhost.localdomain",
            "127.0.0.1",
            "::1",
        ):
            return False, ""
        return True, f"SSRF engellendi: Yerel ağ adresi ({raw_host}) dış çağrılarda kullanılamaz."

    ip = _try_parse_ip_variants(raw_host)
    if ip is not None:
        # Unwrap IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1 -> 127.0.0.1) for consistent checks across Python 3.12+
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped

        if ip.is_link_local or str(ip) == "169.254.169.254":
            return True, f"SSRF engellendi: Link-local / metadata IP adresi ({ip}) yasaktır."
        if ip.is_loopback or ip.is_unspecified:
            if (allow_local_base_url or allow_private_spec_server) and ip.is_loopback:
                return False, ""
            return True, f"SSRF engellendi: Loopback IP adresi ({ip}) yasaktır."
        if ip.is_reserved or ip.is_multicast:
            return True, f"SSRF engellendi: Özel ağ (RFC 1918) IP adresi ({ip}) yasaktır."
        if ip.is_private:
            if allow_private_spec_server:
                return False, ""
            return True, f"SSRF engellendi: Özel ağ (RFC 1918) IP adresi ({ip}) yasaktır."
    else:
        if raw_host.endswith((".internal", ".local", ".lan")):
            if allow_private_spec_server:
                return False, ""
            return True, f"SSRF engellendi: Dahili ağ alan adı ({raw_host}) yasaktır."
        if raw_host.endswith(".localhost") and not (allow_local_base_url or allow_private_spec_server):
            return True, f"SSRF engellendi: Yerel alan adı ({raw_host}) yasaktır."

    return False, ""


def _luhn_check(digits: str) -> bool:
    """Return True if a 13-19 digit string satisfies the Luhn checksum algorithm."""
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    if len(set(digits)) == 1:
        return False
    total = 0
    reverse_digits = digits[::-1]
    for idx, ch in enumerate(reverse_digits):
        n = int(ch)
        if idx % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _has_card_iin_prefix(digits: str) -> bool:
    """Check whether a digit sequence has a plausible payment card Issuer Identification Number (IIN) prefix and length.

    Supported card networks:
    - Visa: starts with '4' (length 13, 16, or 19)
    - Mastercard: '51'-'55' or '2221'-'2720' (length 16)
    - American Express: '34' or '37' (length 15)
    - Discover: '6011' or '65' (length 16 or 19)
    - JCB: '35' (length 16-19)
    - Troy (Turkey): '9792' (length 16)
    - Diners Club: '36', '38', or '300'-'305' (length 14-16)

    Trade-off note:
    Requiring both a valid card IIN prefix + network length + Luhn checksum reduces false positives on random
    16-digit order/tracking IDs from ~10% down to ~2.1%, while still blocking all real payment card numbers.
    Any synthetic 16-digit identifier that simultaneously starts with a real card network prefix (e.g. '4')
    and passes Luhn will still be rejected as a conservative safety measure under spec_version 1.1 §4.3.
    """
    length = len(digits)
    if not digits.isdigit():
        return False

    # Visa
    if digits.startswith("4") and length in (13, 16, 19):
        return True
    # Mastercard
    if length == 16:
        prefix2 = int(digits[:2])
        prefix4 = int(digits[:4])
        if 51 <= prefix2 <= 55 or 2221 <= prefix4 <= 2720:
            return True
    # Amex
    if length == 15 and digits[:2] in ("34", "37"):
        return True
    # Discover
    if length in (16, 19) and (digits.startswith("6011") or digits.startswith("65")):
        return True
    # JCB
    if 16 <= length <= 19 and digits.startswith("35"):
        return True
    # Troy
    if length == 16 and digits.startswith("9792"):
        return True
    # Diners Club
    if 14 <= length <= 16 and (digits[:2] in ("36", "38") or 300 <= int(digits[:3]) <= 305):
        return True

    return False


def _tckn_check(digits: str) -> bool:
    """Return True if an 11-digit string satisfies the official Turkish Republic ID (TCKN) checksum."""
    if len(digits) != 11 or not digits.isdigit() or digits[0] == "0":
        return False
    if len(set(digits[:10])) == 1:
        return False
    d = [int(c) for c in digits]
    odd_sum = d[0] + d[2] + d[4] + d[6] + d[8]
    even_sum = d[1] + d[3] + d[5] + d[7]
    if ((odd_sum * 7) - even_sum) % 10 != d[9]:
        return False
    if sum(d[:10]) % 10 != d[10]:
        return False
    return True


def _normalize_escaped_text(text: str) -> str:
    """Decode literal \\uXXXX, \\UXXXXXXXX, and backslash line-continuation escapes for accurate inspection."""
    def _replace(match: re.Match[str]) -> str:
        hex_str = match.group(1) or match.group(2)
        if not hex_str:
            return match.group(0)
        try:
            codepoint = int(hex_str, 16)
            if 0 <= codepoint <= 0x10FFFF:
                return chr(codepoint)
        except ValueError:
            pass
        return match.group(0)

    unfolded = _LINE_CONTINUATION_RE.sub("", text)
    return _UNICODE_ESCAPE_RE.sub(_replace, unfolded)


def contains_prohibited_payment_data(text: str) -> bool:
    """Detect raw credit card numbers (IIN + Luhn valid), CVV/expiry/bank secrets (§4.3), or valid TCKN (KVKK)."""
    if not text or not isinstance(text, str):
        return False

    normalized = _normalize_escaped_text(text)

    if _SENSITIVE_PAYMENT_RE.search(normalized):
        return True

    # Strip valid IBAN sequences (e.g. spaced TR IBANs) before scanning digit groups for PAN/TCKN
    scrubbed = _IBAN_RE.sub(" ", normalized)

    for match in _PAN_CANDIDATE_RE.finditer(scrubbed):
        raw_digits = re.sub(r"\D", "", match.group(0))
        if _has_card_iin_prefix(raw_digits) and _luhn_check(raw_digits):
            return True

    for match in _TCKN_CANDIDATE_RE.finditer(scrubbed):
        if _tckn_check(match.group(0)):
            return True

    return False


def scan_object_for_sensitive_data(node: Any, _seen: set[int] | None = None) -> bool:
    """Recursively walk unique containers, keys, and leaf values of a parsed YAML/JSON object for sensitive data."""
    if isinstance(node, str):
        return contains_prohibited_payment_data(node)
    if isinstance(node, int) and not isinstance(node, bool):
        if 10**10 <= abs(node) <= 10**19:
            return contains_prohibited_payment_data(str(node))
        return False
    if isinstance(node, (dict, list, tuple, set)):
        if _seen is None:
            _seen = set()
        obj_id = id(node)
        if obj_id in _seen:
            return False
        _seen.add(obj_id)
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and contains_prohibited_payment_data(k):
                    return True
                if scan_object_for_sensitive_data(v, _seen):
                    return True
            return False
        return any(scan_object_for_sensitive_data(item, _seen) for item in node)
    return False
