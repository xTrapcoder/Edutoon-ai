"""Schemas for training assessments: an administrator's, and - separately - an employee's.

**The two sides share nothing.** The administrator's option has ``is_correct``; the employee's
question and option are *different classes* with no field that could carry the correct option, its
key or an explanation. They are not one class with a flag that hides the answer: a serialiser for
the employee side cannot emit what its type does not have, and ``tests/test_assessments_api.py``
checks both the generated OpenAPI schema and the serialised response bytes. Request bodies reject
unknown fields (rule 10).

Scores are integers (``correct_count``, ``question_count``, ``threshold_percent``); there is no
percentage field, so no client ever receives a float to compare against the gate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from edutoon.domain.training_assessment import (
    DEFAULT_THRESHOLD_PERCENT,
    LABEL_MAX_CHARS,
    MAX_OPTIONS,
    MAX_QUESTIONS,
    MAX_THRESHOLD_PERCENT,
    MIN_OPTIONS,
    MIN_THRESHOLD_PERCENT,
    PROMPT_MAX_CHARS,
)
from edutoon.repositories.assessments import AttemptRecord
from edutoon.services.assessments import AdminAssessmentView, EmployeeAssessmentView

Prompt = Annotated[str, Field(min_length=1, max_length=PROMPT_MAX_CHARS)]
Label = Annotated[str, Field(min_length=1, max_length=LABEL_MAX_CHARS)]


# --- administrator -------------------------------------------------------------------------------


class AssessmentOptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Label
    is_correct: bool = False


class AssessmentQuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: Prompt
    options: Annotated[
        list[AssessmentOptionInput], Field(min_length=MIN_OPTIONS, max_length=MAX_OPTIONS)
    ]


class AssessmentPutRequest(BaseModel):
    """The whole assessment, replaced at once. ``threshold_percent`` is stored on the assessment."""

    model_config = ConfigDict(extra="forbid")

    threshold_percent: Annotated[int, Field(ge=MIN_THRESHOLD_PERCENT, le=MAX_THRESHOLD_PERCENT)] = (
        DEFAULT_THRESHOLD_PERCENT
    )
    questions: Annotated[
        list[AssessmentQuestionInput], Field(min_length=1, max_length=MAX_QUESTIONS)
    ]


class AdminOptionResponse(BaseModel):
    id: UUID
    position: int
    label: str
    is_correct: bool


class AdminQuestionResponse(BaseModel):
    id: UUID
    position: int
    prompt: str
    options: list[AdminOptionResponse]


class AdminAssessmentResponse(BaseModel):
    id: UUID
    module_id: UUID
    threshold_percent: int
    question_count: int
    questions: list[AdminQuestionResponse]
    updated_at: datetime

    @classmethod
    def from_view(cls, view: AdminAssessmentView) -> AdminAssessmentResponse:
        return cls(
            id=view.assessment.id,
            module_id=view.assessment.module_id,
            threshold_percent=view.assessment.threshold_percent,
            question_count=len(view.questions),
            questions=[
                AdminQuestionResponse(
                    id=q.id,
                    position=q.position,
                    prompt=q.prompt,
                    options=[
                        AdminOptionResponse(
                            id=o.id, position=o.position, label=o.label, is_correct=o.is_correct
                        )
                        for o in q.options
                    ],
                )
                for q in view.questions
            ],
            updated_at=view.assessment.updated_at,
        )


# --- employee ------------------------------------------------------------------------------------


class EmployeeOptionResponse(BaseModel):
    """What an employee sees of an option: its id, place and wording. Nothing else."""

    id: UUID
    position: int
    label: str


class EmployeeQuestionResponse(BaseModel):
    id: UUID
    position: int
    prompt: str
    options: list[EmployeeOptionResponse]


class EmployeeAssessmentResponse(BaseModel):
    module_id: UUID
    threshold_percent: int
    question_count: int
    questions: list[EmployeeQuestionResponse]

    @classmethod
    def from_view(cls, view: EmployeeAssessmentView) -> EmployeeAssessmentResponse:
        return cls(
            module_id=view.assessment.module_id,
            threshold_percent=view.assessment.threshold_percent,
            question_count=len(view.questions),
            questions=[
                EmployeeQuestionResponse(
                    id=q.id,
                    position=q.position,
                    prompt=q.prompt,
                    options=[
                        EmployeeOptionResponse(id=o.id, position=o.position, label=o.label)
                        for o in q.options
                    ],
                )
                for q in view.questions
            ],
        )


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: UUID
    option_id: UUID


class AttemptSubmitRequest(BaseModel):
    """Every question answered exactly once, in one request."""

    model_config = ConfigDict(extra="forbid")

    answers: Annotated[list[AnswerInput], Field(min_length=1, max_length=MAX_QUESTIONS)]


class AttemptResponse(BaseModel):
    """A graded attempt: integer score, the threshold it was graded against, and the verdict.
    Deliberately without the answers' correctness - the key is never sent to an employee."""

    id: UUID
    module_id: UUID
    attempt_number: int
    correct_count: int
    question_count: int
    threshold_percent: int
    passed: bool
    submitted_at: datetime

    @classmethod
    def from_record(cls, attempt: AttemptRecord) -> AttemptResponse:
        return cls(
            id=attempt.id,
            module_id=attempt.module_id,
            attempt_number=attempt.attempt_number,
            correct_count=attempt.correct_count,
            question_count=attempt.question_count,
            threshold_percent=attempt.threshold_percent,
            passed=attempt.passed,
            submitted_at=attempt.submitted_at,
        )


class AttemptListResponse(BaseModel):
    items: list[AttemptResponse]
