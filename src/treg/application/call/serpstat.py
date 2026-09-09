"""Serpstat's JSON-RPC envelope as billing evidence: rows returned, or an error that costs nothing.

Pure interpretation of the unmodified response body. Prices stay in the catalog YAML and the
per-row price arrives as `MarketplaceCall.unit_micro`; nothing here touches the ledger.

Serpstat meters API credits ("lines") per RESULT ROW on its list methods and answers every failure
as HTTP 200 with an `error` object instead of `result`. Verified live 2026-09-09 against the
account's own `SerpstatLimitsProcedure.getStats` meter: an error envelope (`error.code -32000`)
moved the meter by 0 lines while treg settled the 20-row estimate, and a `getKeywordTop` that
returned 12 rows cost exactly 12 lines while treg settled 20 credits. The estimate is therefore
never the charge here; the envelope is.
"""

from __future__ import annotations


def _keyed_entries(obj: dict) -> int:
    """Rows of a `result` keyed by the thing asked about (`{"a.example": {...}, "b.example": {...}}`,
    the per-input methods' shape). `summary_info` is the credit meter, never a row."""
    return sum(1 for key, value in obj.items()
               if key != "summary_info" and isinstance(value, (dict, list)))


def billed_rows(doc) -> int | None:
    """How many rows this envelope bills, or None when the shape is not one Serpstat documents.

      - a top-level `error` object: 0. Serpstat charges nothing for a request it rejected, whatever
        the HTTP status says.
      - `result.data[]` (most list methods and the batch domain overview): its length.
      - `result.data.top[]` (`getKeywordTop`, nested one level deeper): its length.
      - `result.data` or `result` as an object keyed by the input (per-keyword methods): the entries.
      - any of those with no rows at all: 1. Serpstat documents a 1-credit minimum on a served
        empty result; it is the documented floor, not a live-verified number, and the safe
        direction for a request the provider did serve.
      - anything else (no `result`, an unexpected type): None, and the caller keeps the estimate.
    """
    if not isinstance(doc, dict):
        return None
    if doc.get("error"):
        return 0
    result = doc.get("result")
    if isinstance(result, list):
        rows = len(result)
    elif isinstance(result, dict):
        if "data" in result:
            data = result["data"]
            if isinstance(data, list):
                rows = len(data)
            elif isinstance(data, dict):
                top = data.get("top")
                rows = len(top) if isinstance(top, list) else _keyed_entries(data)
            else:
                return None
        else:
            rows = _keyed_entries(result)
    else:
        return None
    return max(1, rows)
