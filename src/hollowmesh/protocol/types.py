"""P4 — Record payload schemas (Pydantic), Doc 2 §1.2.

MVP: profile, post, help_request (optional reply_to).
Target: health_report (P8, Countertally).
Stretch: help_ack, checkin (encrypted), water_report, outage_report, tunnel_blocked, offer, vouch.

Location is always level (0-3) + sector (e.g. "L2-S07"), never more precise.
Free text is capped at 280 characters. No floats anywhere.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Level = Annotated[int, Field(ge=0, le=3)]
Sector = Annotated[str, Field(pattern=r"^L[0-3]-S\d{2}$")]
Text = Annotated[str, Field(max_length=280)]
RecordId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Profile(_Payload):
    """Genesis record (seq=1) of every feed."""

    kind: Literal["public", "private"]
    level: Level


class Post(_Payload):
    text: Text
    level: Level
    sector: Sector
    tags: Annotated[list[str], Field(max_length=5)] = []
    reply_to: RecordId | None = None
    expires_day: int | None = None


class HelpRequest(_Payload):
    kind: Literal["medical", "air", "water", "food", "trapped", "other"]
    level: Level
    sector: Sector
    urgency: Annotated[int, Field(ge=1, le=3)]
    people: Annotated[int, Field(ge=0)]
    text: Text
    reply_to: RecordId | None = None
    expires_day: int | None = None


PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    "profile": Profile,
    "post": Post,
    "help_request": HelpRequest,
}
