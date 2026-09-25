"""Normaliza eventos da SendPulse e aplica limites preventivos de reputação."""

from dataclasses import dataclass


EVENT_ALIASES = {
    "delivered": "delivered",
    "undelivered": "undelivered",
    "bounce": "bounce",
    "hard_bounce": "hard_bounce",
    "hard_bounces": "hard_bounce",
    "soft_bounce": "soft_bounce",
    "soft_bounces": "soft_bounce",
    "open": "opened",
    "opened": "opened",
    "click": "clicked",
    "clicked": "clicked",
    "spam": "spam",
    "spam_by_user": "spam",
    "unsubscribe": "unsubscribed",
    "unsubscribed": "unsubscribed",
    "resubscribed": "resubscribed",
}


def normalize_sendpulse_event(value: object) -> str | None:
    return EVENT_ALIASES.get(str(value or "").strip().lower())


def permanent_suppression_reason(event_type: str) -> str | None:
    return {
        "hard_bounce": "invalid_address",
        "spam": "spam_complaint",
        "unsubscribed": "unsubscribe",
    }.get(event_type)


@dataclass(frozen=True)
class ReputationSnapshot:
    sent_15m: int
    bounced_15m: int
    sent_24h: int
    complaints_24h: int

    @property
    def bounce_rate_percent(self) -> float:
        return 100 * self.bounced_15m / self.sent_15m if self.sent_15m else 0.0

    @property
    def complaint_rate_percent(self) -> float:
        return 100 * self.complaints_24h / self.sent_24h if self.sent_24h else 0.0


def reputation_pause_reasons(
    snapshot: ReputationSnapshot,
    *,
    minimum_sample: int,
    max_bounce_rate_percent: float,
    max_complaint_rate_percent: float,
) -> list[str]:
    reasons: list[str] = []
    if (
        snapshot.sent_15m >= minimum_sample
        and snapshot.bounce_rate_percent >= max_bounce_rate_percent
    ):
        reasons.append("bounce_rate")
    if (
        snapshot.sent_24h >= minimum_sample
        and snapshot.complaint_rate_percent >= max_complaint_rate_percent
    ):
        reasons.append("complaint_rate")
    return reasons
