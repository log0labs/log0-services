"""What the model writes when it proposes a fix, before the code has checked it.

``ChangeProposalAnswer`` is the schema the model fills in (the descriptions are what it reads). The
checked form is the existing ``CodeChange`` of ``schemas/domain.py``: ``ground_changes`` in
``graph/subgraphs/change_proposer.py`` turns the first into the second, dropping what does not hold.
"""

from typing import Literal

from pydantic import BaseModel, Field


class CodeChangeDraft(BaseModel):
    file_path: str = Field(description="Path relative to the repository root, exactly as the tools returned it.")
    operation: Literal["modify", "add", "delete"] = Field(
        description="modify: replace old_code with new_code in an existing file. add: create a new file with new_code. delete: remove an existing file."
    )
    explanation: str = Field(description="One or two sentences: what this change does and why it fixes the root cause.")
    old_code: str | None = Field(
        default=None,
        description=(
            "For modify: the exact existing text to replace, copied character for character from the file "
            "(indentation included, WITHOUT the line numbers the read tool prints), long enough to occur "
            "exactly once in the file. Null for add and delete."
        ),
    )
    new_code: str | None = Field(
        default=None,
        description="For modify: the replacement text. For add: the whole content of the new file. Null for delete.",
    )


class ChangeProposalAnswer(BaseModel):
    changes: list[CodeChangeDraft] = Field(
        description="The smallest set of changes that fixes the root cause, at most five. Empty when no safe change can be made from what you read."
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Anything the engineer should know: why no change was proposed, what you could not verify, or what else is needed.",
    )
