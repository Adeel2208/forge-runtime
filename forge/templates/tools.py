"""Your tools.

Replace these with the things your agent should actually be able to do, then
point `forge.toml` at this module:

    tools_module = "tools:registry"

Three declarations are required per tool, and they are the whole reason FORGE
can make the guarantees it does:

  args          a Pydantic model. Arguments are validated before dispatch, so
                a model that invents a field gets a rejection, not a crash.

  side_effect   READ | REVERSIBLE_WRITE | IRREVERSIBLE_WRITE. This drives how
                a failure is reconciled. A REVERSIBLE_WRITE must supply a
                compensator; registration fails without one.

  capability    the permission this tool needs. It must be granted in the
                policy bundle, or the tool cannot run no matter what the
                model says.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from forge.core.enums import RiskClass, SideEffect
from forge.tools.registry import ToolOutcome, ToolRegistry

registry = ToolRegistry()

# Stand-in for your real storage. Replace with a database, an API client, a
# queue - whatever your agent actually touches.
_NOTES: dict[str, str] = {}


class SearchArgs(BaseModel):
    query: str = Field(description="Text to search for.")


class SaveArgs(BaseModel):
    name: str = Field(description="Identifier for the note.")
    content: str = Field(description="Body of the note.")


@registry.tool(
    description="Find notes whose text matches a query. Returns matching names.",
    args=SearchArgs,
    side_effect=SideEffect.READ,
    capability="NOTES_READ",
)
async def search_notes(query: str) -> list[str]:
    needle = query.lower().strip()
    return [name for name, body in _NOTES.items() if needle in body.lower()]


async def _delete_note(name: str, content: str) -> None:
    """Compensator for `save_note`.

    Called when a write was applied but should not stand - for example when
    the write succeeded remotely and the response was lost. Undo, do not
    re-apply.
    """
    del content
    _NOTES.pop(name, None)


@registry.tool(
    description="Save a note. Reversible.",
    args=SaveArgs,
    side_effect=SideEffect.REVERSIBLE_WRITE,
    capability="NOTES_WRITE",
    risk=RiskClass.MEDIUM,
    compensate=_delete_note,
)
async def save_note(name: str, content: str) -> ToolOutcome:
    _NOTES[name] = content
    return ToolOutcome(
        ok=True,
        output=f"saved {name}",
        # `evidence` is what RECONCILE inspects. `applied: True` tells the
        # runtime the write really landed, which is what lets it compensate
        # correctly when the call reports failure afterwards.
        evidence={"applied": True, "name": name},
    )


# An irreversible action, deliberately left ungranted in the starter policy.
# Grant EXTERNAL_SEND in policy.yaml only when you mean it.
class SendArgs(BaseModel):
    to: str
    body: str


@registry.tool(
    description="Send a message externally. Irreversible - requires approval.",
    args=SendArgs,
    side_effect=SideEffect.IRREVERSIBLE_WRITE,
    capability="EXTERNAL_SEND",
    risk=RiskClass.HIGH,
    supports_dry_run=True,
)
async def send_message(to: str, body: str, _dry_run: bool = False) -> ToolOutcome:
    if _dry_run:
        return ToolOutcome(ok=True, output=f"[dry-run] would send {len(body)}b to {to}")
    # ... your real send goes here ...
    return ToolOutcome(
        ok=True,
        output=f"sent {len(body)} bytes to {to}",
        evidence={"applied": True, "to": to},
    )
