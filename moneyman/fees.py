from __future__ import annotations

import base64
import json
import os
import secrets as nonce_secrets
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable


COINBASE_API_HOST = "api.coinbase.com"
TRANSACTION_SUMMARY_PATH = "/api/v3/brokerage/transaction_summary"
TRANSACTION_SUMMARY_URL = f"https://{COINBASE_API_HOST}{TRANSACTION_SUMMARY_PATH}"


class CoinbaseAuthError(RuntimeError):
    pass


class CoinbaseApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class CoinbaseCredentials:
    key_name: str
    private_key: str


@dataclass(frozen=True)
class FeeProfile:
    source: str
    source_status: str
    maker_fee_rate: str
    taker_fee_rate: str
    liquidity_assumption: str
    coinbase_one_advanced_rebate_rate: str
    coinbase_one_monthly_rebate_cap: str
    coinbase_one_monthly_rebate_used: str
    coinbase_one_rebate_currency: str = "USDC"
    pricing_tier: str | None = None
    warnings: tuple[str, ...] = ()
    fee_tier: dict[str, Any] | None = None


@dataclass(frozen=True)
class FeeQuote:
    liquidity: str
    fee_rate: Decimal
    gross_fee_quote: Decimal
    rebate_quote: Decimal
    net_fee_quote: Decimal


def _decimal(value: Any, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc


def _decimal_str(value: Decimal) -> str:
    return format(value.normalize(), "f")


def default_coinbase_one_rebate_rate() -> str:
    return os.environ.get("MONEYMAN_COINBASE_ONE_ADVANCED_REBATE_RATE", "0.25")


def default_coinbase_one_rebate_cap() -> str:
    return os.environ.get("MONEYMAN_COINBASE_ONE_ADVANCED_REBATE_CAP_USDC", "100")


def default_coinbase_one_rebate_used() -> str:
    return os.environ.get("MONEYMAN_COINBASE_ONE_ADVANCED_REBATE_USED_USDC", "0")


def default_liquidity_assumption() -> str:
    return os.environ.get("MONEYMAN_GRIDBOT_LIQUIDITY_ASSUMPTION", "maker")


def load_coinbase_credentials() -> CoinbaseCredentials | None:
    key_name = os.environ.get("COINBASE_API_KEY_NAME") or os.environ.get("COINBASE_API_KEY")
    private_key = os.environ.get("COINBASE_API_PRIVATE_KEY") or os.environ.get("COINBASE_API_SECRET")
    if not key_name or not private_key:
        return None
    return CoinbaseCredentials(key_name=key_name, private_key=private_key)


def _load_private_key(private_key_text: str) -> Any:
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:
        raise CoinbaseAuthError(
            "Coinbase fee auto-pull needs the optional cryptography package. "
            "Run python -m pip install -r requirements.txt."
        ) from exc

    normalized = private_key_text.strip().replace("\\n", "\n")
    if "BEGIN" in normalized:
        try:
            return serialization.load_pem_private_key(normalized.encode("utf-8"), password=None)
        except ValueError as exc:
            raise CoinbaseAuthError("Could not parse Coinbase private key PEM.") from exc

    try:
        decoded = base64.b64decode(normalized, validate=True)
    except Exception as exc:
        raise CoinbaseAuthError(
            "Coinbase private key was not PEM and was not valid base64."
        ) from exc

    try:
        return serialization.load_der_private_key(decoded, password=None)
    except ValueError as exc:
        raise CoinbaseAuthError(
            "Could not parse Coinbase private key as ECDSA PEM or DER. "
            "Coinbase App / Advanced Trade APIs require an ECDSA key for JWT auth; "
            "Ed25519 or raw secret bytes will not work for this endpoint."
        ) from exc


def build_coinbase_rest_jwt(
    credentials: CoinbaseCredentials,
    method: str,
    request_path: str,
    now: int | None = None,
) -> str:
    try:
        import jwt
    except ImportError as exc:
        raise CoinbaseAuthError(
            "Coinbase fee auto-pull needs the optional PyJWT package. "
            "Run python -m pip install -r requirements.txt."
        ) from exc

    issued_at = int(time.time() if now is None else now)
    uri = f"{method.upper()} {COINBASE_API_HOST}{request_path}"
    payload = {
        "sub": credentials.key_name,
        "iss": "cdp",
        "nbf": issued_at,
        "exp": issued_at + 120,
        "uri": uri,
    }
    headers = {"kid": credentials.key_name, "nonce": nonce_secrets.token_hex(16)}
    private_key = _load_private_key(credentials.private_key)
    token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)
    return str(token)


def _fetch_json_with_token(url: str, token: str, timeout_seconds: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CoinbaseApiError(f"Coinbase API returned HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise CoinbaseApiError(f"Coinbase API request failed: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise CoinbaseApiError("Coinbase transaction summary response was not a JSON object")
    return payload


def fetch_coinbase_transaction_summary(
    credentials: CoinbaseCredentials | None = None,
    timeout_seconds: int = 30,
    fetch_json: Callable[[str, str, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved_credentials = credentials or load_coinbase_credentials()
    if resolved_credentials is None:
        raise CoinbaseAuthError(
            "Missing Coinbase API credentials. Set COINBASE_API_KEY_NAME or COINBASE_API_KEY, "
            "and COINBASE_API_PRIVATE_KEY or COINBASE_API_SECRET."
        )
    token = build_coinbase_rest_jwt(resolved_credentials, "GET", TRANSACTION_SUMMARY_PATH)
    fetcher = fetch_json or _fetch_json_with_token
    return fetcher(TRANSACTION_SUMMARY_URL, token, timeout_seconds)


def manual_fee_profile(
    fee_rate: str = "0.006",
    maker_fee_rate: str | None = None,
    taker_fee_rate: str | None = None,
    liquidity_assumption: str = "maker",
    coinbase_one_advanced_rebate_rate: str = "0.25",
    coinbase_one_monthly_rebate_cap: str = "100",
    coinbase_one_monthly_rebate_used: str = "0",
    source: str = "manual",
    source_status: str = "manual",
    warnings: tuple[str, ...] = (),
) -> FeeProfile:
    liquidity = liquidity_assumption.lower()
    if liquidity not in {"maker", "taker"}:
        raise ValueError("liquidity_assumption must be maker or taker")
    maker = maker_fee_rate or fee_rate
    taker = taker_fee_rate or fee_rate
    _decimal(maker, "maker_fee_rate")
    _decimal(taker, "taker_fee_rate")
    _decimal(coinbase_one_advanced_rebate_rate, "coinbase_one_advanced_rebate_rate")
    _decimal(coinbase_one_monthly_rebate_cap, "coinbase_one_monthly_rebate_cap")
    _decimal(coinbase_one_monthly_rebate_used, "coinbase_one_monthly_rebate_used")
    return FeeProfile(
        source=source,
        source_status=source_status,
        maker_fee_rate=str(maker),
        taker_fee_rate=str(taker),
        liquidity_assumption=liquidity,
        coinbase_one_advanced_rebate_rate=str(coinbase_one_advanced_rebate_rate),
        coinbase_one_monthly_rebate_cap=str(coinbase_one_monthly_rebate_cap),
        coinbase_one_monthly_rebate_used=str(coinbase_one_monthly_rebate_used),
        warnings=warnings,
    )


def fee_profile_from_coinbase_transaction_summary(
    payload: dict[str, Any],
    fallback_fee_rate: str = "0.006",
    liquidity_assumption: str = "maker",
    coinbase_one_advanced_rebate_rate: str = "0.25",
    coinbase_one_monthly_rebate_cap: str = "100",
    coinbase_one_monthly_rebate_used: str = "0",
) -> FeeProfile:
    fee_tier = payload.get("fee_tier")
    if not isinstance(fee_tier, dict):
        raise CoinbaseApiError("Coinbase transaction summary did not include fee_tier")
    maker = str(fee_tier.get("maker_fee_rate") or fallback_fee_rate)
    taker = str(fee_tier.get("taker_fee_rate") or fallback_fee_rate)
    profile = manual_fee_profile(
        fee_rate=fallback_fee_rate,
        maker_fee_rate=maker,
        taker_fee_rate=taker,
        liquidity_assumption=liquidity_assumption,
        coinbase_one_advanced_rebate_rate=coinbase_one_advanced_rebate_rate,
        coinbase_one_monthly_rebate_cap=coinbase_one_monthly_rebate_cap,
        coinbase_one_monthly_rebate_used=coinbase_one_monthly_rebate_used,
        source="coinbase_transaction_summary",
        source_status="pulled",
        warnings=(),
    )
    profile_payload = asdict(profile)
    profile_payload["pricing_tier"] = fee_tier.get("pricing_tier")
    profile_payload["fee_tier"] = fee_tier
    return FeeProfile(**profile_payload)


def resolve_fee_profile(
    source: str = "manual",
    fee_rate: str = "0.006",
    maker_fee_rate: str | None = None,
    taker_fee_rate: str | None = None,
    liquidity_assumption: str = "maker",
    coinbase_one_advanced_rebate_rate: str = "0.25",
    coinbase_one_monthly_rebate_cap: str = "100",
    coinbase_one_monthly_rebate_used: str = "0",
    timeout_seconds: int = 30,
    fetch_summary: Callable[[], dict[str, Any]] | None = None,
) -> FeeProfile:
    normalized_source = source.lower()
    if normalized_source not in {"manual", "auto", "coinbase"}:
        raise ValueError("fee source must be manual, auto, or coinbase")
    if normalized_source == "manual":
        return manual_fee_profile(
            fee_rate=fee_rate,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            liquidity_assumption=liquidity_assumption,
            coinbase_one_advanced_rebate_rate=coinbase_one_advanced_rebate_rate,
            coinbase_one_monthly_rebate_cap=coinbase_one_monthly_rebate_cap,
            coinbase_one_monthly_rebate_used=coinbase_one_monthly_rebate_used,
        )

    try:
        if fetch_summary is not None:
            payload = fetch_summary()
        else:
            if load_coinbase_credentials() is None:
                raise CoinbaseAuthError("Coinbase API credentials are not set in this process")
            payload = fetch_coinbase_transaction_summary(timeout_seconds=timeout_seconds)
        return fee_profile_from_coinbase_transaction_summary(
            payload,
            fallback_fee_rate=fee_rate,
            liquidity_assumption=liquidity_assumption,
            coinbase_one_advanced_rebate_rate=coinbase_one_advanced_rebate_rate,
            coinbase_one_monthly_rebate_cap=coinbase_one_monthly_rebate_cap,
            coinbase_one_monthly_rebate_used=coinbase_one_monthly_rebate_used,
        )
    except Exception as exc:
        if normalized_source == "coinbase":
            raise
        warning = f"Coinbase fee auto-pull failed: {exc}. Used manual fee rates."
        return manual_fee_profile(
            fee_rate=fee_rate,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            liquidity_assumption=liquidity_assumption,
            coinbase_one_advanced_rebate_rate=coinbase_one_advanced_rebate_rate,
            coinbase_one_monthly_rebate_cap=coinbase_one_monthly_rebate_cap,
            coinbase_one_monthly_rebate_used=coinbase_one_monthly_rebate_used,
            source="manual_fallback",
            source_status="auto_pull_failed",
            warnings=(warning,),
        )


class FeeAccumulator:
    def __init__(self, profile: FeeProfile) -> None:
        self.profile = profile
        self.rebate_rate = _decimal(profile.coinbase_one_advanced_rebate_rate, "rebate_rate")
        self.monthly_cap = _decimal(profile.coinbase_one_monthly_rebate_cap, "monthly_cap")
        self.monthly_used = _decimal(profile.coinbase_one_monthly_rebate_used, "monthly_used")
        self.gross_fees_quote = Decimal("0")
        self.rebates_quote = Decimal("0")
        self.net_fees_quote = Decimal("0")

    def rate_for(self, liquidity: str | None = None) -> Decimal:
        resolved = (liquidity or self.profile.liquidity_assumption).lower()
        if resolved == "maker":
            return _decimal(self.profile.maker_fee_rate, "maker_fee_rate")
        if resolved == "taker":
            return _decimal(self.profile.taker_fee_rate, "taker_fee_rate")
        raise ValueError("liquidity must be maker or taker")

    def gross_fee_for(self, notional_quote: Decimal, liquidity: str | None = None) -> Decimal:
        return notional_quote * self.rate_for(liquidity)

    def quote(self, notional_quote: Decimal, liquidity: str | None = None) -> FeeQuote:
        resolved = (liquidity or self.profile.liquidity_assumption).lower()
        rate = self.rate_for(resolved)
        gross = notional_quote * rate
        rebate_target = gross * self.rebate_rate
        remaining_cap = max(Decimal("0"), self.monthly_cap - self.monthly_used)
        rebate = min(rebate_target, remaining_cap)
        net = gross - rebate
        self.monthly_used += rebate
        self.gross_fees_quote += gross
        self.rebates_quote += rebate
        self.net_fees_quote += net
        return FeeQuote(
            liquidity=resolved,
            fee_rate=rate,
            gross_fee_quote=gross,
            rebate_quote=rebate,
            net_fee_quote=net,
        )


def zero_fee_quote(liquidity: str = "maker") -> FeeQuote:
    return FeeQuote(
        liquidity=liquidity,
        fee_rate=Decimal("0"),
        gross_fee_quote=Decimal("0"),
        rebate_quote=Decimal("0"),
        net_fee_quote=Decimal("0"),
    )


def fee_profile_to_report(profile: FeeProfile) -> dict[str, Any]:
    report = asdict(profile)
    report["warnings"] = list(profile.warnings)
    return report
