"""A deterministic Markdown reader: a document becomes a tree of sections.

No model is involved. A section is a heading and the text up to the next heading, with the
facts a reader needs to search and link it: its heading path and neighbours, keywords, the
"important" lines (by a list of rules kept in one place), links, code blocks and code spans, and
the names, paths and identifiers it mentions.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_CODE_BLOCK_CHARS = 2000

_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([\w+#.-]*)")
_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:(?:::|\.)[A-Za-z_~][A-Za-z0-9_<>=]*)*")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

STOP_WORDS = frozenset(
    """a about above after again all also an and any are as at be because been before being below between both but by can could did do does
    doing down during each few for from further had has have having he her here hers him his how i if in into is it its just me more most my no nor
    not now of off on once only or other our out over own same she should so some such than that the their them then there these they this those
    through to too under until up use used uses using very was we were what when where which while who why will with would you your""".split()
)

#: What counts as "important" is a convention, not a judgement; these are the rules.
IMPORTANT_KEYWORDS = re.compile(r"\b(MUST NOT|MUST|SHOULD NOT|SHOULD|DEPRECATED|BREAKING|TODO|FIXME)\b")
IMPORTANT_PREFIX = re.compile(r"^\s*(?:>\s*)?(?:\*\*)?(Note|Warning|Important|Caution|Tip|Danger)(?:\*\*)?\s*[:：]", re.I)
CALLOUT = re.compile(r"^\s*>\s*\[!(NOTE|WARNING|IMPORTANT|TIP|CAUTION|DANGER)\]", re.I)
ADMONITION = re.compile(r"^\s*!!!\s+(\w+)")


@dataclass
class ParsedSection:
    title: str
    level: int
    heading_path: str
    start_line: int
    end_line: int
    text: str
    ordinal: int = 0
    parent_index: Optional[int] = None
    code_blocks: List[Dict[str, str]] = field(default_factory=list)
    inline_code: List[str] = field(default_factory=list)
    links: List[Dict[str, str]] = field(default_factory=list)
    tables: List[List[str]] = field(default_factory=list)
    important: List[str] = field(default_factory=list)
    front_matter: Dict[str, Any] = field(default_factory=dict)

    @property
    def text_hash(self) -> str:
        return hashlib.sha256((self.title + "\n" + self.text).encode("utf-8")).hexdigest()[:16]


def split_identifier(name: str) -> List[str]:
    """`snake_case`, `camelCase` and `a::b` into lower-case words."""
    words: List[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        for piece in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+", part):
            words.append(piece.lower())
    return words


def _front_matter(lines: List[str]) -> Tuple[Dict[str, Any], int]:
    """`(keys, first line after the block)`; a `---` block at the very top whose lines are `key: value`."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    for end in range(1, min(len(lines), 200)):
        if lines[end].strip() in ("---", "..."):
            data: Dict[str, Any] = {}
            for line in lines[1:end]:
                if ":" in line and not line.startswith((" ", "\t", "-")):
                    key, _, value = line.partition(":")
                    value = value.strip().strip("\"'")
                    if value.startswith("[") and value.endswith("]"):
                        data[key.strip()] = [item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()]
                    else:
                        data[key.strip()] = value
            return data, end + 1
    return {}, 0


def _headings(lines: List[str], first: int) -> List[Tuple[int, int, str]]:
    """`(line index, level, title)` of every heading outside a fenced block."""
    found: List[Tuple[int, int, str]] = []
    fence: Optional[str] = None
    for index in range(first, len(lines)):
        line = lines[index]
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token[0]
            elif token[0] == fence and not marker.group(2):
                fence = None
            continue
        if fence is not None:
            continue
        atx = _ATX.match(line)
        if atx:
            found.append((index, len(atx.group(1)), atx.group(2).strip()))
            continue
        if index + 1 < len(lines) and line.strip() and not line.lstrip().startswith(("#", ">", "-", "*", "+", "|", "<")) and not re.match(r"^\s*\d+[.)]\s", line):
            underline = _SETEXT.match(lines[index + 1])
            previous_blank = index == first or not lines[index - 1].strip()
            if underline and previous_blank:
                found.append((index, 1 if underline.group(1)[0] == "=" else 2, line.strip()))
    return found


def _analyse(body: List[str]) -> Dict[str, Any]:
    """Code blocks, code spans, links, table headers and important lines of a section's own lines."""
    blocks: List[Dict[str, str]] = []
    spans: List[str] = []
    links: List[Dict[str, str]] = []
    tables: List[List[str]] = []
    important: List[str] = []
    fence: Optional[str] = None
    language = ""
    current: List[str] = []
    previous_row = False
    in_comment = False
    for line in body:
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence, language, current = token[0], marker.group(2), []
            elif token[0] == fence and not marker.group(2):
                blocks.append({"language": language, "text": "\n".join(current)[:MAX_CODE_BLOCK_CHARS]})
                fence = None
            continue
        if fence is not None:
            current.append(line)
            continue
        if in_comment:
            in_comment = "-->" not in line
            continue
        if "<!--" in line and "-->" not in line:
            in_comment = True
            continue
        line = _COMMENT.sub("", line)
        for match in _CODE_SPAN.finditer(line):
            spans.append(match.group(2).strip())
        stripped_spans = _CODE_SPAN.sub(" ", line)
        for match in _LINK.finditer(stripped_spans):
            links.append({"text": match.group(2), "target": match.group(3), "image": bool(match.group(1))})
        is_row = bool(_TABLE_ROW.match(line))
        if is_row and not previous_row:
            tables.append([cell.strip() for cell in line.strip().strip("|").split("|")])
        previous_row = is_row
        if (
            IMPORTANT_KEYWORDS.search(line)
            or IMPORTANT_PREFIX.match(line)
            or CALLOUT.match(line)
            or ADMONITION.match(line)
        ):
            important.append(line.strip()[:300])
        for bold in _BOLD.finditer(stripped_spans):
            phrase = (bold.group(1) or bold.group(2)).strip()
            if 1 < len(phrase) <= 120:
                important.append(phrase)
    if fence is not None and current:  # an unclosed fence runs to the end of the section
        blocks.append({"language": language, "text": "\n".join(current)[:MAX_CODE_BLOCK_CHARS]})
    for header in tables:
        important.append("table: " + " | ".join(header)[:200])
    return {"blocks": blocks, "spans": spans, "links": links, "tables": tables, "important": list(dict.fromkeys(important))[:20]}


def parse_markdown(text: str) -> List[ParsedSection]:
    """The sections of a Markdown document, in order. A document with no heading is one section."""
    lines = text.splitlines()
    front, first = _front_matter(lines)
    headings = _headings(lines, first)
    sections: List[ParsedSection] = []

    # Setext headings take two lines (title and underline); ATX one.
    spans: List[Tuple[int, int, int, str, int]] = []
    for position, (index, level, title) in enumerate(headings):
        underlined = not _ATX.match(lines[index])
        spans.append((index, level, 2 if underlined else 1, title, position))

    preamble_end = headings[0][0] if headings else len(lines)
    preamble = lines[first:preamble_end]
    if any(line.strip() for line in preamble):
        analysis = _analyse(preamble)
        sections.append(
            ParsedSection(
                title="(top)", level=0, heading_path="/", start_line=first + 1, end_line=preamble_end,
                text="\n".join(preamble).strip(), code_blocks=analysis["blocks"], inline_code=analysis["spans"],
                links=analysis["links"], tables=analysis["tables"], important=analysis["important"], front_matter=front,
            )
        )
    elif front and not headings:
        sections.append(ParsedSection(title="(top)", level=0, heading_path="/", start_line=1, end_line=len(lines), text="", front_matter=front))

    stack: List[Tuple[int, int]] = []  # (level, index in sections)
    for position, (index, level, skip, title, _) in enumerate(spans):
        end = spans[position + 1][0] if position + 1 < len(spans) else len(lines)
        own = lines[index + skip : end]
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_index = stack[-1][1] if stack else None
        path = (sections[parent_index].heading_path if parent_index is not None and sections[parent_index].level > 0 else "/") + title + "/"
        analysis = _analyse(own)
        section = ParsedSection(
            title=title, level=level, heading_path=path, start_line=index + 1, end_line=end,
            text="\n".join(own).strip(), parent_index=parent_index, code_blocks=analysis["blocks"], inline_code=analysis["spans"],
            links=analysis["links"], tables=analysis["tables"], important=analysis["important"],
            front_matter=front if not sections else {},
        )
        sections.append(section)
        stack.append((level, len(sections) - 1))
    seen: Counter = Counter()
    for section in sections:
        section.ordinal = seen[section.heading_path]
        seen[section.heading_path] += 1
    return sections


def section_id(file_path: str, section: ParsedSection) -> str:
    digest = hashlib.sha1(f"{section.heading_path}|{section.ordinal}".encode("utf-8")).hexdigest()[:8]
    return f"{file_path}::{section.heading_path}#{digest}"


def tokens(text: str) -> List[str]:
    """Words of a text, identifiers split at `snake_case`, `camelCase` and `::`, stop words and one-letter words removed."""
    words: List[str] = []
    for identifier in _IDENTIFIER.findall(text):
        words.extend(split_identifier(identifier))
    return [word for word in words if len(word) > 1 and word not in STOP_WORDS and not word.isdigit()]


def keywords(section: ParsedSection, document_frequency: Dict[str, int], documents: int, limit: int) -> List[str]:
    """The `limit` words that best set this section apart: frequency times inverse document frequency."""
    counts = Counter(tokens(section.title + "\n" + section.title + "\n" + section.text))
    scored = []
    for word, count in counts.items():
        idf = math.log((1 + documents) / (1 + document_frequency.get(word, 0))) + 1.0
        scored.append((count * idf, word))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [word for _, word in scored[:limit]]


def mentions(section: ParsedSection) -> List[str]:
    """Names, paths and identifiers the section contains: code spans, the words of code blocks that look like names,
    and the targets of links."""
    found: List[str] = []
    for span in section.inline_code:
        found.append(span)
        found.extend(_IDENTIFIER.findall(span))
    for block in section.code_blocks:
        found.extend(_IDENTIFIER.findall(block["text"]))
    for link in section.links:
        found.append(link["target"])
    found.extend(re.findall(r"[\w./-]+\.[A-Za-z]{1,4}(?:::[\w.:~<>=]+)?(?:#[0-9a-f]{8})?", section.text))
    return list(dict.fromkeys(item for item in found if 1 < len(item) <= 200))[:400]


def dumps(value: Iterable[Any]) -> str:
    return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))
