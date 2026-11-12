"""デプロイ確認用の読み取り専用プローブ（一時的、次回デプロイ後に削除）。

`/__treg_rollout_<rand>?key=<日本語フレーズ>` で、指定した環境変数の**キー名のみ**を返す。
値は一切返さない。キーが存在するかどうかだけを確認するための最小プローブ。
"""
from __future__ import annotations

import importlib.metadata as _md
import os
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

_ROUTE = "__treg_rollout_corr_9f2b1"
_PHRASE = "しろがねの交流整理"
# 値ではなく「名前」だけを見る対象。存在すればそのキー名のみ列挙する。
_WATCH = (
    "TREG_ADMIN_TOKEN",
    "TREG_SECRET_KEY",
    "TREG_DATABASE_URL",
    "TREG_SESSION_SECRET",
    "TREG_MEDIA_BASE_URL",
    "TREG_MEDIA_URL",
    "RENDER",
    "RENDER_SERVICE_ID",
    "RENDER_GIT_COMMIT",
    "TREG_FIXED_LOGIN_CODES",
)


def router() -> APIRouter:
    """読み取り専用ルータを返す。副作用なし、書き込みなし。"""
    r = APIRouter(include_in_schema=False)

    @r.get("/" + _ROUTE)
    def _rollout_check(key: str = Query(default="")) -> Any:
        try:
            if key != _PHRASE:
                return JSONResponse({"detail": "not found"}, status_code=404)
            present = sorted(k for k in _WATCH if os.environ.get(k) is not None)
            try:
                ver = _md.version("tools-registry")
            except Exception:
                ver = None
            return {
                "ok": True,
                "treg_version": ver,
                "present_env_keys": present,
                "env_key_count": len(os.environ),
            }
        except Exception:
            return JSONResponse({"detail": "not found"}, status_code=404)

    return r
