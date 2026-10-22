"""Shared credential-field policy for uploads and durable job snapshots.

The policy classifies field names and explicit structured HTTP header records,
not arbitrary free text. Upload evidence keeps a visible placeholder;
persistence removes the same fields entirely. Business identifiers such as
request_id, idempotency_key, and model_identity remain data.
"""
from __future__ import annotations

import re


_SENSITIVE_NAMES = frozenset({
    "apikey", "key", "authorization", "proxyauthorization", "password", "passwd",
    "accesstoken", "refreshtoken", "apptoken", "authtoken", "token", "secret",
    "clientsecret", "modelconfig",
    "cookie", "setcookie", "privatekey", "credential", "credentials",
})
_SENSITIVE_SUFFIXES = ("apikey", "password", "accesstoken", "refreshtoken", "clientsecret")
_HEADER_CONTEXTS = frozenset({
    "headers", "requestheaders", "responseheaders", "httpheaders",
    "httprequestheaders", "httpresponseheaders", "inboundheaders", "outboundheaders",
})


def _normalized_name(value):
    return re.sub(r"[^a-z0-9]", "", value.lower()) if isinstance(value, str) else ""


def is_sensitive_key(key):
    """Recognize credential names regardless of ASCII case or punctuation.

    Match explicit names and the established provider-prefixed credential
    suffixes. Do not treat every name ending in ``key``, ``id``, or ``token`` as
    secret: those can be ordinary business fields.
    """
    if not isinstance(key, str):
        return False
    name = _normalized_name(key)
    return name in _SENSITIVE_NAMES or name.endswith(_SENSITIVE_SUFFIXES)


def sensitive_fields(mapping, context=None):
    """Return credential-bearing keys at one dictionary boundary.

    Direct sensitive keys always follow :func:`is_sensitive_key`. Within an
    explicitly named HTTP headers dictionary/list, also recognize records such
    as ``{"name": "Authorization", "value": "..."}`` and
    ``{"key": "Cookie", "values": ["..."]}``. The caller passes the nearest
    dictionary key as context, preserving it through lists. Ordinary business
    ``name/value`` records outside that context remain untouched.
    """
    fields = {key for key in mapping if is_sensitive_key(key)}
    if _normalized_name(context) not in _HEADER_CONTEXTS:
        return fields
    credential_record = any(
        _normalized_name(key) in ("name", "key") and is_sensitive_key(value)
        for key, value in mapping.items()
    )
    if credential_record:
        fields.update(key for key in mapping if _normalized_name(key) in ("value", "values"))
    return fields
