"""Tests du masquage des secrets (fonction + filtre de log au niveau handler)."""

from __future__ import annotations

import logging

from sources import RedactingFilter, masquer_secrets


def test_masquer_query_string():
    url = "https://api.adzuna.com/v1/api/jobs/fr/search/1?app_id=ABC&app_key=SECRET123&what=stage"
    masque = masquer_secrets(url)
    assert "SECRET123" not in masque
    assert "ABC" not in masque
    assert "app_key=***" in masque
    assert "what=stage" in masque  # les paramètres non sensibles sont conservés


def test_masquer_bearer():
    masque = masquer_secrets("Authorization: Bearer eyJhbGciOi.PAYLOAD.sig")
    assert "eyJhbGciOi.PAYLOAD.sig" not in masque
    assert "Bearer ***" in masque


def test_redacting_filter_sur_message(caplog):
    filtre = RedactingFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="échec pour url=%s", args=("http://x?api_key=TOPSECRET",), exc_info=None,
    )
    assert filtre.filter(record) is True
    # Après filtrage, le message formaté ne contient plus le secret.
    assert "TOPSECRET" not in record.getMessage()
    assert "api_key=***" in record.getMessage()
