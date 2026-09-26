"""Explicit assignment policy. An audit reference is never user authorization."""
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Text = Annotated[str, Field(min_length=1, max_length=16000)]
NonemptyList = Annotated[list[Text], Field(min_length=1, max_length=100)]


def workspace_path(value: str, allowed_roots: list[str]) -> str:
    requested = Path(value)
    if not requested.is_absolute() or '..' in requested.parts:
        raise ValueError('Workspace must be an absolute path without traversal')
    try:
        if any(part.is_symlink() for part in (requested, *requested.parents)):
            raise PermissionError('Workspace and roots must not contain symlinks')
        if any(any(p.is_symlink() for p in (Path(root), *Path(root).parents)) for root in allowed_roots):
            raise PermissionError('Allowed roots must not contain symlinks')
        canonical = requested.resolve(strict=True)
        roots = [Path(root).resolve(strict=True) for root in allowed_roots]
    except (OSError, RuntimeError) as exc:
        raise ValueError('Workspace or allowed root is unavailable') from exc
    if not canonical.is_dir() or not any(canonical == root or root in canonical.parents for root in roots):
        raise PermissionError('Workspace is outside authorized roots')
    return str(canonical)


class Assignment(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    objective: Text
    task_type: Literal['inspect', 'implement', 'test', 'review']
    workspace: Text
    write_roots: Annotated[list[Text], Field(max_length=100)]
    context: Annotated[list[Text], Field(max_length=100)]
    instructions: Annotated[list[Text], Field(max_length=100)]
    in_scope: NonemptyList
    out_of_scope: NonemptyList
    criteria: NonemptyList
    verification: NonemptyList
    sandbox: Literal['read-only', 'workspace-write', 'full-access']
    network: bool
    approval_policy: Literal['on-request']
    authorization_ref: Text
    timeout_seconds: Annotated[int, Field(ge=1, le=86400)]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]

    @model_validator(mode='after')
    def consistent_policy(self):
        if self.sandbox == 'read-only' and (self.write_roots or self.network):
            raise ValueError('Read-only assignments cannot request writes or network')
        if self.sandbox != 'read-only' and not self.write_roots:
            raise ValueError('Write-capable assignments require explicit write roots')
        return self

    @field_validator('criteria')
    @classmethod
    def unique_criteria(cls, values):
        if len(set(values)) != len(values):
            raise ValueError('Acceptance criteria must be unique')
        return values
