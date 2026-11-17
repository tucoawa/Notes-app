"""Block parsing and marker helpers for per-block metadata.

A "block" is a top-level markdown chunk, split on blank lines
(`\\n\\n`). A block may optionally carry a stable ID marker on its
own line at the end:

    - [ ] eggs
    <!--blk:a3f9c1e2-->

The marker is:
  - invisible in the preview (stripped before rendering)
  - invisible in the CodeMirror editor (hidden by a widget in editor.js)
  - stripped on markdown export

Blocks WITHOUT a marker get a deterministic ID derived from the
block's text (sha1 of the text, first 8 hex chars). This means the
same content always produces the same IDs across parses — so
block links written as `[[Note#a3f9c1e2]]` keep working.

If two blocks in the same note have identical text, the second gets
`a3f9c102`, the third `a3f9c103`, etc. (suffix by occurrence count).

Marker format rules (strict, to avoid colliding with user HTML
comments):
  - starts with ``<!--blk:``
  - 8 lowercase hex chars
  - ends with ``-->``
  - the whole thing on its own line
"""

import hashlib
import re
import secrets


MARKER_RE = re.compile(r'<!--blk:([0-9a-f]{8})-->')
MARKER_LINE_RE = re.compile(r'^\s*<!--blk:[0-9a-f]{8}-->\s*$')


def new_block_id() -> str:
    """Return a new 8-char lowercase hex block ID."""
    return secrets.token_hex(4)


def block_hash(text: str) -> str:
    """Stable hash of a block's visible text, ignoring markers."""
    return hashlib.sha1((text or '').encode('utf-8')).hexdigest()


def _stable_id(text: str) -> str:
    """Deterministic 8-char hex ID derived from block text."""
    return hashlib.sha1((text or '').encode('utf-8')).hexdigest()[:8]


def strip_markers(text: str) -> str:
    """Remove all block markers from `text` (for preview/export)."""
    if not text:
        return text
    lines = text.split('\n')
    kept = [ln for ln in lines if not MARKER_LINE_RE.match(ln)]
    return '\n'.join(kept)


def split_blocks(content: str) -> list[dict]:
    """Split `content` into top-level blocks on blank lines.

    Each block dict has:
      - id:      8-char hex. From the marker if present, otherwise
                 a deterministic hash of the block text (stable
                 across parses). Identical texts in the same note
                 get unique suffixes.
      - text:    visible text (markers stripped, trailing whitespace trimmed)
      - marker:  the full marker line as it appears in content (or '')
      - hash:    sha1 of `text`
      - raw:     the block as it appears in content (including marker line)
      - start:   character offset in `content` where the block starts
      - end:     character offset where the block ends (exclusive)
    """
    if not content:
        return []

    blocks = []
    # Split on runs of two or more newlines, keeping offsets.
    # We walk manually so we can record start/end offsets.
    i = 0
    n = len(content)
    block_start = 0

    def emit(start, end):
        raw = content[start:end]
        # Find a marker on its own line at the end of the raw block.
        lines = raw.rstrip('\n').split('\n')
        marker = ''
        if lines and MARKER_LINE_RE.match(lines[-1]):
            marker = lines[-1].strip()
            # Remove it from the visible text
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
        if not text and not marker:
            return  # empty block, skip
        m = MARKER_RE.search(marker)
        if m:
            block_id = m.group(1)
        else:
            # Deterministic ID from block text (stable across parses)
            block_id = _stable_id(text)
        blocks.append({
            'id': block_id,
            'text': text,
            'marker': marker,
            'hash': block_hash(text),
            'raw': raw,
            'start': start,
            'end': end,
        })

    while i < n:
        if content[i] == '\n':
            # count run of newlines
            j = i
            while j < n and content[j] == '\n':
                j += 1
            if j - i >= 2:
                emit(block_start, i)
                block_start = j
            i = j
        else:
            i += 1
    emit(block_start, n)

    # Post-process: disambiguate blocks with identical text.
    # Two blocks with the same text → same base ID → we suffix
    # the second one and beyond.
    seen = {}
    for b in blocks:
        base = b['id']
        # Only adjust blocks that are NOT from a marker. A marker
        # is authoritative.
        if b['marker']:
            continue
        count = seen.get(base, 0)
        seen[base] = count + 1
        if count > 0:
            # Suffix: replace last 2 chars with zero-padded count
            # e.g. a3f9c1e2 → a3f9c102 for the second identical block.
            b['id'] = f"{base[:6]}{count:02d}"

    return blocks


def markers_in(content: str) -> set[str]:
    """Return the set of all block IDs currently referenced in content."""
    return set(MARKER_RE.findall(content or ''))


def ensure_marker(content: str, block_id: str) -> str:
    """Return `content` with a marker for `block_id` guaranteed present
    on the block whose text matches the block bearing that ID (if any).
    If the ID is already in the content, this is a no-op.
    """
    if block_id in markers_in(content):
        return content
    blocks = split_blocks(content)
    for b in blocks:
        if b['id'] == block_id:
            if b['marker']:
                return content  # already present
            # Append the marker on a new line after the block
            insertion = f"\n<!--blk:{block_id}-->"
            return content[:b['end']] + insertion + content[b['end']:]
    return content


def add_marker_to_first_match(content: str, block_text: str, block_id: str) -> str:
    """Insert a marker for `block_id` after the first block whose text
    matches `block_text`. Used by the frontend after a save: the
    client says "this block's text is X, give it this ID", and we
    splice the marker in.
    """
    blocks = split_blocks(content)
    for b in blocks:
        if b['text'] == block_text.strip():
            if b['marker']:
                return content  # already has one
            insertion = f"\n<!--blk:{block_id}-->"
            return content[:b['end']] + insertion + content[b['end']:]
    return content