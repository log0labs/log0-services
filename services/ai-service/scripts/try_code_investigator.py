"""Run the code investigator once with a REAL model, on a demo repository or on a local checkout.

    uv run python scripts/try_code_investigator.py --demo
    uv run python scripts/try_code_investigator.py --repo C:\\path\\to\\checkout [--prefix services/payment] [--trace trace.txt]

WARNING: this sends code to your configured INVESTIGATION model (Groq by default): the
stack-frame code, every file the model decides to read or search, and commit messages and diffs.
* ``--demo`` builds a small synthetic repository in a temporary folder (a payment service whose
  gateway call lost its timeout in the last commit): nothing of yours leaves the machine except
  that made-up code. Use it first.
* ``--repo`` points at a real local checkout: its code goes to the model provider. Secret-bearing
  file names are never read, but secrets written inside ordinary source files would be sent. Use a
  repository you are happy to send.

Nothing is stored and nothing goes to LangSmith unless you enabled tracing in your .env.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if sys.platform == "win32":  # keep the same event loop policy as the service
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.config import get_settings
from app.graph.subgraphs.code_investigator import (
    CodeInvestigatorContext,
    investigate_code,
)
from app.graph.subgraphs.code_tools import build_toolset
from app.graph.usage import UsageRecorder
from app.llm.models import PurposeChatModels
from app.parsing.stacktrace import parse_stack_trace
from app.tools.code import CodeTools
from app.tools.git import GitTools

DEMO_TRACE = (
    "java.net.SocketTimeoutException: Read timed out after 30000ms calling the payment gateway\n"
    "\tat com.demo.payment.PaymentProcessor.charge(PaymentProcessor.java:42)\n"
    "\tat com.demo.payment.CheckoutController.pay(CheckoutController.java:27)\n"
    "\tat org.springframework.web.servlet.FrameworkServlet.service(FrameworkServlet.java:883)\n"
)
DEMO_BRIEF = (
    "Service payment-service (production). 1,500 occurrences of java.net.SocketTimeoutException: "
    "Read timed out after 30000ms calling the payment gateway. First seen 2026-01-01T12:00:00Z."
)


def _git(root: Path, when: str, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Demo Dev", "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "Demo Dev", "GIT_COMMITTER_EMAIL": "dev@example.com", "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)


def make_demo_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="log0-demo-"))
    folder = root / "services" / "payment" / "src" / "main" / "java" / "com" / "demo" / "payment"
    folder.mkdir(parents=True)
    body = [f"    // padding {i}" for i in range(1, 80)]

    def processor(with_timeout: bool) -> str:
        lines = list(body)
        lines[41] = "        Response r = gateway.charge(request, Duration.ofSeconds(5));" if with_timeout else "        Response r = gateway.charge(request);  // blocks until the gateway answers"
        return "package com.demo.payment;\n\nclass PaymentProcessor {\n" + "\n".join(lines) + "\n}\n"

    (folder / "PaymentProcessor.java").write_text(processor(True), encoding="utf-8")
    (folder / "CheckoutController.java").write_text("package com.demo.payment;\n\nclass CheckoutController {\n    void pay() { /* calls PaymentProcessor.charge */ }\n}\n", encoding="utf-8")
    _git(root, "2025-12-01T10:00:00+00:00", "init", "-b", "main")
    _git(root, "2025-12-01T10:00:00+00:00", "add", "-A")
    _git(root, "2025-12-01T10:00:00+00:00", "commit", "-m", "Initial import of the payment service")
    (folder / "PaymentProcessor.java").write_text(processor(False), encoding="utf-8")
    _git(root, "2025-12-30T10:00:00+00:00", "add", "-A")
    _git(root, "2025-12-30T10:00:00+00:00", "commit", "-m", "Simplify the gateway call")
    return root


async def main(root: Path, prefix: str, trace: str, brief: str) -> None:
    settings = get_settings()
    recorder = UsageRecorder(settings.llm_prices)
    ctx = CodeInvestigatorContext(
        tools=build_toolset(CodeTools(root, prefix=prefix), GitTools(root, prefix=prefix)),
        models=PurposeChatModels(settings),
    )
    result = await investigate_code(ctx, brief=brief, frames=parse_stack_trace(trace).frames, config={"callbacks": [recorder]})

    findings = result.findings
    print(f"tool calls: {findings.tool_calls} | model turns: {findings.steps} | tokens: {recorder.input_tokens} in + {recorder.output_tokens} out")
    print(f"\nfindings ({len(findings.findings)}):")
    for finding in findings.findings:
        print(f"- [{finding.kind}] {finding.summary}")
        for ref in finding.locations:
            print(f"    at {ref.path}:{ref.start_line}-{ref.end_line}")
        for sha in finding.commits:
            print(f"    commit {sha[:8]}")
    print(f"\nunresolved: {findings.unresolved}")
    print(f"gaps: {result.gaps}")
    print("\nmodel calls (node, model, in, out, seconds):")
    for call in recorder.calls:
        print(f"  {call.node:10} {call.model:28} {call.input_tokens:6} {call.output_tokens:6} {call.seconds:6.2f}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--demo" in argv:
        repo_root, scope, trace_text, brief_text = make_demo_repo(), "services/payment", DEMO_TRACE, DEMO_BRIEF
        print(f"demo repository: {repo_root}")
    elif "--repo" in argv:
        repo_root = Path(argv[argv.index("--repo") + 1])
        scope = argv[argv.index("--prefix") + 1] if "--prefix" in argv else ""
        trace_text = Path(argv[argv.index("--trace") + 1]).read_text(encoding="utf-8") if "--trace" in argv else ""
        brief_text = argv[argv.index("--brief") + 1] if "--brief" in argv else "A production incident in this service; the failing code is described by the stack frames."
    else:
        sys.exit(__doc__)
    asyncio.run(main(repo_root, scope, trace_text, brief_text))
