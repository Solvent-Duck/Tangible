import base64
import hashlib
import re
from pathlib import Path

from tangible.workbench.ui import routes

UI = Path(routes.__file__).parent


def test_vendored_htmx_matches_pinned_sri() -> None:
    assert routes.HTMX_URL.startswith("/static/vendor/")
    path = UI / "static" / routes.HTMX_URL.removeprefix("/static/")
    digest = base64.b64encode(hashlib.sha384(path.read_bytes()).digest()).decode()
    assert routes.HTMX_SRI == f"sha384-{digest}"
    assert (path.parent / path.name.replace(".min.js", ".LICENSE")).exists()


def test_templates_load_no_external_scripts() -> None:
    for tpl in (UI / "templates").glob("*.html"):
        for src in re.findall(r"<script[^>]*src=\"([^\"]+)\"", tpl.read_text()):
            assert not re.match(r"(https?:)?//", src), (tpl.name, src)
