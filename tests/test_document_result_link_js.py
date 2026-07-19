"""Static regression checks for authoritative document-result links.

chatRenderer is browser-coupled, so follow the repository's existing JS
source-test pattern rather than importing the module into pytest.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHAT_RENDERER = (ROOT / "static/js/chatRenderer.js").read_text()


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    depth = 0
    index = src.index("{", start)
    while index < len(src):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
        index += 1
    raise AssertionError(f"unbalanced braces after {signature!r}")


RESOLVER = _function_body(
    CHAT_RENDERER, "function resolveDocumentPlaceholderLinks(text, metadata)"
)


def test_single_stored_document_result_replaces_a_stale_uuid_link():
    assert "uniqueDocIds.length === 1" in RESOLVER
    assert (
        "/#document-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        "[0-9a-f]{4}-[0-9a-f]{12}\\b/gi"
    ) in RESOLVER
    assert "`#document-${uniqueDocIds[0]}`" in RESOLVER
