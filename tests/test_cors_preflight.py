"""Regression test for the CORS-preflight auth bypass.

AuthMiddleware is the outermost middleware, so it used to 401 the credential-less
OPTIONS preflight before CORSMiddleware could answer it -- which blocks every
cross-origin browser/WebView client before the real request is ever sent. The
fix lets a genuine preflight through; `is_cors_preflight` is the pure predicate
it uses. Guard it so the bypass can't silently regress.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.middleware import is_agentide_preview_preflight, is_cors_preflight


def test_genuine_preflight_is_detected():
    assert is_cors_preflight("OPTIONS", {"access-control-request-method": "POST"}) is True


def test_bare_options_is_not_a_preflight():
    # OPTIONS without Access-Control-Request-Method must NOT bypass auth.
    assert is_cors_preflight("OPTIONS", {}) is False


def test_real_methods_are_never_preflight():
    headers = {"access-control-request-method": "POST"}
    for method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        assert is_cors_preflight(method, headers) is False


def test_sandboxed_agentide_capability_preflight_is_detected():
    path = (
        "/api/agentide/ide-1/preview-token/"
        + ("a" * 64)
        + "/api/signup"
    )
    headers = {
        "origin": "null",
        "access-control-request-method": "POST",
        "access-control-request-headers": "content-type",
    }
    assert is_agentide_preview_preflight("OPTIONS", path, headers) is True


def test_null_origin_is_not_globally_trusted():
    headers = {
        "origin": "null",
        "access-control-request-method": "POST",
    }
    assert (
        is_agentide_preview_preflight(
            "OPTIONS",
            "/api/chat",
            headers,
        )
        is False
    )


def test_agentide_owner_preview_does_not_get_null_origin_bypass():
    headers = {
        "origin": "null",
        "access-control-request-method": "POST",
    }
    assert (
        is_agentide_preview_preflight(
            "OPTIONS",
            "/api/agentide/ide-1/preview/api/signup",
            headers,
        )
        is False
    )
