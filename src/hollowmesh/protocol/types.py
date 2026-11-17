"""Record body schemas (Pydantic).

M6 (MVP): profile, post, help_request (optional reply_to).
M10 (Target): health_report, checkin (encrypted), help_ack, outage_report.
"""

from __future__ import annotations

from pydantic import BaseModel


class Profile(BaseModel):
    name: str
    level: int


class Post(BaseModel):
    text: str
    reply_to: str | None = None


class HelpRequest(BaseModel):
    text: str
    sector: str
    reply_to: str | None = None


BODY_SCHEMAS: dict[str, type[BaseModel]] = {
    "profile": Profile,
    "post": Post,
    "help_request": HelpRequest,
}
