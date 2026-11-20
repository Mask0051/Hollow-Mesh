"""P4 — Record payload schemas (Pydantic), Doc 2 §1.2.

MVP: profile, post, help_request (optional reply_to).
Target: health_report (P8, Countertally's input), checkin (encrypted, family only).
Stretch: help_ack, water_report, outage_report, tunnel_blocked, offer, vouch.
Demo: beacon (our addition, not in Doc 2) — the dashboard colours every node holding it.

Location is always level (0-3) + sector (e.g. "L2-S07"), never more precise.
Free text is capped at 280 characters. No floats anywhere.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class HealthReport(_Payload):
    """Countertally's input (P8): one household's symptoms, from the public-activity key.

    Analytics count at most one per author per day (Sybil damping, Doc 2 §3).
    """

    level: Level
    sector: Sector
    sym: Annotated[list[Literal["cough", "fever", "breath", "chest"]],
                   Field(min_length=1, max_length=4)]
    severity: Annotated[int, Field(ge=1, le=3)]
    sick: Annotated[int, Field(ge=1, le=50)]  # sick people in the household, incl. reporter
    onset_day: Annotated[int, Field(ge=0)]  # sim day the symptoms started

    @field_validator("sym")
    @classmethod
    def _no_repeats(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("each symptom at most once")
        return v

    @model_validator(mode="after")
    def _sector_on_level(self) -> HealthReport:
        # Countertally counts by level and by sector, so they must agree.
        if self.sector[1] != str(self.level):
            raise ValueError("sector is not on that level")
        return self


B64 = r"^[A-Za-z0-9+/]*={0,2}$"


class Checkin(_Payload):
    """Family check-in, sealed with the family's shared key (SecretBox) and published from
    the private feed. Relays see only ciphertext. No group tag: a phone tries each family
    key it holds (Doc 2 §1.2). The plaintext is a CheckinBody."""

    nonce: Annotated[str, Field(min_length=32, max_length=32, pattern=B64)]  # 24 bytes
    ct: Annotated[str, Field(min_length=24, max_length=1024, pattern=B64)]


class CheckinBody(_Payload):
    """What a check-in says, once a family member decrypts it. Never on the wire in clear."""

    status: Literal["ok", "hurt", "need_help", "moving"]
    level: Level
    sector: Sector
    note: Annotated[str, Field(max_length=140)] = ""

    @model_validator(mode="after")
    def _sector_on_level(self) -> CheckinBody:
        if self.sector[1] != str(self.level):
            raise ValueError("sector is not on that level")
        return self


class Beacon(_Payload):
    """Demo record: shows how far one message has spread.

    The record itself changes nothing. The dashboard paints every node that holds it in
    `color`, so nodes the message hasn't reached (e.g. across a cut stairwell) stay grey.
    Nodes know a record by (author, seq): node X holds it iff X.version_vector()[author] >= seq.
    """

    color: Annotated[str, Field(pattern=r"^#[0-9a-f]{6}$")]  # lowercase "#rrggbb"
    label: Annotated[str, Field(max_length=40)] = ""  # short on-screen caption


PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    "profile": Profile,
    "post": Post,
    "help_request": HelpRequest,
    "health_report": HealthReport,
    "checkin": Checkin,
    "beacon": Beacon,
}
