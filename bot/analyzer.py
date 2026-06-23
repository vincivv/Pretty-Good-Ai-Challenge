"""
Post-call QA analysis using GPT-4o-mini.

For each completed call, analyze_transcript() reads the saved transcript,
sends it to GPT-4o-mini with a structured QA prompt, parses any bugs found,
and appends the findings to reports/bug_report.md.

Bug severity scale:
  Critical — safety risk (e.g. ignoring emergency symptoms)
  High     — major functional failure (wrong info, broken flow)
  Medium   — sub-optimal but not broken (unclear response, unnecessary friction)
  Low      — minor polish issue (awkward phrasing, redundant confirmation)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openai import OpenAI

_client = OpenAI()

BASE_DIR = Path(__file__).parent.parent

_QA_SYSTEM_PROMPT = """\
You are a senior QA analyst evaluating a medical office AI phone agent.

Your job is to read a call transcript and identify REAL bugs — things the agent
actually did wrong, based on what happened in THIS conversation.

CRITICAL RULE — Evaluate in context, not against pre-call assumptions:
  The agent may legitimately give a different outcome than the scenario
  description suggests if the conversation reveals new constraints. For example:
  - If the agent offered an alternative date/time because the first choice was
    unavailable, and the patient accepted, that is CORRECT behaviour — not a bug.
  - If the patient corrected the agent and the agent acknowledged and adapted,
    that is CORRECT behaviour — not a bug.
  Only flag something as a bug if the agent's action was wrong given what was
  said in the conversation itself.

For EACH distinct bug found, output a block in EXACTLY this format (no
variation in field names or order):

BUG: <one-sentence description of what went wrong>
SEVERITY: <Critical | High | Medium | Low>
TIMESTAMP: <MM:SS of first occurrence, e.g. 00:34>
EXPECTED: <what the agent should have done, given the conversation context>
ACTUAL: <what the agent actually did>
---

Severity guide:
  Critical — potential patient safety issue (ignored emergency, wrong medication)
  High     — major failure (incorrect factual claim, broken booking flow, hallucinated doctor)
  Medium   — clear mistake without immediate safety risk (misunderstood request, lost context)
  Low      — minor issue (repeated itself unnecessarily, slightly curt phrasing)

If you find NO bugs, output exactly:
  NO_BUGS_FOUND

Do NOT add any text before the first BUG block or after the last --- separator.
"""

_QA_USER_TEMPLATE = """\
Scenario name: {scenario_name}
Scenario description: {scenario_description}
Expected agent behaviour: {expected_behavior}

Transcript:
{transcript}
"""


def analyze_transcript(
    transcript_path: Path,
    scenario: dict,
    scenario_num: int,
) -> list[dict]:
    """Run GPT-4o-mini QA analysis on a completed call transcript.

    Reads the transcript at ``transcript_path``, sends it to GPT-4o-mini
    with a structured prompt, parses bug blocks from the response, and
    appends all findings to ``reports/bug_report.md``.

    Args:
        transcript_path: Path to the ``.txt`` transcript file.
        scenario:        The scenario dict from scenarios.json.
        scenario_num:    Numeric ID used for the report heading.

    Returns:
        List of parsed bug dicts, each with keys:
        ``scenario``, ``severity``, ``timestamp``, ``description``,
        ``expected``, ``actual``.
    """
    transcript_text = transcript_path.read_text().strip()
    if not transcript_text:
        print(f"    [WARN] Transcript is empty — skipping analysis")
        return []

    user_msg = _QA_USER_TEMPLATE.format(
        scenario_name=scenario.get("name", "Unknown"),
        scenario_description=scenario.get("description", ""),
        expected_behavior=scenario.get("expected_behavior", "Handle the request correctly."),
        transcript=transcript_text,
    )

    response = _client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _QA_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=1_200,
        temperature=0.2,  # low temperature for consistent structured output
    )

    raw_analysis = response.choices[0].message.content.strip()
    bugs = _parse_bugs(raw_analysis, scenario_num)
    _append_to_report(scenario, scenario_num, transcript_path, bugs)

    return bugs


# ─── Parsing ──────────────────────────────────────────────────────────────────


def _parse_bugs(raw: str, scenario_num: int) -> list[dict]:
    """Extract structured bug dicts from the GPT response text."""
    if "NO_BUGS_FOUND" in raw:
        return []

    bugs: list[dict] = []

    for block in raw.split("---"):
        block = block.strip()
        if not block or "BUG:" not in block:
            continue

        bug: dict = {"scenario": scenario_num}
        for line in block.splitlines():
            if line.startswith("BUG:"):
                bug["description"] = line[4:].strip()
            elif line.startswith("SEVERITY:"):
                bug["severity"] = line[9:].strip()
            elif line.startswith("TIMESTAMP:"):
                bug["timestamp"] = line[10:].strip()
            elif line.startswith("EXPECTED:"):
                bug["expected"] = line[9:].strip()
            elif line.startswith("ACTUAL:"):
                bug["actual"] = line[7:].strip()

        if "description" in bug:
            bugs.append(bug)

    return bugs


# ─── Report writer ────────────────────────────────────────────────────────────


def _append_to_report(
    scenario: dict,
    scenario_num: int,
    transcript_path: Path,
    bugs: list[dict],
) -> None:
    """Append QA findings for one call to reports/bug_report.md."""
    report_path = BASE_DIR / "reports" / "bug_report.md"
    report_path.parent.mkdir(exist_ok=True)

    severity_counts = _count_severities(bugs)
    summary_parts = [
        f"{count} {sev}" for sev, count in severity_counts.items() if count
    ]
    summary = ", ".join(summary_parts) if summary_parts else "none"

    with open(report_path, "a") as f:
        f.write(f"\n\n## Call #{scenario_num:02d} — {scenario.get('name', 'Unknown')}\n\n")
        f.write(f"| Field | Value |\n")
        f.write(f"|---|---|\n")
        f.write(f"| Date | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |\n")
        f.write(f"| Transcript | `{transcript_path.name}` |\n")
        f.write(f"| Bugs found | {len(bugs)} ({summary}) |\n\n")

        if bugs:
            f.write("### Bugs\n\n")
            for i, bug in enumerate(bugs, 1):
                severity = bug.get("severity", "Unknown")
                f.write(f"**Bug {i}** `[{severity}]` @ `{bug.get('timestamp', '?')}`\n\n")
                f.write(f"- **Issue:** {bug.get('description', '')}\n")
                f.write(f"- **Expected:** {bug.get('expected', '')}\n")
                f.write(f"- **Actual:** {bug.get('actual', '')}\n\n")
        else:
            f.write("_No bugs detected._\n")

        f.write("\n---\n")

    bug_word = "bug" if len(bugs) == 1 else "bugs"
    print(f"    Analysis → {report_path.name}  ({len(bugs)} {bug_word}: {summary})")


def _count_severities(bugs: list[dict]) -> dict[str, int]:
    """Return a dict of severity label → count, in descending order."""
    order = ["Critical", "High", "Medium", "Low"]
    counts = {sev: 0 for sev in order}
    for bug in bugs:
        sev = bug.get("severity", "")
        if sev in counts:
            counts[sev] += 1
    return counts
