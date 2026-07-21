from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OutboundEmail:
    message_id: int
    to: str
    subject: str
    text: str
    unsubscribe_url: str


@dataclass(frozen=True)
class SendResult:
    accepted: bool
    provider_message_id: str | None = None
    error: str | None = None


class EmailProvider(Protocol):
    def send(self, email: OutboundEmail) -> SendResult: ...
