"""PDF text and file-trait extraction with pypdf. Pure functions of the bytes."""

import io
import re
from datetime import datetime
from typing import Any

from pypdf import PdfReader

_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
_SUBSET = re.compile(r"^[A-Z]{6}\+")
MAX_TRAIT_LEN = 100


class PdfError(ValueError):
    """The bytes are not a readable PDF."""


def _open(data: bytes) -> PdfReader:
    try:
        return PdfReader(io.BytesIO(data))
    except Exception as e:  # pypdf raises many types on malformed input
        raise PdfError(f"not a readable PDF: {type(e).__name__}") from e


def _clean(v: Any) -> str:
    """Metadata is attacker-controlled: strip control characters and cap the length."""
    return _CTRL.sub(" ", str(v)).strip()[:MAX_TRAIT_LEN]


def extract_text(data: bytes) -> str:
    """All text the PDF draws, including invisible (white, tiny) text. Pages joined by newline."""
    reader = _open(data)
    if reader.is_encrypted:
        try:
            ok = reader.decrypt("")
        except Exception as e:
            raise PdfError("PDF is encrypted") from e
        if not ok:
            raise PdfError("PDF is encrypted")
    try:
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        raise PdfError(f"text extraction failed: {type(e).__name__}") from e


def _fonts(reader: PdfReader) -> list[str]:
    names: set[str] = set()
    try:
        for page in reader.pages:
            res: Any = page.get("/Resources")
            res = res.get_object() if res is not None else {}
            fonts: Any = res.get("/Font")
            fonts = fonts.get_object() if fonts is not None else {}
            for f in fonts.values():
                base = f.get_object().get("/BaseFont")
                if base:
                    names.add(_SUBSET.sub("", str(base).lstrip("/")))
    except Exception:
        pass
    return sorted(names)


def _dates(reader: PdfReader) -> tuple[datetime | None, datetime | None]:
    md = reader.metadata
    if md is None:
        return None, None
    try:
        created = md.creation_date
    except Exception:
        created = None
    try:
        modified = md.modification_date
    except Exception:
        modified = None
    return created, modified


def _naive(d: datetime) -> datetime:
    return d.replace(tzinfo=None) if d.tzinfo else d


def file_traits(data: bytes) -> dict[str, str]:
    """String-valued traits. Keys: producer, creator, creation_date, mod_date,
    modified_after_creation, fonts, page_count, has_incremental_updates, encrypted.
    Values from document metadata are control-stripped and capped at 100 characters."""
    reader = _open(data)
    md = reader.metadata
    created, modified = _dates(reader)
    after = bool(created and modified and _naive(modified) > _naive(created))
    incremental = data.count(b"%%EOF") > 1 or data.count(b"startxref") > 1
    return {
        "producer": _clean(md.producer or "") if md else "",
        "creator": _clean(md.creator or "") if md else "",
        "creation_date": _clean(md.get("/CreationDate") or "") if md else "",
        "mod_date": _clean(md.get("/ModDate") or "") if md else "",
        "modified_after_creation": "true" if after else "false",
        "fonts": _clean(",".join(_fonts(reader))),
        "page_count": str(len(reader.pages)),
        "has_incremental_updates": "true" if incremental else "false",
        "encrypted": "true" if reader.is_encrypted else "false",
    }
