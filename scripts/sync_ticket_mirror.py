#!/usr/bin/env python3
"""Render docs/tickets.md from the repository's live GitHub issue tracker."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.tasklists import tasklists_plugin

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "docs" / "tickets.md"
ISSUE_LIMIT = 10_000
ISSUE_FIELDS = "number,title,body,state,updatedAt,labels,milestone,url"

ISSUE_REF_RE = re.compile(r"(?<![\w/])#(\d+)\b")
LIST_ITEM_REF_RE = re.compile(r"^\s*(?:\[([ xX])\]\s+)?#(\d+)\b")
MILESTONE_ORDER_RE = re.compile(r"^M(\d+)\b", re.IGNORECASE)
MARKDOWN_ESCAPE_RE = re.compile(r"([\\`*_[\]<>#|])")
TRACKER_MARKDOWN = MarkdownIt("gfm-like", {"html": False})
ISSUE_BODY_MARKDOWN = MarkdownIt("gfm-like", {"html": False}).use(tasklists_plugin)


class MirrorError(RuntimeError):
    """Raised when live tracker data cannot produce a trustworthy mirror."""


@dataclass(frozen=True)
class MilestoneGroup:
    """Live issues and classifications for one GitHub milestone."""

    metadata: dict[str, Any]
    epic: dict[str, Any]
    original: tuple[dict[str, Any], ...]
    follow_ups: tuple[dict[str, Any], ...]
    other: tuple[dict[str, Any], ...]

    @property
    def issues(self) -> tuple[dict[str, Any], ...]:
        return (self.epic, *self.original, *self.follow_ups, *self.other)


@dataclass(frozen=True)
class SectionReference:
    """An issue reference found in a list under an epic body section."""

    number: int
    is_task: bool


def run_gh(arguments: Sequence[str]) -> str:
    """Run an authenticated gh command from the repository root."""

    try:
        result = subprocess.run(
            ["gh", *arguments],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise MirrorError("GitHub CLI (gh) is required but was not found") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
        raise MirrorError(f"gh {' '.join(arguments)} failed: {detail}")
    return result.stdout


def discover_repository() -> str:
    """Return the current GitHub owner/name slug."""

    repository = run_gh(
        ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    ).strip()
    if not repository or "/" not in repository:
        raise MirrorError(f"could not determine GitHub repository: {repository!r}")
    return repository


def load_json(raw: str, description: str) -> Any:
    """Decode gh JSON with a useful error if its shape is invalid."""

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MirrorError(f"gh returned invalid JSON for {description}") from exc


def fetch_issues(repository: str) -> list[dict[str, Any]]:
    """Fetch every issue, excluding pull requests, through gh issue list."""

    raw = run_gh(
        [
            "issue",
            "list",
            "--repo",
            repository,
            "--state",
            "all",
            "--limit",
            str(ISSUE_LIMIT),
            "--json",
            ISSUE_FIELDS,
        ]
    )
    issues = load_json(raw, "issues")
    if not isinstance(issues, list):
        raise MirrorError("expected the GitHub issue response to be a list")
    if len(issues) >= ISSUE_LIMIT:
        raise MirrorError(
            f"issue count reached the {ISSUE_LIMIT} fetch limit; increase ISSUE_LIMIT"
        )

    required = {
        "number",
        "title",
        "body",
        "state",
        "updatedAt",
        "labels",
        "milestone",
        "url",
    }
    for issue in issues:
        if not isinstance(issue, dict) or not required.issubset(issue):
            raise MirrorError("an issue is missing fields required by the mirror")
    return sorted(issues, key=lambda issue: int(issue["number"]))


def fetch_milestones(repository: str) -> dict[int, dict[str, Any]]:
    """Fetch open and closed milestone metadata indexed by milestone number."""

    raw = run_gh(
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repository}/milestones?state=all&per_page=100",
        ]
    )
    pages = load_json(raw, "milestones")
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise MirrorError("expected paginated GitHub milestone arrays")
    milestones = [milestone for page in pages for milestone in page]
    return {int(milestone["number"]): milestone for milestone in milestones}


def label_names(issue: dict[str, Any]) -> set[str]:
    """Return an issue's label names."""

    return {
        str(label["name"])
        for label in issue.get("labels", [])
        if isinstance(label, dict) and label.get("name")
    }


def find_master_tracker(issues: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Identify the unmilestoned roadmap epic used as the master tracker."""

    candidates = [
        issue
        for issue in issues
        if issue.get("milestone") is None
        and "roadmap" in label_names(issue)
        and "type:epic" in label_names(issue)
    ]
    titled = [issue for issue in candidates if "roadmap" in str(issue.get("title", "")).lower()]
    selected = titled or candidates
    if len(selected) != 1:
        numbers = ", ".join(f"#{issue['number']}" for issue in selected) or "none"
        raise MirrorError(f"expected one master roadmap tracker, found {numbers}")
    return selected[0]


def visible_inline_text(token: Token) -> str:
    """Return visible prose from an inline token, excluding code and images."""

    return "".join(
        child.content if child.type == "text" else "\n"
        for child in token.children or ()
        if child.type in {"text", "softbreak", "hardbreak"}
    )


def section_reference_entries(
    body: str, heading_matches: Callable[[str], bool]
) -> list[SectionReference]:
    """Parse issue-list entries inside matching Markdown body sections."""

    tokens = TRACKER_MARKDOWN.parse(body)
    active_level: int | None = None
    list_item_needs_inline: list[bool] = []
    references: dict[int, SectionReference] = {}

    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.level == 0:
            heading = tokens[index + 1] if index + 1 < len(tokens) else None
            heading_text = (
                visible_inline_text(heading).strip().lower()
                if heading is not None and heading.type == "inline"
                else ""
            )
            level = int(token.tag[1:])
            matches = heading_matches(heading_text)
            if active_level is None:
                if matches:
                    active_level = level
            elif level <= active_level:
                active_level = level if matches else None
            continue

        if token.type == "list_item_open":
            list_item_needs_inline.append(True)
            continue
        if token.type == "list_item_close":
            if list_item_needs_inline:
                list_item_needs_inline.pop()
            continue
        if token.type != "inline" or not list_item_needs_inline:
            continue
        if not list_item_needs_inline[-1]:
            continue
        list_item_needs_inline[-1] = False
        if active_level is None or tokens[index - 1].type == "heading_open":
            continue

        match = LIST_ITEM_REF_RE.match(visible_inline_text(token))
        if match is None:
            continue
        reference = SectionReference(number=int(match.group(2)), is_task=match.group(1) is not None)
        previous = references.get(reference.number)
        if previous is None or reference.is_task:
            references[reference.number] = reference

    return list(references.values())


def section_references(body: str, heading_matches: Callable[[str], bool]) -> list[int]:
    """Return issue references inside matching Markdown body sections."""

    return [reference.number for reference in section_reference_entries(body, heading_matches)]


def milestone_sort_key(title: str) -> tuple[int, str]:
    """Sort M1, M2, ... naturally before any nonstandard milestone names."""

    match = MILESTONE_ORDER_RE.match(title)
    if match:
        return int(match.group(1)), title
    return sys.maxsize, title


def build_groups(
    issues: Sequence[dict[str, Any]], milestones: dict[int, dict[str, Any]]
) -> list[MilestoneGroup]:
    """Classify each milestone's epic, original tickets, and follow-ups."""

    issues_by_milestone: dict[int, list[dict[str, Any]]] = {}
    for issue in issues:
        milestone = issue.get("milestone")
        if milestone is None:
            continue
        number = int(milestone["number"])
        issues_by_milestone.setdefault(number, []).append(issue)

    groups: list[MilestoneGroup] = []
    for milestone_number, milestone_issues in issues_by_milestone.items():
        metadata = milestones.get(milestone_number)
        if metadata is None:
            raise MirrorError(f"milestone {milestone_number} is missing API metadata")

        epics = [issue for issue in milestone_issues if "type:epic" in label_names(issue)]
        if len(epics) != 1:
            numbers = ", ".join(f"#{issue['number']}" for issue in epics) or "none"
            raise MirrorError(
                f"expected one epic in milestone {metadata['title']!r}, found {numbers}"
            )
        epic = epics[0]
        by_number = {int(issue["number"]): issue for issue in milestone_issues}

        original_references = section_reference_entries(
            str(epic["body"]), lambda heading: heading == "child issues"
        )
        follow_up_references = section_reference_entries(
            str(epic["body"]), lambda heading: "follow-up" in heading
        )
        follow_up_set = {reference.number for reference in follow_up_references}
        original_references = [
            reference for reference in original_references if reference.number not in follow_up_set
        ]

        # Plain list entries can document cross-milestone work (for example #91 in
        # the M1 closeout). A task entry is an ownership assertion and must resolve
        # inside this milestone, while an informational cross-reference is ignored.
        def milestone_references(
            references: Sequence[SectionReference],
        ) -> list[int]:
            return [
                reference.number
                for reference in references
                if reference.number in by_number or reference.is_task
            ]

        original_numbers = milestone_references(original_references)
        follow_up_numbers = milestone_references(follow_up_references)

        classified_numbers = {int(epic["number"])}

        def resolve(numbers: Sequence[int], kind: str) -> tuple[dict[str, Any], ...]:
            resolved: list[dict[str, Any]] = []
            for number in numbers:
                issue = by_number.get(number)
                if issue is None:
                    raise MirrorError(
                        f"{metadata['title']} epic references {kind} #{number}, "
                        "but it is not assigned to that milestone"
                    )
                resolved.append(issue)
                classified_numbers.add(number)
            return tuple(resolved)

        original = resolve(original_numbers, "child issue")
        follow_ups = resolve(follow_up_numbers, "follow-up")
        other = tuple(
            sorted(
                (
                    issue
                    for issue in milestone_issues
                    if int(issue["number"]) not in classified_numbers
                ),
                key=lambda issue: int(issue["number"]),
            )
        )
        groups.append(
            MilestoneGroup(
                metadata=metadata,
                epic=epic,
                original=original,
                follow_ups=follow_ups,
                other=other,
            )
        )

    return sorted(groups, key=lambda group: milestone_sort_key(str(group.metadata["title"])))


def text_token(content: str) -> Token:
    """Create an inline text token for a fragment of issue prose."""

    token = Token("text", "", 0)
    token.content = content
    return token


def linkify_issue_references(
    tokens: Sequence[Token], repository: str, issue_numbers: set[int]
) -> list[Token]:
    """Link bare issue references in prose without touching links or code."""

    transformed: list[Token] = []
    link_depth = 0
    for token in tokens:
        if token.type == "link_open":
            link_depth += 1
            transformed.append(token)
            continue
        if token.type == "link_close":
            transformed.append(token)
            link_depth = max(0, link_depth - 1)
            continue
        if token.type != "text" or link_depth:
            transformed.append(token)
            continue

        cursor = 0
        for match in ISSUE_REF_RE.finditer(token.content):
            if match.start() > cursor:
                transformed.append(text_token(token.content[cursor : match.start()]))
            number = int(match.group(1))
            resource = "issues" if number in issue_numbers else "pull"
            link_open = Token("link_open", "a", 1)
            link_open.attrSet("href", f"https://github.com/{repository}/{resource}/{number}")
            transformed.extend([link_open, text_token(f"#{number}"), Token("link_close", "a", -1)])
            cursor = match.end()
        if cursor < len(token.content):
            transformed.append(text_token(token.content[cursor:]))
    return transformed


def render_issue_body(
    body: str,
    repository: str,
    issue_numbers: set[int],
    heading_offset: int,
    anchor_prefix: str,
) -> str:
    """Render an untrusted issue body as safe HTML nested under its issue heading."""

    tokens = ISSUE_BODY_MARKDOWN.parse(body.strip())
    anchor_counts: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.type in {"heading_open", "heading_close"}:
            level = min(6, int(token.tag[1:]) + heading_offset)
            token.tag = f"h{level}"
            token.markup = "#" * level
            if token.type == "heading_open":
                heading = tokens[index + 1] if index + 1 < len(tokens) else None
                heading_text = (
                    visible_inline_text(heading)
                    if heading is not None and heading.type == "inline"
                    else ""
                )
                slug = re.sub(r"[^\w\s-]", "", heading_text.lower())
                slug = re.sub(r"[-\s]+", "-", slug).strip("-") or "section"
                base_anchor = f"{anchor_prefix}-{slug}"
                anchor_counts[base_anchor] = anchor_counts.get(base_anchor, 0) + 1
                occurrence = anchor_counts[base_anchor]
                anchor = base_anchor if occurrence == 1 else f"{base_anchor}-{occurrence}"
                token.attrSet("id", anchor)
        elif token.type == "inline" and token.children is not None:
            token.children = linkify_issue_references(token.children, repository, issue_numbers)
    return ISSUE_BODY_MARKDOWN.renderer.render(tokens, ISSUE_BODY_MARKDOWN.options, {}).strip()


def state_title(state: str) -> str:
    """Format GitHub's uppercase issue state for prose."""

    return state.lower().capitalize()


def single_line(value: Any) -> str:
    """Collapse external metadata to one line before Markdown interpolation."""

    return re.sub(r"\s+", " ", str(value)).strip()


def escape_markdown(value: Any) -> str:
    """Escape Markdown punctuation in externally supplied metadata."""

    return MARKDOWN_ESCAPE_RE.sub(r"\\\1", single_line(value))


def code_span(value: Any) -> str:
    """Render arbitrary single-line metadata as a safe Markdown code span."""

    text = single_line(value)
    longest_run = max((len(run) for run in re.findall(r"`+", text)), default=0)
    delimiter = "`" * (longest_run + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{delimiter}{padding}{text}{padding}{delimiter}"


def append_issue(
    lines: list[str],
    issue: dict[str, Any],
    repository: str,
    issue_numbers: set[int],
    kind: str,
    heading_level: int,
) -> None:
    """Append one issue and its live body to the generated Markdown."""

    number = int(issue["number"])
    title = escape_markdown(issue["title"])
    lines.extend(
        [
            f"{'#' * heading_level} [#{number} — {title}]({issue['url']})",
            "",
            f"- **Kind:** {kind}",
            f"- **Status:** {state_title(str(issue['state']))}",
        ]
    )
    milestone = issue.get("milestone")
    lines.append(
        f"- **Milestone:** {escape_markdown(milestone['title'])}"
        if milestone
        else "- **Milestone:** None"
    )
    labels = sorted(label_names(issue))
    rendered_labels = ", ".join(code_span(label) for label in labels) or "None"
    lines.extend(
        [
            f"- **Labels:** {rendered_labels}",
            f"- **Last updated:** {str(issue['updatedAt'])[:10]}",
            "",
        ]
    )
    body = render_issue_body(
        str(issue.get("body") or ""),
        repository,
        issue_numbers,
        heading_offset=heading_level - 1,
        anchor_prefix=f"issue-{number}",
    )
    if body:
        lines.extend([body, ""])


def render_label_taxonomy(issues: Sequence[dict[str, Any]]) -> list[str]:
    """Render every label currently used by a mirrored issue."""

    labels: dict[str, str] = {}
    for issue in issues:
        for label in issue.get("labels", []):
            if not isinstance(label, dict) or not label.get("name"):
                continue
            labels[str(label["name"])] = str(label.get("description") or "No description")
    lines = ["## Label taxonomy", ""]
    lines.extend(
        f"- {code_span(name)} — {escape_markdown(description)}"
        for name, description in sorted(labels.items())
    )
    lines.append("")
    return lines


def render_mirror(
    repository: str,
    issues: Sequence[dict[str, Any]],
    groups: Sequence[MilestoneGroup],
    master: dict[str, Any],
) -> str:
    """Render the complete deterministic Markdown mirror."""

    if not issues:
        raise MirrorError("cannot render an empty issue tracker")
    latest_update = max(str(issue["updatedAt"]) for issue in issues)
    open_count = sum(issue["state"] == "OPEN" for issue in issues)
    closed_count = len(issues) - open_count
    repository_url = f"https://github.com/{repository}"
    issue_numbers = {int(issue["number"]) for issue in issues}

    lines = [
        "# CogniStore Ticket Mirror",
        "",
        (
            "> Snapshot synchronized from GitHub Issues through "
            f"{latest_update} (latest tracker update). GitHub is the source of truth; "
            "this file is a generated, read-only reference."
        ),
        "",
        f"- **Repository:** [{escape_markdown(repository)}]({repository_url})",
        f"- **Master tracker:** [#{master['number']}]({master['url']})",
        "- **Source roadmap:** [roadmap.md](./roadmap.md)",
        "- **Original proposal:** [proposal.md](./proposal.md)",
        (f"- **Snapshot:** {len(issues)} issues; {open_count} open, {closed_count} closed"),
        "",
        "## How to use this mirror",
        "",
        "- Start with the master tracker and milestone epic for sequencing.",
        (
            "- Child tickets are listed in live epic order; their **Dependencies** "
            "sections are authoritative."
        ),
        "- Statuses and checkboxes reflect the live GitHub issue bodies in this file.",
        "- Make tracker changes in GitHub first, then regenerate this file.",
        "",
        "## Synchronize and validate",
        "",
        "Authenticate GitHub CLI, then run these commands from the repository root:",
        "",
        "```bash",
        "gh auth status",
        "python scripts/sync_ticket_mirror.py --write",
        "python scripts/sync_ticket_mirror.py --check",
        (
            'GITHUB_TOKEN="$(gh auth token)" lychee --no-progress '
            "docs/tickets.md docs/roadmap.md docs/next_ticket_roadmap_2026-08-27.md"
        ),
        "git diff --check",
        "```",
        "",
        (
            "`--write` replaces this mirror from live issue and milestone data. "
            "`--check` re-fetches the same data, prints a unified diff on drift, "
            "and exits nonzero without changing the file."
        ),
        "",
        "## Milestone index",
        "",
        ("| Milestone | Epic | Original | Follow-ups | Other | Open / total | GitHub |"),
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]

    for group in groups:
        milestone = group.metadata
        milestone_title = escape_markdown(milestone["title"])
        milestone_open = sum(issue["state"] == "OPEN" for issue in group.issues)
        milestone_state = state_title(str(milestone["state"]))
        lines.append(
            "| "
            f"{milestone_title} | "
            f"[#{group.epic['number']}]({group.epic['url']}) | "
            f"{len(group.original)} | {len(group.follow_ups)} | {len(group.other)} | "
            f"{milestone_open} / {len(group.issues)} | "
            f"[{milestone_state} milestone]({milestone['html_url']}) |"
        )
    lines.append("")
    lines.extend(render_label_taxonomy(issues))

    lines.extend(["## Master tracker", ""])
    append_issue(lines, master, repository, issue_numbers, "Master tracker", 3)

    rendered_numbers = {int(master["number"])}
    for group in groups:
        milestone = group.metadata
        milestone_title = escape_markdown(milestone["title"])
        milestone_open = sum(issue["state"] == "OPEN" for issue in group.issues)
        milestone_closed = len(group.issues) - milestone_open
        lines.extend(
            [
                f"## {milestone_title}",
                "",
                (f"- **GitHub milestone:** [{milestone_title}]({milestone['html_url']})"),
                f"- **Original delivery tickets:** {len(group.original)}",
                f"- **Verification follow-ups:** {len(group.follow_ups)}",
                f"- **Other tracking issues:** {len(group.other)}",
                (
                    f"- **Status:** {milestone_open} open, {milestone_closed} closed "
                    f"({len(group.issues)} including the epic)"
                ),
                "",
                "### Epic",
                "",
            ]
        )
        append_issue(lines, group.epic, repository, issue_numbers, "Milestone epic", 4)
        rendered_numbers.add(int(group.epic["number"]))

        categories = (
            ("Original delivery tickets", group.original, "Original delivery ticket"),
            ("Verification follow-ups", group.follow_ups, "Verification follow-up"),
            ("Other tracking issues", group.other, "Tracking issue"),
        )
        for category_title, category_issues, kind in categories:
            if not category_issues:
                continue
            lines.extend([f"### {category_title}", ""])
            for issue in category_issues:
                append_issue(lines, issue, repository, issue_numbers, kind, 4)
                rendered_numbers.add(int(issue["number"]))

    unmilestoned = [
        issue
        for issue in issues
        if issue.get("milestone") is None and int(issue["number"]) not in rendered_numbers
    ]
    if unmilestoned:
        lines.extend(["## Other unmilestoned issues", ""])
        for issue in unmilestoned:
            append_issue(lines, issue, repository, issue_numbers, "Unmilestoned issue", 3)
            rendered_numbers.add(int(issue["number"]))

    expected_numbers = {int(issue["number"]) for issue in issues}
    if rendered_numbers != expected_numbers:
        missing = sorted(expected_numbers - rendered_numbers)
        duplicate_or_extra = sorted(rendered_numbers - expected_numbers)
        raise MirrorError(
            f"mirror coverage mismatch; missing={missing}, extra={duplicate_or_extra}"
        )
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="replace the mirror")
    mode.add_argument("--check", action="store_true", help="fail if the mirror differs from GitHub")
    parser.add_argument(
        "--repo",
        help="GitHub owner/name (defaults to the repository detected by gh)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"mirror path (default: {DEFAULT_OUTPUT})",
    )
    return parser


def write_atomic(path: Path, content: str) -> None:
    """Replace a mirror atomically while preserving its existing file mode."""

    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    """Synchronize or validate the generated ticket mirror."""

    args = build_parser().parse_args(argv)
    output = args.output.resolve()
    try:
        repository = args.repo or discover_repository()
        issues = fetch_issues(repository)
        milestones = fetch_milestones(repository)
        master = find_master_tracker(issues)
        groups = build_groups(issues, milestones)
        rendered = render_mirror(repository, issues, groups, master)
    except MirrorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.write:
        write_atomic(output, rendered)
        print(f"Updated {output} from {len(issues)} live issues.")
        return 0

    try:
        current = output.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"error: mirror does not exist: {output}", file=sys.stderr)
        return 1
    if current == rendered:
        print(f"Ticket mirror matches {len(issues)} live issues.")
        return 0

    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        rendered.splitlines(keepends=True),
        fromfile=str(output),
        tofile=f"live:{repository}",
    )
    sys.stderr.writelines(diff)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
