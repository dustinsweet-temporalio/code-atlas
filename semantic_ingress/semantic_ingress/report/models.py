"""
Response models for the report's LLM calls.

Length and range limits are stated in the prompts and enforced in code, because
structured outputs do not support them in the schema.
"""
from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["confirmed", "rejected", "unclear"]
Severity = Literal["high", "medium", "low", "n/a"]


# --- lead verification ---------------------------------------------------------

class LeadVerdict(BaseModel):
    lead: str = Field(description="The lead key exactly as given")
    verdict: Verdict
    line: str = Field(description="Line anchor of the deciding evidence, e.g. L88 or L80-95; empty if none")
    severity: Severity = Field(description="For review checks; n/a for gates and rejected leads")
    reason: str = Field(description="One sentence, at most 30 words")

    model_config = {"extra": "forbid"}


class FileVerdicts(BaseModel):
    verdicts: list[LeadVerdict]

    model_config = {"extra": "forbid"}


# --- process card (what a process does today, from the source) ---------------------

Effect = Literal["paid_call", "external_call", "db_write", "file_write", "message", "irreversible"]


class Fact(BaseModel):
    text: str = Field(description="At most 30 words; 'Not established from the code.' if unknown")
    refs: list[str] = Field(description="Source passages that show it, as path@Lstart-end; only lines you have read")

    model_config = {"extra": "forbid"}


class Step(BaseModel):
    name: str
    does: str = Field(description="At most 25 words")
    effects: list[Effect]
    refs: list[str]

    model_config = {"extra": "forbid"}


class CrashLoss(BaseModel):
    stage: str = Field(description="When the crash happens, e.g. 'while files are being summarised'")
    loses: str = Field(description="At most 30 words: what is lost or must be repeated")
    refs: list[str]

    model_config = {"extra": "forbid"}


class ProcessCard(BaseModel):
    process: str
    trigger: Fact
    unit_of_work: Fact
    scales_with: str = Field(description="What the number of units per run depends on, at most 15 words")
    scale_assumption: str = Field(description="A typical run, stated as an assumption, at most 30 words")
    steps: list[Step]
    state_between_steps: Fact
    persistence: Fact
    crash_loss: list[CrashLoss]
    reliability: list[Fact]
    waits: list[Fact]
    run_length: Fact
    files: list[str] = Field(description="Indexed files that make up the process")

    model_config = {"extra": "forbid"}


class FactVerdict(BaseModel):
    fact: str = Field(description="The fact id exactly as given, e.g. F3")
    verdict: Literal["supported", "unsupported", "unclear"]
    reason: str = Field(description="One sentence, at most 25 words")

    model_config = {"extra": "forbid"}


class CardVerdicts(BaseModel):
    verdicts: list[FactVerdict]

    model_config = {"extra": "forbid"}


# --- conversion bars -------------------------------------------------------------

class BarScore(BaseModel):
    id: str = Field(description="Bar id from the rubric")
    score: int = Field(description="0 to 10, chosen from the bar's anchors")
    reason: str = Field(description="At most 20 words")
    evidence: list[str] = Field(description="Process card fact ids (F1, F2, ...) or indexed file paths that show it")
    signals: list[str] = Field(description="Signal codes that show it")
    cites: list[str] = Field(description="Pack ids the reason relies on")

    model_config = {"extra": "forbid"}


class ConversionScores(BaseModel):
    bars: list[BarScore]

    model_config = {"extra": "forbid"}


# --- conversion write-up (design or not-suitable) ----------------------------------

class DesignElement(BaseModel):
    kind: Literal["workflow", "activity", "child_workflow", "signal", "query", "update", "timer", "schedule"]
    name: str
    files: list[str] = Field(description="Indexed files this element is built from")
    does: str = Field(description="At most 20 words")
    cites: list[str] = Field(description="Pack ids or returned docs URLs")

    model_config = {"extra": "forbid"}


class Gain(BaseModel):
    text: str = Field(description="What the team gains, at most about 12 words, in plain terms")
    cites: list[str] = Field(description="Card fact ids (F1, F2, ...) or pack ids it rests on")

    model_config = {"extra": "forbid"}


class ProcessDesign(BaseModel):
    process: str = Field(description="Process name exactly as in the repository lens")
    elements: list[DesignElement]
    removes: list[str] = Field(description="Hand-built reliability code the design removes, each 'path: what', at most 15 words")
    stays: list[str] = Field(description="What stays outside Temporal and why, each at most 15 words")
    why: str = Field(description="At most 60 words: what goes wrong today and how the design addresses it")
    gains: list[Gain] = Field(description="Up to 3 concrete benefits of the design for the team")
    cites: list[str]

    model_config = {"extra": "forbid"}


class Reason(BaseModel):
    text: str = Field(description="At most 30 words")
    evidence: list[str] = Field(description="Indexed file paths")
    cites: list[str]

    model_config = {"extra": "forbid"}


class ConversionWriteup(BaseModel):
    verdict: str = Field(description="At most 80 words")
    designs: list[ProcessDesign] = Field(description="Empty when the repository is not suitable")
    reasons: list[Reason] = Field(description="Why it is not suitable; empty when designing")
    would_change: list[str] = Field(description="What would change the verdict; empty when designing")

    model_config = {"extra": "forbid"}


# --- evaluation write-up -------------------------------------------------------

class Remediation(BaseModel):
    finding: str = Field(description="The finding key exactly as given")
    fix: str = Field(description="At most 30 words")
    cites: list[str]

    model_config = {"extra": "forbid"}


class EvaluationWriteup(BaseModel):
    verdict: str = Field(description="At most 80 words")
    remediations: list[Remediation]

    model_config = {"extra": "forbid"}
