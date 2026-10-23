"""Data contracts shared by every agent, the orchestrator and the UI."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Category = Literal["code", "config", "resource", "dependency"]
VerdictLabel = Literal["supported", "refuted", "insufficient"]
FixType = Literal["patch", "config_change", "rollback"]


class Evidence(BaseModel):
    source: str = Field(description="Log file name (e.g. 'app.log'), repo file ('repo/app/x.py') or commit ('git:<sha>')")
    line: int = Field(description="1-based line number exactly as shown by the tool output")
    quote: str = Field(description="Exact text copied from that line")


class Claim(BaseModel):
    id: str = Field(description="Claim id such as 'C1'")
    category: Category
    statement: str
    evidence: list[Evidence]


class TriageOutput(BaseModel):
    claims: list[Claim] = Field(description="1 to 3 hypotheses, most likely first")


class Verdict(BaseModel):
    claim_id: str
    verdict: VerdictLabel
    reasoning: str
    evidence: list[Evidence]


class VerifierOutput(BaseModel):
    verdicts: list[Verdict]


class SuspectChange(BaseModel):
    sha: str = Field(description="Commit sha (short or full)")
    summary: str
    reason: str = Field(description="Why the timing and the content match the symptom")
    evidence: list[Evidence]


class CodeHistoryOutput(BaseModel):
    suspects: list[SuspectChange]


class Edit(BaseModel):
    file: str = Field(description="Path inside the repo, e.g. 'app/checkout.py'")
    old: str = Field(description="Exact text to replace; must occur exactly once in the file")
    new: str = Field(description="Replacement text")


class FixOutput(BaseModel):
    fix_type: FixType
    rationale: str
    edits: list[Edit] = Field(default_factory=list, description="Required for patch and config_change")
    rollback_to: Optional[str] = Field(default=None, description="Commit sha to roll back to (rollback only)")


PlanAgent = Literal["investigator", "fact_checker", "change_finder", "fixer", "test_lab"]


class PlanStep(BaseModel):
    agent: PlanAgent
    goal: str = Field(description="One plain sentence starting with a verb, at most 14 words")


class Plan(BaseModel):
    steps: list[PlanStep] = Field(description="4 to 6 steps in order")
    rationale: str = Field(description="One sentence: what the plan focuses on, or what changed and why")


class ReportOutput(BaseModel):
    business_summary: str = Field(description="Three plain sentences for a manager: what broke, who it affected, what was done")
    summary: str
    impact: str
    root_cause: str
    follow_ups: list[str]


class SandboxRun(BaseModel):
    exit_code: int
    output: str
    seconds: float


class SandboxResult(BaseModel):
    mode: str
    before: SandboxRun
    after: Optional[SandboxRun] = None
    tests: Optional[SandboxRun] = None
    reproduced: bool
    passed: bool
    diff: str = ""
    error: Optional[str] = None
