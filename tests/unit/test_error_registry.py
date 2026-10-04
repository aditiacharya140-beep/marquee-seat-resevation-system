"""The error-code registry (SEAT-003, left to the tester by mds/12-testing-and-burst.md).

The registry is the client contract in executable form, so the assertions are about
closure rather than about any single code: every code maps to exactly one status, no
code is declared twice, every 4xx logs at `info` and every 5xx at `error`, and the
documented contract and the registry do not drift apart.

On the contract comparison: `mds/12-testing-and-burst.md` specifies an **equality**
between the registry and `06-apis.md` ∪ the operational table in `08-error-logging.md`.
That equality is not yet satisfiable — `ROUTE_NOT_FOUND` and `METHOD_NOT_ALLOWED` land
with SEAT-066 — so what is asserted here is the two-sided relation that *is* true now
and that tightens into the equality when SEAT-066 lands:

    contract_codes - operational_codes  ⊆  registry  ⊆  contract_codes ∪ operational_codes

The right-hand containment is what stops a stray code being added unnoticed; a plain
subset assertion in one direction only would not.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path
from typing import Final

import pytest

from app.core.constants import LogLevel
from app.core.error_codes import REGISTRY, CodeSpec, ErrorCode
from app.core.errors import AppError

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
ERROR_CODES_SOURCE: Final = PROJECT_ROOT / "app" / "core" / "error_codes.py"
APIS_DOC: Final = PROJECT_ROOT / "mds" / "06-apis.md"
ERRORS_DOC: Final = PROJECT_ROOT / "mds" / "08-error-logging.md"

_CODE = r"[A-Z][A-Z0-9_]{3,}"


def _documented_contract_codes() -> dict[str, int]:
    """Status-and-code pairs as `06-apis.md` writes them: inline and in tables."""
    text = APIS_DOC.read_text()
    pairs: dict[str, int] = {}
    patterns = (
        rf"`(\d{{3}})\s+({_CODE})`",
        rf"^\|\s*(\d{{3}})\s*\|\s*`({_CODE})`",
        rf"^\|\s*(\d{{3}})\s*\|\s*`({_CODE})`\s*/\s*`({_CODE})`",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.MULTILINE):
            status = int(match.group(1))
            for code in match.groups()[1:]:
                if code:
                    pairs[code] = status
    return pairs


def _documented_operational_codes() -> dict[str, int]:
    """The operational table in `08-error-logging.md`: codes owned by no endpoint."""
    text = ERRORS_DOC.read_text()
    section = text.split("### Operational codes", 1)[1].split("\n---", 1)[0]
    return {
        match.group(1): int(match.group(2))
        for match in re.finditer(rf"^\|\s*`({_CODE})`\s*\|\s*(\d{{3}})\s*\|", section, re.M)
    }


CONTRACT_CODES: Final = _documented_contract_codes()
OPERATIONAL_CODES: Final = _documented_operational_codes()


def _source_tree() -> ast.Module:
    return ast.parse(ERROR_CODES_SOURCE.read_text())


# ---------------------------------------------------------------------------------
# The documents parsed as expected
# ---------------------------------------------------------------------------------


def test_the_contract_extraction_is_not_vacuous() -> None:
    """A regex that matched nothing would make every comparison below pass trivially."""
    assert len(CONTRACT_CODES) >= 15, sorted(CONTRACT_CODES)
    assert CONTRACT_CODES["SEAT_TAKEN"] == 409
    assert CONTRACT_CODES["UNAUTHENTICATED"] == 401


def test_the_operational_table_lists_the_five_documented_codes() -> None:
    assert OPERATIONAL_CODES == {
        "ROUTE_NOT_FOUND": 404,
        "METHOD_NOT_ALLOWED": 405,
        "DATABASE_UNAVAILABLE": 503,
        "NOT_READY": 503,
        "INTERNAL_ERROR": 500,
    }


# ---------------------------------------------------------------------------------
# One code, one entry, one status
# ---------------------------------------------------------------------------------


def test_every_code_has_exactly_one_registry_entry() -> None:
    assert set(REGISTRY) == set(ErrorCode)
    assert len(REGISTRY) == len(ErrorCode)


def test_no_code_is_declared_twice_in_the_enum() -> None:
    """Read from the source, because a duplicated enum member aliases silently."""
    names: list[str] = [
        target.id
        for node in _source_tree().body
        if isinstance(node, ast.ClassDef) and node.name == "ErrorCode"
        for statement in node.body
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name)
    ]

    assert names, "no enum members found; the parser is looking in the wrong place"
    duplicates = [name for name, count in Counter(names).items() if count > 1]
    assert not duplicates, f"duplicated enum members: {duplicates}"
    assert len(names) == len(ErrorCode)


def test_no_code_is_declared_twice_in_the_registry_literal() -> None:
    """A repeated key in a dict literal collapses, so `len(REGISTRY)` cannot catch it."""
    keys: list[str] = []
    for node in ast.walk(_source_tree()):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "REGISTRY":
            assert isinstance(node.value, ast.Dict)
            keys = [key.attr for key in node.value.keys if isinstance(key, ast.Attribute)]

    assert keys, "the REGISTRY literal was not found"
    duplicates = [name for name, count in Counter(keys).items() if count > 1]
    assert not duplicates, f"duplicated registry keys: {duplicates}"
    assert len(keys) == len(ErrorCode)


def test_every_code_value_equals_its_name() -> None:
    """Clients branch on the string; a value drifting from its name breaks them silently."""
    for code in ErrorCode:
        assert code.value == code.name


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda code: code.value)
def test_every_spec_is_well_formed(code: ErrorCode) -> None:
    spec = REGISTRY[code]

    assert isinstance(spec, CodeSpec)
    assert 400 <= spec.http_status <= 599, spec.http_status
    assert spec.message and spec.message.strip() == spec.message
    assert isinstance(spec.log_level, LogLevel)


# ---------------------------------------------------------------------------------
# Level discipline: a decline is `info`, a fault is `error`
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda code: code.value)
def test_a_4xx_logs_at_info_and_a_5xx_logs_at_error(code: ErrorCode) -> None:
    """The most important line in mds/08-error-logging.md: 20,000 losers of a seat race
    are not 20,000 errors, and an error rate that includes them is meaningless."""
    spec = REGISTRY[code]

    if spec.http_status < 500:
        assert spec.log_level is LogLevel.INFO, f"{code.value} is a 4xx logged at {spec.log_level}"
    else:
        assert spec.log_level is LogLevel.ERROR, f"{code.value} is a 5xx logged at {spec.log_level}"


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda code: code.value)
def test_the_fault_flag_tracks_the_status_class(code: ErrorCode) -> None:
    spec = REGISTRY[code]

    assert spec.fault is (spec.http_status >= 500), code.value


def test_no_decline_is_a_5xx() -> None:
    """Invariant 2: losing a race, exceeding a limit and replaying a key are all 4xx."""
    declines = {code for code, spec in REGISTRY.items() if not spec.fault}

    assert declines, "the registry declares no declines at all"
    for code in declines:
        assert REGISTRY[code].http_status < 500, code.value


# ---------------------------------------------------------------------------------
# The registry against the documents
# ---------------------------------------------------------------------------------


def test_the_registry_adds_no_code_the_documents_do_not_declare() -> None:
    documented = set(CONTRACT_CODES) | set(OPERATIONAL_CODES)
    surplus = {code.value for code in ErrorCode} - documented

    assert not surplus, f"codes in the registry but in no document: {sorted(surplus)}"


def test_every_endpoint_contract_code_is_in_the_registry() -> None:
    """Operational codes are excluded: `ROUTE_NOT_FOUND` and `METHOD_NOT_ALLOWED` are
    owned by SEAT-066, so they are not expected in the registry at Stage 0."""
    endpoint_codes = set(CONTRACT_CODES) - set(OPERATIONAL_CODES)
    missing = endpoint_codes - {code.value for code in ErrorCode}

    assert not missing, f"codes promised by mds/06-apis.md but not implemented: {sorted(missing)}"


def test_the_only_registry_surplus_over_the_contract_is_operational() -> None:
    surplus = {code.value for code in ErrorCode} - set(CONTRACT_CODES)

    assert surplus <= set(OPERATIONAL_CODES), sorted(surplus - set(OPERATIONAL_CODES))


@pytest.mark.parametrize("code", sorted(CONTRACT_CODES))
def test_a_documented_status_matches_the_registry(code: str) -> None:
    if code not in ErrorCode.__members__:
        pytest.skip(f"{code} is not implemented yet (SEAT-066)")

    assert REGISTRY[ErrorCode[code]].http_status == CONTRACT_CODES[code]


@pytest.mark.parametrize("code", sorted(OPERATIONAL_CODES))
def test_an_operational_status_matches_the_registry(code: str) -> None:
    if code not in ErrorCode.__members__:
        pytest.skip(f"{code} is not implemented yet (SEAT-066)")

    assert REGISTRY[ErrorCode[code]].http_status == OPERATIONAL_CODES[code]


def test_no_documented_code_is_unimplemented() -> None:
    """SEAT-066 landed the two router codes, so the documents and the registry now
    agree in both directions."""
    unimplemented = set(CONTRACT_CODES) | set(OPERATIONAL_CODES)
    unimplemented -= {code.value for code in ErrorCode}

    assert unimplemented == set()


# ---------------------------------------------------------------------------------
# AppError reads the registry rather than the call site
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda code: code.value)
def test_an_app_error_takes_status_message_and_level_from_the_registry(
    code: ErrorCode,
) -> None:
    spec = REGISTRY[code]
    error = AppError(code)

    assert error.http_status == spec.http_status
    assert error.message == spec.message
    assert error.log_level is spec.log_level
    assert error.details is None


def test_the_envelope_has_exactly_the_documented_shape() -> None:
    error = AppError(ErrorCode.SEAT_TAKEN, details={"conflicts": ["A13"]})

    envelope = error.envelope("req-1")

    assert envelope == {
        "error": {
            "code": "SEAT_TAKEN",
            "message": REGISTRY[ErrorCode.SEAT_TAKEN].message,
            "details": {"conflicts": ["A13"]},
            "request_id": "req-1",
        }
    }


def test_the_internal_error_message_describes_nothing_specific() -> None:
    """A 500 reaching a client is a bug report about this service, not information."""
    message = REGISTRY[ErrorCode.INTERNAL_ERROR].message.lower()

    for leak in ("sql", "postgres", "traceback", "exception", "table", "column", "asyncpg"):
        assert leak not in message, f"the generic 500 message mentions {leak!r}"
