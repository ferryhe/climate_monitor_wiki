"""Local ToolResult-shaped loopbacks, independently authored for consumer tests."""
from __future__ import annotations

import hashlib
from typing import Any, Mapping


class Resolver:
    def __init__(self):
        self.contents: dict[str, bytes] = {}

    def put(self, body: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(body).hexdigest()
        ref = f"memory:{digest}"
        self.contents[ref] = body
        return ref, digest

    def __call__(self, content_ref, content_hash) -> bytes:
        return self.contents[content_ref]


in_process_resolver = Resolver()


def loopback_success_provider(article_id, url) -> Mapping[str, Any]:
    body = f"Climate insurance evidence fixture for {url}."
    ref, digest = in_process_resolver.put(body.encode("utf-8"))
    attempts = [{"tool": "web_http", "data_status": "present",
                 "stop_reason": "usable_data_found", "skipped": False, "reason": None}]
    return {
        "ok": True, "has_data": True, "data_status": "present", "data_count": 1,
        "tool": "web_http", "stop_reason": "usable_data_found", "error": None,
        "attempts": attempts, "warnings": [], "quality_gates": {},
        "meta": {"contract_version": "web-listening-tool-result.v1", "policy_version": "article_content.v1"},
        "data": {"article_id": article_id, "requested_url": url, "final_url": url,
                 "selected_method": "web_http", "content_type": "text/html",
                 "full_text": body, "content_ref": ref, "sha256": digest,
                 "content_hash": digest, "truncated": False,
                 "extraction_metadata": {"status_code": 200, "word_count": len(body.split())},
                 "attempts": attempts},
    }


def loopback_no_content_provider(article_id, url):
    result = loopback_success_provider(article_id, url)
    result.update(has_data=False, data_status="no_content", data_count=0, stop_reason="no_content")
    for field in ("full_text", "content_ref", "sha256", "content_hash"):
        result["data"].pop(field)
    result["data"]["attempts"][0].update(data_status="no_content", stop_reason="no_content")
    return result


def loopback_redirect_provider(article_id, url):
    result = loopback_success_provider(article_id, url)
    result["data"]["final_url"] = url + "/redirected"
    return result


def loopback_failure_provider(article_id, url):
    result = loopback_no_content_provider(article_id, url)
    result.update(ok=False, data_status="error", stop_reason="reader_runtime_error",
                  error={"code": "reader_runtime_error", "message": "fixture failure", "retryable": False})
    result["data"]["attempts"][0].update(data_status="error", stop_reason="reader_runtime_error")
    return result


def loopback_preview_only_provider(article_id, url):
    result = loopback_success_provider(article_id, url)
    body = ("Climate insurance complete body. " * 200).encode("utf-8")
    ref, digest = in_process_resolver.put(body)
    result["data"].pop("full_text")
    result["data"].update(truncated=True, truncated_preview=body[:2000].decode(),
                          content_ref=ref, sha256=digest, content_hash=digest)
    return result


def loopback_damaged_content_ref_provider(article_id, url):
    result = loopback_success_provider(article_id, url)
    result["data"]["content_ref"] = "memory:missing"
    return result


def loopback_hash_mismatch_provider(article_id, url):
    result = loopback_success_provider(article_id, url)
    result["data"].update(sha256="0" * 64, content_hash="0" * 64)
    return result


def loopback_wrong_identity_provider(article_id, url):
    result = loopback_success_provider(article_id, url)
    result["data"]["article_id"] = "wrong-identity"
    return result


def loopback_stealth_skip_provider(article_id, url):
    result = loopback_no_content_provider(article_id, url)
    result["data"]["attempts"].append({"tool": "cloakbrowser", "data_status": "no_content",
        "stop_reason": "policy_disabled", "skipped": True, "reason": "not enabled"})
    return result


for provider in (loopback_success_provider, loopback_no_content_provider,
                 loopback_redirect_provider, loopback_failure_provider,
                 loopback_preview_only_provider, loopback_damaged_content_ref_provider,
                 loopback_hash_mismatch_provider, loopback_wrong_identity_provider,
                 loopback_stealth_skip_provider):
    provider.content_resolver = in_process_resolver
