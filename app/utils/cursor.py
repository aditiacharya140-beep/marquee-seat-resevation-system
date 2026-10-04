"""An opaque keyset cursor: the sort key of the last row a page returned."""

import base64
import json
from datetime import datetime
from uuid import UUID


def encode_cursor(created_at: datetime, row_id: UUID) -> str:
    payload = json.dumps([created_at.isoformat(), str(row_id)])
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Raises `ValueError` for anything that is not a cursor this module produced."""
    try:
        created_at, row_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(created_at), UUID(row_id)
    except (TypeError, KeyError) as exc:
        raise ValueError("malformed cursor") from exc
