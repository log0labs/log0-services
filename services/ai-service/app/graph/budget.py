"""How much one investigation may spend, and the check that says when to stop.

An investigation that can loop (verify asks for more evidence, gathers it, verifies again)
needs hard limits, or one confused model could keep it running and spending forever. There
are three:

* wall-clock time: so an engineer is never left waiting on an endless run,
* tool calls: every log query or API call counts (``tool_calls`` in the state),
* verify rounds: how many times the hypotheses may be re-tested.

Hitting a limit is not a failure. The loop stops, the answer so far is kept, and the node
that noticed records a gap saying what was not done. The check only *looks*: it never
raises and never cancels a running node (each LLM call has its own timeout).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Budget:
    max_wall_seconds: float = 300.0
    max_tool_calls: int = 30
    max_verify_rounds: int = 2

    def exhausted(self, *, elapsed_seconds: float, tool_calls: int, verify_rounds: int) -> str | None:
        """Why no more work may start, or ``None`` when there is room. The text goes into a
        gap, so it reads as a sentence fragment: "... because the <reason>"."""
        if elapsed_seconds >= self.max_wall_seconds:
            return f"time limit ({self.max_wall_seconds:.0f} seconds) was reached"
        if tool_calls >= self.max_tool_calls:
            return f"tool-call limit ({self.max_tool_calls}) was reached"
        if verify_rounds >= self.max_verify_rounds:
            return f"verification round limit ({self.max_verify_rounds}) was reached"
        return None
