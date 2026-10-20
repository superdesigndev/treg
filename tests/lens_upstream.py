"""Synthetic Lens provider responses shared by HTTP and real-server browser tests.

Only the upstream relay is replaced. Authentication, authorization, catalog resolution,
reservation/settlement, durable projects and run recovery execute the production code.
This module is test-only and is never imported by the application.
"""
from __future__ import annotations

import json

from treg.application.call.types import UpstreamResponse

QUESTION = "Which project planning tools suit small teams?"
SOURCES = [{"url": "https://acme.example/product", "title": "Acme project planning"}]
ANSWER = "1. Acme helps small teams plan projects.\n2. Rival is another option."
RESEARCH = {
    "answer": {
        "brand": "Acme",
        "description": "Project planning for small teams.",
        "competitors": [{"name": "Rival", "domain": "rival.example",
                         "reason": "Another project planning product."}],
        "questions": [QUESTION],
    },
    "citations": SOURCES,
    "costDollars": {"total": 0.005},
}
CLORO = {"result": {
    "markdown": ANSWER, "sources": SOURCES,
    "entities": [{"type": "brand", "name": "Acme"}, {"type": "brand", "name": "Rival"}],
}}
PERPLEXITY = {
    "cost": 0.005861,
    "tasks": [{"status_code": 20000, "result": [{"items": [{
        "sections": [{"text": ANSWER, "annotations": SOURCES}],
    }]}]}],
}


def response(document, *, status=200, headers=()):
    body = json.dumps(document).encode()

    async def stream():
        yield body

    async def close():
        pass

    return UpstreamResponse(status, ((b"content-type", b"application/json"), *headers), stream(), close)


class LensUpstream:
    def __init__(self):
        self.seen = []

    async def __call__(self, request, upstream_url, tool, secrets, client,
                       drop_params=None, force_identity=False):
        body = b""
        async for chunk in request.body_stream():
            body += chunk
        self.seen.append({"url": upstream_url, "method": request.method,
                          "body": json.loads(body) if body else None})
        if "api.exa.ai/answer" in upstream_url:
            return response(RESEARCH)
        if "cloro" in upstream_url:
            return response(CLORO, headers=((b"x-credits-charged", b"7"),))
        if "dataforseo" in upstream_url and "perplexity" in upstream_url:
            return response(PERPLEXITY)
        raise AssertionError("The Lens fixture refuses every non-allowlisted upstream request.")
