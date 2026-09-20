"""Request/response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class MergeCreate(BaseModel):
    base: Any = Field(..., description="Common ancestor JSON document")
    left: Any = Field(..., description="Left side JSON document")
    right: Any = Field(..., description="Right side JSON document")
    result_schema: Any | None = Field(
        default=None,
        description=(
            "Optional Draft 2020-12 JSON Schema gating the final result. "
            "Validated against the meta-schema on creation; the resolved "
            "document must satisfy it."
        ),
    )
    idempotency_key: str | None = Field(
        default=None,
        description="Optional key; may also be sent as the Idempotency-Key header",
    )


class MergeAnalysisRequest(BaseModel):
    base: Any = Field(..., description="Common ancestor JSON document")
    left: Any = Field(..., description="Left side JSON document")
    right: Any = Field(..., description="Right side JSON document")


class ResolutionItem(BaseModel):
    pointer: str = Field(..., description="JSON Pointer of an open conflict")
    choice: Literal["left", "right"] | None = Field(
        default=None, description="Take the candidate from this side"
    )
    value: Any = Field(
        default=None, description="Explicit replacement value (may be null)"
    )

    @property
    def value_provided(self) -> bool:
        return "value" in self.model_fields_set


class ResolveRequest(BaseModel):
    revision: int = Field(..., description="Current merge revision (409 if stale)")
    resolutions: list[ResolutionItem] = Field(
        ..., min_length=1, description="Batch of conflict resolutions"
    )


class RuleItem(BaseModel):
    left: str | None = Field(
        default=None,
        description=(
            "RFC 6901-style path pattern; matched conflicts take the left "
            "candidate. Supports a single '*' segment and a trailing '**'."
        ),
    )
    right: str | None = Field(
        default=None,
        description=(
            "RFC 6901-style path pattern; matched conflicts take the right "
            "candidate. Supports a single '*' segment and a trailing '**'."
        ),
    )


class RuleResolveRequest(BaseModel):
    revision: int = Field(..., description="Current merge revision (409 if stale)")
    rules: list[RuleItem] = Field(
        ..., min_length=1, description="Ordered resolution rules (first one wins ties)"
    )


class ReuseResolutionsRequest(BaseModel):
    revision: int = Field(..., description="Current merge revision (409 if stale)")
    source_merge_id: str = Field(
        ...,
        min_length=1,
        description="ID of an already resolved merge whose adjudications are reused",
    )


class DraftUpdateRequest(BaseModel):
    resolutions: list[ResolutionItem] = Field(
        ...,
        min_length=1,
        description=(
            "Resolutions to append to (or overwrite in) the draft; each item "
            "takes 'choice' or 'value', like a resolution batch"
        ),
    )


class ResolutionUndoRequest(BaseModel):
    revision: int = Field(..., description="Current merge revision (409 if stale)")
