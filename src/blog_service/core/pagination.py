import base64
import json
from datetime import UTC, datetime
from uuid import UUID

from blog_service.core.errors import APIError


def encode_cursor(created_at: datetime, item_id: UUID) -> str:
    encoded = json.dumps(
        [created_at.astimezone(UTC).isoformat(), str(item_id)], separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        if len(cursor) > 512:
            raise ValueError("Cursor is too long")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != 2:
            raise ValueError("Expected a timestamp and UUID")
        created_at = datetime.fromisoformat(values[0])
        if created_at.tzinfo is None:
            raise ValueError("Timezone is required")
        return created_at.astimezone(UTC), UUID(values[1])
    except (ValueError, TypeError, OverflowError) as exc:
        raise APIError(400, "invalid_cursor", "Malformed pagination cursor.") from exc
