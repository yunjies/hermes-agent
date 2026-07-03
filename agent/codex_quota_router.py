"""Codex quota-based model routing for new Hermes sessions.

The router is intentionally narrow: it is called by runtime provider
resolution only for fresh ``provider=auto`` sessions. Explicit providers,
explicit endpoints, and target-model resolution keep their normal behavior.

Configuration is via environment variables:

``HERMES_CODEX_QUOTA_THRESHOLD``
    Minimum remaining quota percent required to use the primary route.
``HERMES_CODEX_ROUTING_PRIMARY_PROVIDER``
    Provider when Codex quota is sufficient. Defaults to ``openai-codex``.
``HERMES_CODEX_ROUTING_PRIMARY_MODEL``
    Model when Codex quota is sufficient. Defaults to ``gpt-5.5``.
``HERMES_CODEX_ROUTING_FALLBACK_PROVIDER``
    Provider when quota is insufficient or unavailable. Defaults to ``deepseek``.
``HERMES_CODEX_ROUTING_FALLBACK_MODEL``
    Model when quota is insufficient or unavailable. Defaults to
    ``deepseek-v4-flash``.
``HERMES_CODEX_ROUTER_ENABLED_PROFILES``
    Optional comma-separated profile allowlist. Empty, ``*``, or ``all`` means
    every fresh session without an explicit provider may route. When set, only
    listed profiles use the router.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_QUOTA_THRESHOLD_PCT = 10
_DEFAULT_PRIMARY_PROVIDER = "openai-codex"
_DEFAULT_PRIMARY_MODEL = "gpt-5.5"
_DEFAULT_FALLBACK_PROVIDER = "deepseek"
_DEFAULT_FALLBACK_MODEL = "deepseek-v4-flash"
_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


@dataclass(frozen=True)
class QuotaRoutingResult:
    provider: str
    model: str
    codex_available: bool = False
    fallback: bool = False
    error: Optional[str] = None
    used_percent: Optional[float] = None


def _get_config() -> tuple[int, str, str, str, str]:
    threshold_raw = os.environ.get("HERMES_CODEX_QUOTA_THRESHOLD", "").strip()
    try:
        threshold = int(threshold_raw) if threshold_raw else _DEFAULT_QUOTA_THRESHOLD_PCT
    except (TypeError, ValueError):
        threshold = _DEFAULT_QUOTA_THRESHOLD_PCT

    primary_provider = (
        os.environ.get("HERMES_CODEX_ROUTING_PRIMARY_PROVIDER", "").strip()
        or _DEFAULT_PRIMARY_PROVIDER
    )
    primary_model = (
        os.environ.get("HERMES_CODEX_ROUTING_PRIMARY_MODEL", "").strip()
        or _DEFAULT_PRIMARY_MODEL
    )
    fallback_provider = (
        os.environ.get("HERMES_CODEX_ROUTING_FALLBACK_PROVIDER", "").strip()
        or _DEFAULT_FALLBACK_PROVIDER
    )
    fallback_model = (
        os.environ.get("HERMES_CODEX_ROUTING_FALLBACK_MODEL", "").strip()
        or _DEFAULT_FALLBACK_MODEL
    )
    return threshold, primary_provider, primary_model, fallback_provider, fallback_model


def _parse_profile_allowlist(raw: str) -> Optional[set[str]]:
    value = (raw or "").strip()
    if not value:
        return None
    if value.lower() in {"*", "all"}:
        return None
    normalized = value.replace(";", ",").replace(" ", ",")
    return {item.strip().lower() for item in normalized.split(",") if item.strip()}


def get_current_profile_name() -> str:
    for env_name in ("HERMES_PROFILE", "HERMES_ACTIVE_PROFILE", "HERMES_PROFILE_NAME"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value.lower()

    home = os.environ.get("HERMES_HOME", "").strip() or os.environ.get("HOME", "").strip()
    if not home:
        return ""
    try:
        path = Path(home).resolve()
        if path.parent.name == "profiles":
            return path.name.lower()
        if str(path) == "/opt/data":
            return "default"
        return path.name.lower()
    except Exception:
        return ""


def is_codex_quota_router_enabled_for_current_profile() -> bool:
    raw = (
        os.environ.get("HERMES_CODEX_ROUTER_ENABLED_PROFILES", "").strip()
        or os.environ.get("HERMES_CODEX_ROUTER_PROFILE_ALLOWLIST", "").strip()
    )
    allowlist = _parse_profile_allowlist(raw)
    if allowlist is None:
        return True
    profile = get_current_profile_name()
    return bool(profile and profile.lower() in allowlist)


def _resolve_codex_wham_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/") or _CODEX_BASE_URL
    if normalized.endswith("/codex"):
        normalized = normalized[: -len("/codex")]
    if "/backend-api" in normalized:
        return normalized + "/wham/usage"
    return normalized + "/api/codex/usage"


def _has_codex_credentials() -> bool:
    return bool(_fetch_codex_access_token())


def _fetch_codex_access_token() -> Optional[str]:
    def _pool_token() -> Optional[str]:
        try:
            from hermes_cli.auth import _pool_codex_access_token

            return str(_pool_codex_access_token() or "").strip() or None
        except Exception:
            return None

    try:
        from hermes_cli.auth import resolve_codex_runtime_credentials

        creds = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        token = str(creds.get("api_key", "") or "").strip()
        if token:
            return token
    except Exception:
        pass
    return _pool_token()


def check_codex_quota() -> Optional[float]:
    token = _fetch_codex_access_token()
    if not token:
        logger.debug("codex_quota_router: no Codex credentials available")
        return None

    try:
        from hermes_cli.auth import _read_codex_tokens

        try:
            token_data = _read_codex_tokens(lazy=True)
        except TypeError:
            token_data = _read_codex_tokens()
        tokens = token_data.get("tokens") or {}
        account_id = str(tokens.get("account_id", "") or "").strip()
        base_url = os.environ.get("HERMES_CODEX_BASE_URL", "").strip() or _CODEX_BASE_URL

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "hermes-agent",
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id

        with httpx.Client(timeout=10.0) as client:
            response = client.get(_resolve_codex_wham_url(base_url), headers=headers)
            response.raise_for_status()

        payload = response.json() or {}
        primary_window = (payload.get("rate_limit") or {}).get("primary_window") or {}
        used_percent = primary_window.get("used_percent")
        if used_percent is None:
            logger.debug("codex_quota_router: no primary_window.used_percent in response")
            return None
        return float(used_percent)
    except httpx.TimeoutException:
        logger.warning("codex_quota_router: timeout fetching Codex quota")
        return None
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "codex_quota_router: HTTP %s fetching Codex quota",
            exc.response.status_code,
        )
        return None
    except Exception as exc:
        logger.warning("codex_quota_router: error checking Codex quota: %s", exc)
        return None


def resolve_codex_quota_routed_provider() -> QuotaRoutingResult:
    (
        threshold_pct,
        primary_provider,
        primary_model,
        fallback_provider,
        fallback_model,
    ) = _get_config()

    if not _has_codex_credentials():
        return QuotaRoutingResult(
            provider=fallback_provider,
            model=fallback_model,
            fallback=True,
            error="No Codex credentials available",
        )

    used_percent = check_codex_quota()
    if used_percent is None:
        return QuotaRoutingResult(
            provider=fallback_provider,
            model=fallback_model,
            fallback=True,
            error="Codex quota check failed (fail-closed)",
        )

    remaining_pct = 100.0 - used_percent
    if remaining_pct > threshold_pct:
        return QuotaRoutingResult(
            provider=primary_provider,
            model=primary_model,
            codex_available=True,
            fallback=False,
            used_percent=used_percent,
        )

    return QuotaRoutingResult(
        provider=fallback_provider,
        model=fallback_model,
        fallback=True,
        used_percent=used_percent,
    )
