"""Validation for the short-lived media-session token issued by the web service."""

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping


_TOKEN_PART = re.compile(r"^[A-Za-z0-9_-]+$")
_BOUNDED_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_CLAIM_KEYS = frozenset({"v", "sub", "controller", "path", "actions", "iat", "exp", "nonce"})
_HEADER_KEYS = frozenset({"alg", "typ"})
_MAX_SESSION_SECONDS = 60


class TokenValidationError(ValueError):
    """Raised for every invalid media token without distinguishing its cause."""


def validate_media_token(token: str, secret: str, *, now: int,
                         controller: str = "primary") -> dict:
    """Return validated claims, or raise TokenValidationError for an invalid JWT."""
    if not isinstance(token, str) or not isinstance(secret, str) or not secret:
        raise TokenValidationError("invalid token")
    if not isinstance(now, int) or isinstance(now, bool):
        raise TokenValidationError("invalid token")

    parts = token.split(".")
    if len(parts) != 3 or any(not _TOKEN_PART.fullmatch(part) for part in parts):
        raise TokenValidationError("invalid token")
    signed = f"{parts[0]}.{parts[1]}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    supplied = _decode_base64url(parts[2])
    if not hmac.compare_digest(supplied, expected):
        raise TokenValidationError("invalid token")

    header = _decode_json_part(parts[0])
    claims = _decode_json_part(parts[1])
    if not isinstance(header, Mapping) or set(header) != _HEADER_KEYS:
        raise TokenValidationError("invalid token")
    if header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise TokenValidationError("invalid token")
    if not isinstance(claims, Mapping) or set(claims) != _CLAIM_KEYS:
        raise TokenValidationError("invalid token")

    _validate_claims(claims, now=now, controller=controller)
    return dict(claims)


def _validate_claims(claims: Mapping, *, now: int, controller: str) -> None:
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    if claims.get("v") != 1 or isinstance(claims.get("v"), bool):
        raise TokenValidationError("invalid token")
    if not _is_bounded_id(claims.get("sub")) or not _is_bounded_id(claims.get("nonce")):
        raise TokenValidationError("invalid token")
    if claims.get("controller") != controller or claims.get("path") != "gate":
        raise TokenValidationError("invalid token")
    if claims.get("actions") != ["read"]:
        raise TokenValidationError("invalid token")
    if not _is_int(issued_at) or not _is_int(expires_at):
        raise TokenValidationError("invalid token")
    if issued_at > now or expires_at <= now or expires_at <= issued_at:
        raise TokenValidationError("invalid token")
    if expires_at - issued_at > _MAX_SESSION_SECONDS:
        raise TokenValidationError("invalid token")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_bounded_id(value: object) -> bool:
    return isinstance(value, str) and bool(_BOUNDED_ID.fullmatch(value))


def _decode_json_part(value: str):
    try:
        return json.loads(_decode_base64url(value).decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise TokenValidationError("invalid token") from error


def _decode_base64url(value: str) -> bytes:
    if not _TOKEN_PART.fullmatch(value) or len(value) % 4 == 1:
        raise TokenValidationError("invalid token")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as error:
        raise TokenValidationError("invalid token") from error
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(value, canonical):
        raise TokenValidationError("invalid token")
    return decoded


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
