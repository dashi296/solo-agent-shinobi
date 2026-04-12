from __future__ import annotations

import re
from pathlib import Path

from .models import MissionContext
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
CANDIDATE_SOURCE_SECTIONS = (
    "purpose",
    "requirements",
    "completion_criteria",
    "notes",
)
REFERENCE_SOURCE_SECTIONS = (
    "purpose",
    "requirements",
    "completion_criteria",
    "notes",
    "targets",
)

DEFAULT_REVIEW_NOTE_CATEGORIES = ("scope-control", "state-transition")
REVIEW_NOTE_CATEGORY_SIGNALS = {
    "state-transition": (
        "label",
        "phase",
        "state",
        "publish",
        "review",
        "start",
        "run",
        "transition",
        "lease",
    ),
    "cleanup-recovery": (
        "cleanup",
        "recovery",
        "resume",
        "retryable",
        "stale",
        "rollback",
        "lock",
        "failure",
    ),
    "test-coverage": (
        "test",
        "tests",
        "ci",
        "workflow",
        "coverage",
        "verification",
    ),
    "scope-control": (
        "scope",
        "candidate",
        "minimal context",
        "single task",
        "broad",
        "refactor",
    ),
    "docs-consistency": (
        "readme",
        "docs",
        "spec",
        "architecture",
        "mvp",
        "documentation",
    ),
}


def build_mission_context(root: Path, issue: dict) -> MissionContext:
    body = str(issue.get("body") or "")
    sections = parse_markdown_sections(body)
    store = StateStore(root)

    summary = read_optional_text(store.paths.summary_path)
    decisions = read_optional_text(store.paths.decisions_path)
    review_notes_sections = parse_review_note_sections(
        read_optional_text(store.paths.review_notes_path)
    )
    issue_paths = extract_paths_from_sections(
        sections,
        keys=REFERENCE_SOURCE_SECTIONS,
        fallback=body,
    )
    target_paths = filter_local_knowledge_paths(
        extract_candidate_files("\n".join(sections.get("targets", [])))
    )
    fallback_candidate_paths = extract_paths_from_sections(
        sections,
        keys=CANDIDATE_SOURCE_SECTIONS,
        fallback=body,
    )
    fallback_candidate_paths = filter_local_knowledge_paths(fallback_candidate_paths)
    candidate_files = target_paths or fallback_candidate_paths
    reference_files = unique_items(
        [
            ".shinobi/summary.md",
            ".shinobi/decisions.md",
            *issue_paths,
        ]
    )
    review_note_categories = select_review_note_categories(
        issue=issue,
        sections=sections,
        candidate_files=candidate_files,
        reference_files=reference_files,
        available_categories=list(review_notes_sections),
    )
    needs_human_review_reason = broad_scope_reason(body, sections, candidate_files)

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
        review_note_categories=review_note_categories,
        review_note_entries={
            category: review_notes_sections[category]
            for category in review_note_categories
            if category in review_notes_sections
        },
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
                append_section(sections, current_key, current_lines)
            current_key = section_key_for_heading(heading)
            current_lines = []
            continue

        if current_key is not None:
            current_lines.append(line)

    if current_key is not None:
        append_section(sections, current_key, current_lines)

    return sections


def append_section(
    sections: dict[str, list[str]], key: str, lines: list[str]
) -> None:
    sections.setdefault(key, []).extend(normalize_section_lines(lines))


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
        path = normalize_path_reference(raw_path)
        if not path or should_ignore_candidate_path(path):
            continue
        candidates.append(path)

    return unique_items(candidates)


def normalize_path_reference(path: str) -> str:
    normalized = path.strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def extract_paths_from_sections(
    sections: dict[str, list[str]], *, keys: tuple[str, ...], fallback: str
) -> list[str]:
    selected_text = "\n".join(item for key in keys for item in sections.get(key, []))
    if selected_text:
        return extract_candidate_files(selected_text)
    if sections:
        return []
    return extract_candidate_files(fallback)


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
    body: str,
    sections: dict[str, list[str]],
    candidate_files: list[str],
) -> str | None:
    if not candidate_files:
        if not body.strip():
            return None
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


def parse_review_note_sections(body: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in body.splitlines():
        heading_match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
        if heading_match is not None:
            level = len(heading_match.group(1))
            heading = heading_match.group(2).strip().lower()
            if level == 2:
                if current_key is not None:
                    append_section(sections, current_key, current_lines)
                current_key = heading
                current_lines = []
                continue
            if level < 2:
                if current_key is not None:
                    append_section(sections, current_key, current_lines)
                current_key = None
                current_lines = []
                continue
            if current_key is not None:
                current_lines.append(heading)
            continue

        if current_key is not None:
            current_lines.append(line)

    if current_key is not None:
        append_section(sections, current_key, current_lines)

    return {key: values for key, values in sections.items() if values}


def select_review_note_categories(
    *,
    issue: dict,
    sections: dict[str, list[str]],
    candidate_files: list[str],
    reference_files: list[str],
    available_categories: list[str],
) -> list[str]:
    if not available_categories:
        return []

    scoring_text = "\n".join(
        [
            str(issue.get("title") or ""),
            *sections.get("purpose", []),
            *sections.get("requirements", []),
            *sections.get("notes", []),
            *sections.get("scope_out", []),
            *candidate_files,
            *reference_files,
        ]
    ).lower()

    scored_categories: list[tuple[int, int, str]] = []
    for index, category in enumerate(available_categories):
        score = review_note_category_score(category, scoring_text, candidate_files, reference_files)
        scored_categories.append((score, -index, category))

    selected = [
        category
        for score, _, category in sorted(scored_categories, reverse=True)
        if score > 0
    ][:2]
    if selected:
        return selected

    fallback = [category for category in DEFAULT_REVIEW_NOTE_CATEGORIES if category in available_categories]
    if fallback:
        return fallback[:2]
    return available_categories[:2]


def review_note_category_score(
    category: str,
    scoring_text: str,
    candidate_files: list[str],
    reference_files: list[str],
) -> int:
    score = 0
    for signal in REVIEW_NOTE_CATEGORY_SIGNALS.get(category, ()):
        score += scoring_text.count(signal)

    paths = [*candidate_files, *reference_files]
    if category == "docs-consistency" and any(
        path == "README.md" or path.startswith("docs/") for path in paths
    ):
        score += 3
    if category == "test-coverage" and any(path.startswith("tests/") for path in paths):
        score += 3
    if category == "scope-control" and ("broad scope" in scoring_text or "candidate" in scoring_text):
        score += 3
    if category == "scope-control" and not candidate_files:
        score += 2
    return score


def unique_items(items: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique
