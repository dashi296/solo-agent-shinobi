from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .state_store import StateStore


SECTION_ALIASES = {
    "purpose": ("目的", "goal", "purpose"),
    "requirements": ("要件", "requirements"),
    "completion_criteria": ("完了条件", "acceptance criteria", "done"),
    "scope_out": ("スコープ外", "out of scope", "scope out"),
    "notes": ("注意点", "notes", "cautions"),
    "targets": ("対象", "target", "targets", "files"),
    "prohibited": ("禁止事項", "やってはいけないこと", "prohibited"),
}

PATH_PATTERN = re.compile(r"`([^`\n]+)`|(?:^|\s)([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)")
HIGH_RISK_WORDS = (
    "全ファイル",
    "全体修正",
    "全体リファクタ",
    "repo 全体を変更",
    "repo 全体を修正",
    "repository-wide",
    "必要に応じて広く",
)
LOCAL_KNOWLEDGE_PATHS = {".shinobi/summary.md", ".shinobi/decisions.md"}


@dataclass(frozen=True)
class MissionContext:
    issue_number: int
    issue_title: str
    mission_summary: str
    completion_criteria: list[str]
    scope_out: list[str]
    reference_files: list[str]
    candidate_files: list[str]
    prohibited_actions: list[str]
    summary: str
    decisions: str
    requirements: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    needs_human_review: bool = False
    needs_human_review_reason: str | None = None


def build_mission_context(root: Path, issue: dict) -> MissionContext:
    body = str(issue.get("body") or "")
    sections = parse_markdown_sections(body)
    store = StateStore(root)

    summary = read_optional_text(store.paths.summary_path)
    decisions = read_optional_text(store.paths.decisions_path)
    issue_paths = extract_candidate_files(body)
    target_paths = extract_candidate_files("\n".join(sections.get("targets", [])))
    candidate_files = filter_local_knowledge_paths(target_paths or issue_paths)
    reference_files = unique_items(
        [
            ".shinobi/summary.md",
            ".shinobi/decisions.md",
            *issue_paths,
        ]
    )
    needs_human_review_reason = broad_scope_reason(sections, candidate_files)

    return MissionContext(
        issue_number=int(issue["number"]),
        issue_title=str(issue.get("title") or ""),
        mission_summary=first_paragraph(
            sections.get("purpose", []),
            fallback=str(issue.get("title") or ""),
        ),
        completion_criteria=sections.get("completion_criteria", []),
        scope_out=sections.get("scope_out", []),
        reference_files=reference_files,
        candidate_files=candidate_files,
        prohibited_actions=derive_prohibited_actions(sections),
        summary=summary,
        decisions=decisions,
        requirements=sections.get("requirements", []),
        notes=sections.get("notes", []),
        needs_human_review=needs_human_review_reason is not None,
        needs_human_review_reason=needs_human_review_reason,
    )


def parse_markdown_sections(body: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in body.splitlines():
        heading = parse_heading(line)
        if heading is not None:
            if current_key is not None:
                sections[current_key] = normalize_section_lines(current_lines)
            current_key = section_key_for_heading(heading)
            current_lines = []
            continue

        if current_key is not None:
            current_lines.append(line)

    if current_key is not None:
        sections[current_key] = normalize_section_lines(current_lines)

    return sections


def parse_heading(line: str) -> str | None:
    match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
    if match is None:
        return None
    return match.group(1).strip().lower()


def section_key_for_heading(heading: str) -> str:
    normalized = heading.strip().lower()
    for key, aliases in SECTION_ALIASES.items():
        if any(alias.lower() == normalized for alias in aliases):
            return key
    return normalized


def normalize_section_lines(lines: list[str]) -> list[str]:
    items: list[str] = []
    paragraph: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_paragraph(items, paragraph)
            continue

        list_item = re.match(r"^[-*]\s+(.*)$", stripped)
        ordered_item = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if list_item or ordered_item:
            flush_paragraph(items, paragraph)
            item = list_item.group(1) if list_item else ordered_item.group(1)
            items.append(item.strip())
            continue

        paragraph.append(stripped)

    flush_paragraph(items, paragraph)
    return items


def flush_paragraph(items: list[str], paragraph: list[str]) -> None:
    if not paragraph:
        return
    items.append(" ".join(paragraph).strip())
    paragraph.clear()


def first_paragraph(items: list[str], *, fallback: str) -> str:
    for item in items:
        if item:
            return item
    return fallback


def read_optional_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def extract_candidate_files(body: str) -> list[str]:
    candidates: list[str] = []

    for match in PATH_PATTERN.finditer(body):
        raw_path = match.group(1) or match.group(2) or ""
        path = raw_path.strip()
        if not path or should_ignore_candidate_path(path):
            continue
        candidates.append(path)

    return unique_items(candidates)


def filter_local_knowledge_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if path not in LOCAL_KNOWLEDGE_PATHS]


def should_ignore_candidate_path(path: str) -> bool:
    if " " in path:
        return True
    if path.startswith(("http://", "https://")):
        return True
    if path in {".", ".."}:
        return True
    return not ("/" in path or "." in path)


def derive_prohibited_actions(sections: dict[str, list[str]]) -> list[str]:
    prohibited = list(sections.get("prohibited", []))
    prohibited.extend(f"Do not include: {item}" for item in sections.get("scope_out", []))
    return unique_items(prohibited)


def broad_scope_reason(
    sections: dict[str, list[str]], candidate_files: list[str]
) -> str | None:
    if not candidate_files:
        return "issue body does not name candidate files"

    scope_text = "\n".join(
        item
        for key in ("purpose", "requirements", "targets", "notes")
        for item in sections.get(key, [])
    )
    lowered_scope_text = scope_text.lower()
    for word in HIGH_RISK_WORDS:
        if word.lower() in lowered_scope_text:
            return f"issue body contains broad scope marker: {word}"

    return None


def unique_items(items: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique
