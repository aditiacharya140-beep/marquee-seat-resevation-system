"""Equal requests must fingerprint equally, and different ones differently (REQ-025)."""

from __future__ import annotations

from uuid import UUID

from app.services.reservation_service import _fingerprint
from app.utils.canonical_json import canonical_json, fingerprint

SHOW = UUID("11111111-1111-1111-1111-111111111111")
OTHER_SHOW = UUID("22222222-2222-2222-2222-222222222222")


def test_key_order_and_whitespace_do_not_matter() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": [1, 2], "b": 1})
    assert canonical_json({"a": 1}) == '{"a":1}'


def test_a_different_value_is_a_different_fingerprint() -> None:
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    assert len(fingerprint({"a": 1})) == 64


def test_the_reserve_fingerprint_separates_show_seats_and_ttl() -> None:
    base = _fingerprint(SHOW, ["A1", "A2"], None)

    assert _fingerprint(SHOW, ["A1", "A2"], None) == base
    assert _fingerprint(OTHER_SHOW, ["A1", "A2"], None) != base
    assert _fingerprint(SHOW, ["A1"], None) != base
    # "Confirm now" and "hold for 120s" are different operations.
    assert _fingerprint(SHOW, ["A1", "A2"], 120) != base
