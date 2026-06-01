"""
Learned invoice-layout store.

/data/invoice_layouts.json  —  accumulated regex patterns derived from user training.

Layout learning philosophy:
  The Tauron PDF layout drifts between versions — mainly label wording and
  diacritic rendering changes (e.g. "Pobrano z sieci" → "Pobrane z sieci",
  "Okres rozliczeniowy" → "Okre rozliczeniowy").  Rather than trying to cover
  every variant up front, the add-on lets the user correct one invoice by hand,
  then automatically derives the differing anchors and saves them.

  Built-in patterns (in invoice_parser.py) always take priority; learned
  patterns only activate when the built-ins produce None for a field.

Schema of /data/invoice_layouts.json:
  {
    "schema_version": 1,
    "fields": {
      "imp_total":        ["<regex1>", "<regex2>", ...],
      "energy_peak_net":  [...],
      ...
    },
    "examples": [
      {"trained_at": "...", "invoice_number": "...", "learned_fields": [...]}
    ]
  }
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path('/data/invoice_layouts.json')

# Fields exposed for layout learning — single-line label→number patterns only.
# Billing period and distribution-block fields are handled via the Train form
# (user supplies values directly) but their anchors can also be learned.
LEARNABLE_FIELDS = [
    'imp_total',
    'exp_total',
    'imp_peak',
    'imp_offpeak',
    'energy_peak_net',
    'energy_offpeak_net',
    'amount_due',
    'avg_price',
    'deposit_current',
    'deposit_previous',
    'deposit_used',
    'invoice_number',
    'fixed_mocowa',
    'fixed_abonament',
    'fixed_stalysieciowy',
]


# ── Internal I/O ──────────────────────────────────────────────────────────────

def _empty_doc() -> dict:
    return {'schema_version': 1, 'fields': {}, 'examples': []}


def _atomic_write(path: Path, doc: dict) -> None:
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.rename(path)


def _load_doc(path: Path) -> dict:
    if not path.exists():
        return _empty_doc()
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        bak = path.with_suffix('.json.bak')
        if bak.exists():
            logger.error('invoice_layouts.json corrupt (%s); loading .bak', exc)
            return json.loads(bak.read_text(encoding='utf-8'))
        return _empty_doc()


def _save_doc(doc: dict, path: Path) -> None:
    if path.exists():
        path.rename(path.with_suffix('.json.bak'))
    _atomic_write(path, doc)


# ── Public API ────────────────────────────────────────────────────────────────

def load(path: Path = DEFAULT_PATH) -> dict:
    """Return the full layouts document."""
    return _load_doc(path)


def learned_for(field_key: str, path: Path = DEFAULT_PATH) -> list:
    """Return learned patterns for a field (may be empty)."""
    return _load_doc(path).get('fields', {}).get(field_key, [])


def add_patterns(field_key: str, patterns: list, path: Path = DEFAULT_PATH) -> int:
    """
    Append patterns to a field's learned list, deduplicating.
    Returns the number of newly added patterns.
    """
    if not patterns:
        return 0
    doc = _load_doc(path)
    existing: list = doc.setdefault('fields', {}).setdefault(field_key, [])
    added = 0
    for p in patterns:
        if p not in existing:
            existing.append(p)
            added += 1
    if added:
        _save_doc(doc, path)
        logger.info('Learned %d new pattern(s) for field %s', added, field_key)
    return added


def record_example(meta: dict, path: Path = DEFAULT_PATH) -> None:
    """Append a training-example summary to the examples list."""
    doc = _load_doc(path)
    entry = {'trained_at': datetime.now(timezone.utc).isoformat(), **meta}
    doc.setdefault('examples', []).append(entry)
    _save_doc(doc, path)


def clear(path: Path = DEFAULT_PATH) -> None:
    """Reset all learned patterns (keeps schema)."""
    _save_doc(_empty_doc(), path)
    logger.info('Invoice layouts cleared')


def summary(path: Path = DEFAULT_PATH) -> dict:
    """Return per-field pattern counts and example count."""
    doc = _load_doc(path)
    return {
        'field_counts': {k: len(v) for k, v in doc.get('fields', {}).items() if v},
        'examples': len(doc.get('examples', [])),
    }


# ── Pattern derivation ────────────────────────────────────────────────────────

def _polish_to_str(value: float) -> str:
    """Format a float as it appears in Tauron PDFs (comma separator, up to 5 dp)."""
    # Try integer first (e.g. 215 → "215")
    if value == int(value):
        return str(int(value))
    # Otherwise round-trip through Polish notation
    s = f'{value:.5f}'.rstrip('0').rstrip('.')
    return s.replace('.', ',')


def _escape_anchor(anchor: str) -> str:
    """
    Escape an anchor text for use in a regex pattern.
    Replace each non-alphanumeric character with `.` (covers diacritic mangling
    and whitespace variants), then collapse multiple dots/spaces.
    """
    # Replace each non-ascii-alphanum char with `.`
    escaped = re.sub(r'[^a-zA-Z0-9]', '.', anchor)
    # Collapse runs of dots to `.+`
    escaped = re.sub(r'\.{2,}', '.+', escaped)
    return escaped


def derive_pattern(
    text: str,
    value: float,
    field_key: str,
) -> Optional[str]:
    """
    Find `value` in `text` and extract the preceding label words as an anchor.
    Returns a regex string that re-extracts the value, or None if not derivable.

    The derivation:
    1. Format value as Polish string (e.g. 215 → "215", 0.627 → "0,627")
    2. Scan the text for this value preceded by a word-boundary and followed by
       one of: whitespace, end-of-field (kWh, zl, newline), or end-of-string
    3. Capture the preceding 80 chars and extract the last 2-5 meaningful words
       as the anchor (stop at newline or sentence-ending punctuation)
    4. Build: `<escaped_anchor>` + `\\s+([\\d,]+)` and validate it re-extracts the value
    5. Return the pattern if valid, else None
    """
    val_str = _polish_to_str(value)
    # Build search pattern: value as a standalone token
    val_escaped = re.escape(val_str)
    search_pat = rf'(?<![,\d]){val_escaped}(?![,\d])'

    matches = list(re.finditer(search_pat, text))
    if not matches:
        logger.debug('derive_pattern: value %s not found in text', val_str)
        return None

    for m in matches:
        start = m.start()
        # Grab up to 80 chars before the value
        pre = text[max(0, start - 80):start]
        # Take everything after the last newline (stay on the same line)
        last_nl = pre.rfind('\n')
        if last_nl >= 0:
            pre = pre[last_nl + 1:]
        pre = pre.strip()
        if not pre:
            continue

        # Extract last 2-5 words as anchor (skip numeric tokens at the end)
        tokens = re.findall(r'[^\s]+', pre)
        # Drop trailing numeric / symbol tokens
        while tokens and re.fullmatch(r'[\d,.\-\(\)]+', tokens[-1]):
            tokens = tokens[:-1]
        if not tokens:
            continue
        # Keep at most 5 words
        anchor_tokens = tokens[-5:]
        anchor_raw = ' '.join(anchor_tokens)

        # Build tolerant pattern
        escaped = _escape_anchor(anchor_raw)
        pattern = rf'{escaped}\s+([\d,]+)'

        # Validate: the pattern must extract the value from the same text
        extracted = _first_float_from_pattern(pattern, text)
        if extracted is not None and abs(extracted - value) < 0.001:
            logger.debug('derive_pattern [%s]: anchor=%r pattern=%r', field_key, anchor_raw, pattern)
            return pattern

    logger.debug('derive_pattern: could not derive valid pattern for %s=%s', field_key, val_str)
    return None


def _first_float_from_pattern(pattern: str, text: str) -> Optional[float]:
    """Re-parse a value using a regex pattern (single capture group → float)."""
    try:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(',', '.')
            return float(raw)
    except Exception:
        pass
    return None


def derive_and_store_patterns(
    text: str,
    corrected: dict,
    path: Path = DEFAULT_PATH,
) -> dict:
    """
    For each field in `corrected` that has a non-None float/int value,
    call derive_pattern and add the result to the store.

    `corrected` maps field_key → Python float value.
    Returns { field_key: pattern_or_None } for all attempted fields.
    """
    results = {}
    for field_key, value in corrected.items():
        if value is None or field_key not in LEARNABLE_FIELDS:
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        pat = derive_pattern(text, v, field_key)
        results[field_key] = pat
        if pat:
            add_patterns(field_key, [pat], path)
    return results
