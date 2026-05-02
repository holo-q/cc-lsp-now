"""Pure renderer helpers for the agent-first LSP tool surface.

Side-effect-free formatting primitives. They take plain data and return
strings; they never read from the filesystem, the LSP chain, the semantic
nav context, or render-memory state. That keeps them safe to call from any
worker, any thread, and any test, and makes their behaviour pinnable
without spinning up a language server.

The shapes pinned here follow ``docs/rendering.md``,
``docs/render-memory.md``, and ``docs/lsp-path.md``:

- non-exhaustive sample lists render as ``L<n>,L<n>,L<n>,...`` with a
  trailing ``...`` only when more remain past ``max_shown``;
- truncation footers and empty states use one family
  (``... +<n> more <kind>; raise <knob> to unfold.``,
  ``No <scope>.`` / ``No <scope> for <target>.``);
- legend blocks are reversible: every member listed is decoded back to
  ``<bucket_label>.<member_name>@L<line>``;
- dense path/call rows compress to ``A3 -> A7 -> J1`` (or
  ``A3 -calls-> A7`` when edge family must be carried per-hop), matching
  the L3 sample in ``docs/render-memory.md``.

``server.py`` may eventually consume these helpers when it migrates the
inline ``lines.append(...)`` assemblers to the renderer layer described in
``docs/rendering.md``. Until then, they are tested in isolation; the
existing inline renderers stay until that migration ships, so the agent
surface contract does not flap.

A ``RenderMemory``-shaped Protocol is declared here so future
``src/hsp/render_memory.py`` can plug in without changing this
module's API. The Protocol is intentionally small: aliases are looked up
*by record* (already minted by the alias book), never minted here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


COMPACT_LIMIT = 240
SAMPLE_DEFAULT_MAX = 3
ROW_SEP = " — "


def compact_one_line(text: str, limit: int = COMPACT_LIMIT) -> str:
    """Mirror of ``server._compact_line`` but pure.

    Trims to ``limit`` characters with a single-character ``…`` ellipsis when
    over budget. Newlines are kept intact (callers that want a strict
    one-line invariant should join their own pieces beforehand) so the
    helper can be slotted under existing call sites without changing
    behavior.
    """
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_sample_lines(
    lines: Iterable[int],
    max_shown: int = SAMPLE_DEFAULT_MAX,
) -> str:
    """Render non-exhaustive sample line numbers as
    ``L<n>,L<n>,L<n>,...``.

    Each line number is rendered as ``L<n>`` (1-based; the helper does not
    re-shift). When the input has more than ``max_shown`` items, a literal
    ``...`` is appended so an agent can tell at a glance that the list was
    clipped (``samples L57,L694,L218,...``). When the list fits inside the
    budget the trailing ``...`` is omitted (``samples L57,L694``).

    ``max_shown=0`` always elides; the only way to get an empty string is
    to pass an empty ``lines`` iterable, which the caller should usually
    short-circuit before calling.
    """
    seq = list(lines)
    shown = seq[:max_shown] if max_shown > 0 else []
    parts = [f"L{n}" for n in shown]
    if len(seq) > len(shown):
        parts.append("...")
    return ",".join(parts)


def format_sample_locs(
    locs: Iterable[tuple[str | None, int]],
    max_shown: int = SAMPLE_DEFAULT_MAX,
    primary_path: str | None = None,
) -> str:
    """Render mixed-file line samples.

    Each entry is ``(path, line_1based)``. When ``path`` matches
    ``primary_path`` (or is ``None``) the entry compresses to ``L<line>``;
    otherwise it expands to ``<basename>:L<line>`` so cross-file evidence
    stays visible. A trailing ``...`` is appended when the input exceeds
    ``max_shown``, matching ``format_sample_lines``.
    """
    seq = list(locs)
    shown = seq[:max_shown] if max_shown > 0 else []
    parts: list[str] = []
    for path, line in shown:
        if path is None or path == primary_path:
            parts.append(f"L{line}")
        else:
            parts.append(f"{Path(path).name}:L{line}")
    if len(seq) > len(shown):
        parts.append("...")
    return ",".join(parts)


def format_truncation_footer(more: int, kind: str, knob: str) -> str:
    """``... +<n> more <kind>; raise <knob> to unfold.``

    One-family truncation tail per ``docs/rendering.md``. ``more`` is the
    count of additional items past whatever the renderer chose to show;
    ``kind`` is the human-readable noun (``refs``, ``groups``, ``edges``,
    ``samples``); ``knob`` is the parameter name an agent can raise.
    """
    return f"... +{more} more {kind}; raise {knob} to unfold."


def format_empty_state(scope: str, target: str | None = None) -> str:
    """``No <scope>.`` or ``No <scope> for <target>.``

    One-family empty state per ``docs/rendering.md``. Agents should never
    have to learn a dozen local phrasings for "nothing here".
    """
    if target:
        return f"No {scope} for {target}."
    return f"No {scope}."


def format_compact_row(parts: Iterable[str], sep: str = ROW_SEP, limit: int = COMPACT_LIMIT) -> str:
    """Join row fragments into one single-line compact row.

    Drops any empty fragment so callers can pass conditional strings
    without sprinkling ``if foo:`` chains. The result is collapsed to a
    single line (any embedded newline becomes a single space) and clamped
    by ``compact_one_line``. The default separator is the em-dash sequence
    ``" — "`` already used by ``server._format_semantic_grep_group`` so a
    future swap-in does not change the printed contract.
    """
    fragments = [p for p in parts if p]
    text = sep.join(fragments)
    if "\n" in text or "\r" in text:
        text = " ".join(text.split())
    return compact_one_line(text, limit)


# --- Path / call dense rows --------------------------------------------------

def format_path_dense(
    aliases: Sequence[str],
    edge_labels: Sequence[str] | None = None,
    default_arrow: str = "->",
) -> str:
    """Render the L3 dense alias chain ``A3 -> A7 -> J1``.

    With ``edge_labels`` the per-hop family is carried inline:
    ``format_path_dense(["A3", "A7", "J1"], ["calls", "refs"])`` →
    ``"A3 -calls-> A7 -refs-> J1"`` (matching the mixed-edge sample in
    ``docs/render-memory.md``). When ``edge_labels`` is omitted the
    section header is expected to declare the edge family, so a plain
    ``->`` arrow keeps each hop short.

    Raises ``ValueError`` when ``edge_labels`` is supplied but the count
    does not match ``len(aliases) - 1``: a mismatch would silently drop a
    hop's provenance, which the alias contract forbids.
    """
    if not aliases:
        return ""
    if edge_labels is not None and len(edge_labels) != max(0, len(aliases) - 1):
        raise ValueError(
            f"edge_labels has {len(edge_labels)} entries but "
            f"{len(aliases)} aliases need {max(0, len(aliases) - 1)} edges"
        )
    if len(aliases) == 1:
        return aliases[0]
    chunks: list[str] = [aliases[0]]
    for i in range(len(aliases) - 1):
        if edge_labels is not None:
            chunks.append(f" -{edge_labels[i]}-> ")
        else:
            chunks.append(f" {default_arrow} ")
        chunks.append(aliases[i + 1])
    return "".join(chunks)


def format_path_dense_header(
    handle: str,
    cost: int,
    hops: int,
    status: str,
    dense: str = "",
) -> str:
    """``[P0] cost 3 hops 3 verified  A3 -> A7 -> J1``.

    ``handle`` is the ``[Pn]`` path handle, ``status`` is a short
    annotation (``verified``, ``hub-pruned``, ...), ``dense`` is the
    alias chain produced by ``format_path_dense`` (empty when the path
    list is degenerate). The double-space gap between the stats prefix
    and the dense chain matches the sample in ``docs/render-memory.md``
    so a follow-up regex/grep on the dense form remains stable.
    """
    head = f"{handle} cost {cost} hops {hops} {status}".strip()
    if dense:
        return f"{head}  {dense}"
    return head


# --- Alias chip --------------------------------------------------------------

def format_alias_chip(handle: str, alias: str, tail: str) -> str:
    """``[3] A3 Render: void - refs 9 - samples L57,L694,+7``.

    L1 chipped form: graph handle, render-memory alias, then the existing
    row tail. ``tail`` is whatever the L0 renderer would have produced
    after the graph handle; this helper just inserts the alias chip
    between them so warm-symbol rows can shorten without re-implementing
    the row body.
    """
    pieces = [handle, alias, tail]
    return " ".join(p for p in pieces if p)


# --- Legend ------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LegendMember:
    """One bucket member as rendered in the legend block.

    ``alias`` is the dense form (``A3``); ``name`` is the symbol's short
    identifier; ``line`` is the 1-based definition line. The legend
    decodes to ``<bucket_label>.<name>@L<line>``, which is what makes a
    dense row reversible without an LSP roundtrip.
    """

    alias: str
    name: str
    line: int


@dataclass(frozen=True, slots=True)
class LegendBucket:
    """One container bucket: ``A=ComfyNodeRenderer.cs::ComfyNodeRenderer``
    plus its rendered members."""

    bucket_alias: str
    bucket_label: str
    members: tuple[LegendMember, ...] = ()


def format_legend_block(
    buckets: Iterable[LegendBucket],
    gen: int | None = None,
    delta: bool = False,
    indent: str = "  ",
) -> str:
    """Render the reversible legend block for a response that uses
    aliases.

    The output is column-aligned: every member listing starts at the same
    column across rows, so the bucket binding and the member chips read
    as separate columns even on terminals without proportional fonts.
    Empty bucket lists return ``""`` so callers can skip emitting a
    header for a silent legend.
    """
    bucket_list = [b for b in buckets if b.members or b.bucket_label]
    if not bucket_list:
        return ""

    head_word = "legend+" if delta else "legend"
    if gen is None:
        head = f"{head_word}:"
    else:
        head = f"{head_word} gen={gen}:"

    bucket_renders = [
        f"{b.bucket_alias}={b.bucket_label}" for b in bucket_list
    ]
    pad_width = max((len(s) for s in bucket_renders), default=0)

    rows = [head]
    for bucket, bucket_render in zip(bucket_list, bucket_renders, strict=True):
        member_chips = [
            f"{m.alias}={m.name}@L{m.line}" for m in bucket.members
        ]
        if member_chips:
            padded = bucket_render.ljust(pad_width)
            rows.append(f"{indent}{padded}  " + "  ".join(member_chips))
        else:
            rows.append(f"{indent}{bucket_render}")
    return "\n".join(rows)


# --- Render-memory protocol stub --------------------------------------------

@dataclass(frozen=True, slots=True)
class LegendIdentity:
    """Renderer-side projection of an alias identity.

    ``docs/render-memory.md`` requires that an alias decode back to:

    ```
    workspace_root, server_label, kind, name, def_path, def_line, def_char
    ```

    This is not the canonical ``render_memory.AliasIdentity``. It is the
    narrowed DTO the legend renderer needs after the alias book has already
    decided what identity an alias points at.
    """

    workspace_root: str
    server_label: str
    kind: str
    name: str
    def_path: str
    def_line: int
    def_char: int


@dataclass(frozen=True, slots=True)
class LegendBinding:
    """Pre-resolved alias binding produced by the alias book.

    Pure transport object: the renderer never mints aliases on its own
    (rule 1 of ``docs/render-memory.md``: aliases are issued by the
    server). Callers hand the renderer a list of ``AliasRecord`` and the
    renderer turns them into the legend block.
    """

    alias: str
    identity: LegendIdentity
    bucket_alias: str
    bucket_label: str


class RenderMemory(Protocol):
    """Minimum surface a future ``render_memory.RenderMemory`` must
    expose to drive renderer compression.

    ``lookup`` returns ``None`` when the alias is unknown so the caller
    can fall back to L0 verbose output rather than guessing — matching
    the "unknown aliases hard-fail" rule in ``docs/render-memory.md``.
    ``aliases_for_response`` returns the records that should appear in
    the response's legend; it never has side effects on the alias book.
    """

    def lookup(self, alias: str) -> LegendBinding | None: ...

    def aliases_for_response(
        self, records: Iterable[LegendBinding]
    ) -> Sequence[LegendBinding]: ...


def legend_buckets_from_records(
    records: Iterable[LegendBinding],
    member_names: dict[str, tuple[str, int]] | None = None,
) -> list[LegendBucket]:
    """Group ``LegendBinding`` instances into ``LegendBucket`` objects
    suitable for ``format_legend_block``.

    ``member_names`` lets callers override the displayed name/line for a
    given alias when the identity's stored name differs from the
    rendered short form (e.g. constructor or operator aliases). When
    omitted the identity's ``name`` and ``def_line`` are used directly.

    Bucket order is preserved by first sighting; member order follows
    the input record order so the legend reads in the same sequence the
    aliases appeared on the wire.
    """
    member_names = member_names or {}
    by_bucket: dict[str, LegendBucket] = {}
    order: list[str] = []
    members_buf: dict[str, list[LegendMember]] = {}
    for rec in records:
        bucket_key = rec.bucket_alias
        if bucket_key not in by_bucket:
            order.append(bucket_key)
            by_bucket[bucket_key] = LegendBucket(
                bucket_alias=rec.bucket_alias,
                bucket_label=rec.bucket_label,
            )
            members_buf[bucket_key] = []
        if rec.alias != rec.bucket_alias:
            name, line = member_names.get(
                rec.alias, (rec.identity.name, rec.identity.def_line)
            )
            members_buf[bucket_key].append(
                LegendMember(alias=rec.alias, name=name, line=line)
            )
    return [
        LegendBucket(
            bucket_alias=by_bucket[k].bucket_alias,
            bucket_label=by_bucket[k].bucket_label,
            members=tuple(members_buf[k]),
        )
        for k in order
    ]
