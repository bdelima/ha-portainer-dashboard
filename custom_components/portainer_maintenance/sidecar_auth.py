"""How the sidebar panel signs in to the sidecar webapp.

A Home Assistant iframe panel can pass nothing but a URL, so the panel URL
carries a token the sidecar swaps for a session cookie. The token is
HMAC-SHA256(key=password, msg="portainer-sidecar-panel:" + username) in hex.
It is not the password and cannot be turned back into it, but anyone holding
it can sign in to the sidecar until the password changes.

The sidecar computes the identical value (``panel_token`` in its main.py).
The two must stay byte-for-byte the same.
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .const import (
    CONF_SIDECAR_ANONYMOUS,
    CONF_SIDECAR_PASSWORD,
    CONF_SIDECAR_USERNAME,
    DEFAULT_SIDECAR_ANONYMOUS,
)


def panel_token(username: str, password: str) -> str:
    return hmac.new(
        password.encode("utf-8"),
        b"portainer-sidecar-panel:" + username.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def panel_url(webapp_url: str, data: Mapping[str, Any]) -> str:
    """The URL the sidebar panel loads.

    Unchanged when the entry is set to anonymous, or has no credentials (an
    entry created before these options existed): the sidecar then shows its
    own sign-in page. Otherwise ``auth=<token>`` is added to the query,
    replacing any ``auth`` already there; the fragment is left alone.
    """
    if data.get(CONF_SIDECAR_ANONYMOUS, DEFAULT_SIDECAR_ANONYMOUS):
        return webapp_url
    username = data.get(CONF_SIDECAR_USERNAME) or ""
    password = data.get(CONF_SIDECAR_PASSWORD) or ""
    if not username or not password:
        return webapp_url
    parts = urlsplit(webapp_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "auth"]
    query.append(("auth", panel_token(username, password)))
    return urlunsplit(parts._replace(query=urlencode(query)))
