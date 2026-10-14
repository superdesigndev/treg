"""HTTP adapter for the initial OAuth token exchange."""

from __future__ import annotations

import time

import httpx

from .. import crypto


class HTTPXOAuthExchangePort:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def exchange_code(self, pending, code: str) -> dict:
        """Trade an authorization code for the provider's durable token shape."""
        client_secret = crypto.decrypt(pending.client_secret)
        client_id_param = getattr(pending, "client_id_param", "") or "client_id"
        data = {
            "code": code,
            client_id_param: pending.client_id,
            "redirect_uri": pending.redirect_uri,
            "grant_type": "authorization_code",
        }
        if pending.code_verifier:
            data["code_verifier"] = pending.code_verifier
        kwargs: dict = {}
        if pending.token_endpoint_auth_method == "client_secret_basic":
            kwargs["auth"] = (pending.client_id, client_secret)
        else:
            data["client_secret"] = client_secret
        response = await self.client.post(pending.token_uri, data=data, **kwargs)
        response.raise_for_status()
        token = response.json()
        access = token.get("access_token")
        if not access:
            raise ValueError(
                f"token endpoint returned no access_token: {token.get('error') or token}"
            )
        blob = {
            "access_token": access,
            "token": access,
            "refresh_token": token.get("refresh_token"),
            "client_id": pending.client_id,
            "client_secret": client_secret,
            "token_uri": pending.token_uri,
            "expires_at": time.time() + float(token.get("expires_in") or 3600),
        }
        if client_id_param != "client_id":
            blob["client_id_param"] = client_id_param
        if (pending.token_endpoint_auth_method
                and pending.token_endpoint_auth_method != "client_secret_post"):
            blob["token_endpoint_auth_method"] = pending.token_endpoint_auth_method
        style = getattr(pending, "long_lived_exchange_style", "")
        if style == "instagram":
            return await self._extend_instagram_token(blob)
        if getattr(pending, "long_lived_exchange", False):
            return await self._extend_meta_token(blob)
        return blob

    async def _extend_instagram_token(self, blob: dict) -> dict:
        response = await self.client.get(
            "https://graph.instagram.com/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": blob["client_secret"],
                "access_token": blob["access_token"],
            },
        )
        response.raise_for_status()
        token = response.json()
        access = token.get("access_token")
        if not access:
            raise ValueError(
                f"Instagram long-lived exchange returned no access_token: {token}"
            )
        return {
            **blob,
            "access_token": access,
            "token": access,
            "refresh_token": access,
            "token_uri": "https://graph.instagram.com/refresh_access_token",
            "refresh_method": "GET",
            "refresh_grant_type": "ig_refresh_token",
            "refresh_token_param": "access_token",
            "refresh_include_client": False,
            "expires_at": time.time() + float(token.get("expires_in") or 5_184_000),
        }

    async def _extend_meta_token(self, blob: dict) -> dict:
        response = await self.client.get(
            blob["token_uri"],
            params={
                "grant_type": "fb_exchange_token",
                "client_id": blob["client_id"],
                "client_secret": blob["client_secret"],
                "fb_exchange_token": blob["access_token"],
            },
        )
        if response.status_code != 200:
            return blob
        token = response.json()
        access = token.get("access_token")
        if not access:
            return blob
        return {
            **blob,
            "access_token": access,
            "token": access,
            "expires_at": (
                time.time() + float(token["expires_in"])
                if token.get("expires_in") else None
            ),
        }
