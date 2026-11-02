"""What the local model extracts from one entry, as Pydantic models.

The activity and milestone domains come from the taxonomy, so the JSON schema handed to Ollama
lists them as an enum and the model cannot invent new ones.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, create_model

Short = Field(min_length=1, max_length=80)
GENERAL = "general"  # milestone domain for events outside the taxonomy


class Person(BaseModel):
    name: str = Short
    relation: Literal["own_life", "public_or_watched", "not_a_person"]


class HealthEvent(BaseModel):
    condition: str = Short
    category: Literal["injury", "illness", "pain", "mental"]  # no catch-all: it collected false reports
    type: Literal["symptom", "diagnosis", "treatment", "recovery"] = "symptom"
    severity: int | None = Field(None, ge=1, le=5)


class _Activity(BaseModel):
    domain: str
    activity: str = Short
    duration_min: int | None = Field(None, ge=1, le=1440)
    intensity: int | None = Field(None, ge=1, le=5)
    sentiment: int | None = Field(None, ge=-2, le=2)


class _Milestone(BaseModel):
    domain: str
    title: str = Field(min_length=1, max_length=120)
    kind: Literal["first", "achievement", "setback", "decision"]


class _Extraction(BaseModel):
    # written first, so the model names the tone before it scores; used for generation only, not stored
    mood_reason: str = Field(max_length=120)
    mood_score: int = Field(ge=-5, le=5)  # required
    emotions: list[str] = Field(default_factory=list, max_length=8)
    energy: int | None = Field(None, ge=1, le=5)
    sleep: Literal["good", "normal", "poor"] | None = None
    themes: list[str] = Field(default_factory=list, max_length=8)
    people: list[Person] = Field(default_factory=list, max_length=30)
    health_events: list[HealthEvent] = Field(default_factory=list, max_length=10)
    activities: list[_Activity] = Field(default_factory=list, max_length=15)
    milestones: list[_Milestone] = Field(default_factory=list, max_length=10)


class MoodOnly(BaseModel):
    """The mood part of the extraction on its own: faster, for a pass over the whole diary."""
    mood_reason: str = Field(max_length=120)
    mood_score: int = Field(ge=-5, le=5)


def build_model(domains: list[str]) -> type[_Extraction]:
    """The extraction model with `domain` restricted to the taxonomy's domain names."""
    if not domains:
        raise ValueError("The taxonomy defines no domains.")
    domain = Literal[tuple(domains)]  # type: ignore[valid-type]
    # a milestone may lie outside every domain (work, family); without "general" it was left out
    milestone_domain = Literal[tuple(dict.fromkeys([*domains, GENERAL]))]  # type: ignore[valid-type]
    activity = create_model("Activity", __base__=_Activity, domain=(domain, ...))
    milestone = create_model("Milestone", __base__=_Milestone, domain=(milestone_domain, ...))
    return create_model(
        "Extraction", __base__=_Extraction,
        activities=(list[activity], Field(default_factory=list, max_length=15)),
        milestones=(list[milestone], Field(default_factory=list, max_length=10)),
    )
