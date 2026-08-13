"""Monkey-patch the upstream ``southern-company-api`` library for the current
Southern Company web auth flow.

The upstream library (v0.7.0) has several issues that prevent successful
authentication with Georgia Power / Southern Company:

1. **Login payload mismatch.** The frontend sends ``rememberUsername``,
   ``staySignedIn``, and a full ``params`` object (mirrored from the page's
   ``data-params`` attribute). The library only sends ``targetPage`` and a
   minimal ``params`` dict — the API accepts this but the response can
   differ, and some account types (notably GPC) may return ``data.token``
   only when the full payload is present.

2. **No browser-like headers.** Southern Company's CDN (Imperva) treats
   requests without a ``User-Agent`` or ``Referer`` as bot traffic, which
   can cause IP bans or silent failures. The library sends no browser
   headers at all.

3. **Double request-verification-token fetch.** The ``request_token``
   property *always* re-fetches the token (no caching), so calling
   ``await self.request_token`` inside ``_get_sc_web_token`` re-hits the
   login page, wasting a request and doubling the bot-detection surface.

4. **Error handling.** The library only checks ``statusCode == 500`` for
   invalid login. The API also returns ``isSuccess: false`` with
   ``data.result`` values (2 = invalid credentials, 3 = password expired)
   that should be handled distinctly.

5. **ScWebToken extraction.** The original regex
   (``NAME='ScWebToken' value='...'``) no longer matches. The token may
   appear in ``data.token``, embedded in ``data.html``, or in
   ``data.returnUrlWithToken`` as a query parameter.

6. **Downstream auth (LoginComplete / JwtToken).** These endpoints also
   need browser headers and the ``Origin`` header.

7. **Service point number.** The upstream hardcodes ``/GPC`` for all
   accounts; we use the account's company code (``GPC``, ``APC``, ``MPC``)
   and handle empty ``meterAndServicePoints`` gracefully.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any
from urllib.parse import unquote

import aiohttp
import jwt
from aiohttp import ClientSession, ContentTypeError
from southern_company_api.account import Account
from southern_company_api.exceptions import (
    CantReachSouthernCompany,
    InvalidLogin,
    NoJwtTokenFound,
    NoRequestTokenFound,
    NoScTokenFound,
)
from southern_company_api.parser import SouthernCompanyAPI

from .const import EMAIL_VALIDATION_URL

_LOGGER = logging.getLogger(__name__)


class EmailValidationRequired(InvalidLogin):
    """Raised when Southern Company requires email validation before login can complete."""


_JWT_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_SC_ATTR_RE = re.compile(
    r"""name\s*=\s*['"]ScWebToken['"][^>]*?value\s*=\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_LOGIN_PAGE_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_LOGIN_API_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json; charset=utf-8",
    "Origin": "https://webauth.southernco.com",
    "Referer": "https://webauth.southernco.com/account/login",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
}

_DOWNSTREAM_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_sc_token(connection: dict[str, Any]) -> str | None:
    """Try every known location for the ScWebToken in the login response.

    The API can return the token in two forms:
    - ``data.html``: an auto-submitting form with a JWT-format ScWebToken
      (this is the one LoginComplete accepts).
    - ``data.token``: an opaque encrypted token (LoginComplete rejects
      this with 500).
    - ``data.returnUrlWithToken``: a URL containing the token as a query
      parameter.

    Prefer the JWT from ``data.html`` first, then fall back to the others.
    """
    data = connection.get("data") or {}

    # 1. JWT-format token embedded in HTML (preferred — LoginComplete accepts this)
    html = data.get("html") or ""
    if isinstance(html, str) and html:
        attr_match = _SC_ATTR_RE.search(html)
        if attr_match:
            candidate = unquote(attr_match.group(1))
            if _JWT_RE.fullmatch(candidate):
                return candidate
        jwt_match = _JWT_RE.search(html)
        if jwt_match:
            return jwt_match.group(0)

    # 2. Token in returnUrlWithToken query parameter
    return_url = data.get("returnUrlWithToken")
    if isinstance(return_url, str) and return_url:
        url_match = re.search(
            r"ScWebToken=([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", return_url
        )
        if url_match:
            return url_match.group(1)
        jwt_match = _JWT_RE.search(return_url)
        if jwt_match:
            return jwt_match.group(0)

    # 3. Direct token field (opaque, may be URL-encoded) — last resort
    token = data.get("token")
    if isinstance(token, str) and token.strip():
        decoded = unquote(token)
        if _JWT_RE.fullmatch(decoded) or len(decoded) > 100:
            return decoded

    return None


async def _patched_get_request_verification_token(session: ClientSession) -> str:
    """Fetch the anti-forgery token from the login page with browser headers."""
    try:
        async with session.get(
            "https://webauth.southernco.com/account/login",
            headers=_LOGIN_PAGE_HEADERS,
        ) as http_response:
            if http_response.status != 200:
                raise CantReachSouthernCompany(
                    f"Login page returned {http_response.status}"
                )
            login_page = await http_response.text()
            matches = re.findall(r'data-aft="(\S+)"', login_page)
    except (CantReachSouthernCompany, NoRequestTokenFound):
        raise
    except Exception as error:
        raise CantReachSouthernCompany() from error
    if len(matches) < 1:
        raise NoRequestTokenFound()
    return matches[0]


@property  # type: ignore[misc]
async def _patched_request_token(self: SouthernCompanyAPI) -> str:
    """Cached request-verification-token property.

    The upstream property re-fetches on every access. This version caches
    in ``self._request_token`` and only re-fetches when it's ``None``.
    """
    if self._request_token is None:
        self._request_token = await _patched_get_request_verification_token(
            self.session
        )
    return self._request_token


async def _patched_get_sc_web_token(self: SouthernCompanyAPI) -> str:
    # Ensure we have a request verification token (cached via patched property)
    if self._request_token is None:
        self._request_token = await _patched_get_request_verification_token(
            self.session
        )

    headers = dict(_LOGIN_API_HEADERS)
    headers["RequestVerificationToken"] = self._request_token

    # Build the login payload. The API requires params.ReturnUrl to be
    # the string "null" (not JSON null) to return the ScWebToken in
    # data.html as an auto-submitting form. With JSON null, it returns
    # an opaque token in data.token that LoginComplete rejects with 500.
    data = {
        "username": self.username,
        "password": self.password,
        "rememberUsername": False,
        "staySignedIn": False,
        "targetPage": 1,
        "params": {"ReturnUrl": "null"},
        "ScWebToken": "",
    }

    async with self.session.post(
        "https://webauth.southernco.com/api/login",
        json=data,
        headers=headers,
    ) as response:
        if response.status != 200:
            raise CantReachSouthernCompany(
                f"Login API returned status {response.status}"
            )
        try:
            connection = await response.json()
        except (ContentTypeError, json.JSONDecodeError) as err:
            # Non-JSON response means the request was intercepted (Imperva
            # bot detection, WAF, or network proxy). This is never an auth
            # failure — raise CantReachSouthernCompany so HA retries instead
            # of prompting for reauth.
            try:
                body = await response.text()
            except Exception:
                body = ""
            preview = body[:200] if body else "(empty)"
            raise CantReachSouthernCompany(
                f"Login API returned non-JSON response (Content-Type: "
                f"{response.headers.get('Content-Type', 'unknown')}). "
                f"Likely blocked by Southern Company bot detection (Imperva). "
                f"Try accessing southernco.com from a browser on the same "
                f"network. Response preview: {preview}"
            ) from err

    # Check for explicit error states
    if not connection.get("isSuccess", False):
        status_code = connection.get("statusCode")
        result = (connection.get("data") or {}).get("result")
        error_msg = (connection.get("data") or {}).get("errorMessage") or ""
        _LOGGER.warning(
            "Southern Company login failed: statusCode=%s result=%s "
            "errorMessage=%s isSuccess=%s",
            status_code,
            result,
            error_msg,
            connection.get("isSuccess"),
        )

        if status_code == 500 or result == 2:
            raise InvalidLogin(
                f"Invalid username/password (result={result}): {error_msg}"
            )
        if result == 3:
            # Password expired — treat as auth failure so HA prompts reauth
            raise InvalidLogin(
                f"Password expired, must be changed on southernco.com: {error_msg}"
            )

        self._sc = None
        keys = sorted((connection.get("data") or {}).keys())
        raise NoScTokenFound(
            f"Login was not successful (statusCode={status_code}, "
            f"result={result}, data keys: {keys}): {error_msg}"
        )

    token = _extract_sc_token(connection)
    if token is None:
        self._sc = None
        keys = sorted((connection.get("data") or {}).keys())
        raise NoScTokenFound(
            f"Login request did not return a sc token (data keys: {keys})"
        )
    self._sc = token

    # Check if the account needs email validation or other action before
    # the ScWebToken can be exchanged for a JWT. The API returns a redirect
    # to /account/validateemail when email validation is required.
    redirect = (connection.get("data") or {}).get("redirect") or ""
    if redirect and "validateemail" in redirect.lower():
        raise EmailValidationRequired(
            f"Email validation required. Visit {EMAIL_VALIDATION_URL}, "
            "validate your email address, then reconfigure this integration."
        )

    # Try to decode as JWT for expiry; if it's not a JWT (opaque encrypted
    # token), fall back to a reasonable default expiry.
    try:
        sc_decoded = jwt.decode(self._sc, options={"verify_signature": False})
        self._sc_expiry = datetime.datetime.fromtimestamp(sc_decoded["exp"])
    except (jwt.DecodeError, KeyError):
        self._sc_expiry = datetime.datetime.now() + datetime.timedelta(hours=1)
        _LOGGER.debug("ScWebToken is not a JWT; using 1-hour default expiry")
    return self._sc


async def _patched_get_southern_jwt_cookie(self: SouthernCompanyAPI) -> str:
    """Exchange the ScWebToken for a SouthernJwtCookie.

    Adds browser headers and the Origin header to the request.
    """
    if await self.sc is None:
        raise CantReachSouthernCompany("Sc token cannot be refreshed")
    data = {"ScWebToken": self._sc}
    headers = dict(_DOWNSTREAM_HEADERS)
    headers["Origin"] = "https://webauth.southernco.com"
    headers["Referer"] = "https://webauth.southernco.com/"
    async with self.session.post(
        "https://customerservice2.southerncompany.com/Account/LoginComplete?"
        "ReturnUrl=/Billing/Home",
        data=data,
        headers=headers,
        allow_redirects=False,
    ) as resp:
        if resp.status != 302:
            await self.authenticate()
            raise NoScTokenFound(
                f"Failed to get secondary ScWebToken: {resp.status} "
                f"{resp.headers} {data} sc_expiry: {self._sc_expiry}"
            )
        swtregex = re.compile(r"SouthernJwtCookie=(\S*);", re.IGNORECASE)
        swtcookies = resp.headers.get("set-cookie")
        if swtcookies:
            swtmatches = swtregex.search(swtcookies)
            if swtmatches and swtmatches.group(1):
                swtoken = swtmatches.group(1)
            else:
                raise NoScTokenFound(
                    "Failed to get secondary ScWebToken: Could not find any "
                    "token matches in headers"
                )
        else:
            raise NoScTokenFound(
                "Failed to get secondary ScWebToken: No cookies were sent back."
            )
    return swtoken


async def _patched_get_jwt(self: SouthernCompanyAPI) -> str:
    """Exchange the SouthernJwtCookie for a ScJwtToken.

    Adds browser headers to the request.
    """
    swtoken = await self._get_southern_jwt_cookie()
    headers = dict(_DOWNSTREAM_HEADERS)
    headers["Cookie"] = f"SouthernJwtCookie={swtoken}"
    headers["Referer"] = "https://customerservice2.southerncompany.com/Billing/Home"
    async with self.session.get(
        "https://customerservice2.southerncompany.com/Account/LoginValidated/JwtToken",
        headers=headers,
    ) as resp:
        if resp.status != 200:
            raise NoJwtTokenFound(
                f"Failed to get JWT: {resp.status} {await resp.text()} {headers}"
            )
        regex = re.compile(r"ScJwtToken=(\S*);", re.IGNORECASE)
        cookies = resp.headers.get("set-cookie")
        if cookies:
            matches = regex.search(cookies)
            if matches and matches.group(1):
                token = matches.group(1)
            else:
                raise NoJwtTokenFound(
                    "Failed to get JWT: Could not find any token matches in headers"
                )
        else:
            raise NoJwtTokenFound("Failed to get JWT: No cookies were sent back.")

    self._jwt = token
    assert self._jwt is not None
    jwt_decoded = jwt.decode(self._jwt, options={"verify_signature": False})
    self._jwt_expiry = datetime.datetime.fromtimestamp(jwt_decoded["exp"])
    return token


async def _patched_get_accounts(self: SouthernCompanyAPI) -> list[Account]:
    """Get all accounts with browser headers."""
    from southern_company_api.company import COMPANY_MAP, Company

    if await self.jwt is None:
        raise CantReachSouthernCompany(
            f"Can't get jwt. Expired and not refreshed jwt: {self._jwt}"
        )
    headers = dict(_DOWNSTREAM_HEADERS)
    headers["Authorization"] = f"bearer {self._jwt}"
    headers["Accept"] = "application/json, text/plain, */*"
    headers["Sec-Fetch-Dest"] = "empty"
    headers["Sec-Fetch-Mode"] = "cors"
    headers["Sec-Fetch-Site"] = "same-site"
    headers["Origin"] = "https://customerservice2.southerncompany.com"
    headers["Referer"] = "https://customerservice2.southerncompany.com/Billing/Home"
    async with self.session.get(
        "https://customerservice2api.southerncompany.com/api/account/getAllAccounts",
        headers=headers,
    ) as resp:
        if resp.status != 200:
            raise CantReachSouthernCompany(
                f"Failed to get accounts: status {resp.status}"
            )
        try:
            account_json = await resp.json()
        except (ContentTypeError, json.JSONDecodeError) as err:
            try:
                error_text = await resp.text()
            except aiohttp.ClientError:
                error_text = str(err)
            raise CantReachSouthernCompany(
                f"Incorrect mimetype while trying to get accounts. {error_text}"
            ) from err
        accounts = []
        try:
            account_list = account_json["Data"]
            for account in account_list:
                accounts.append(
                    Account(
                        name=account["Description"],
                        primary=account["PrimaryAccount"] == "Y",
                        number=account["AccountNumber"],
                        company=COMPANY_MAP.get(account["Company"], Company.GPC),
                        session=self.session,
                    )
                )
        except (KeyError, TypeError) as err:
            raise CantReachSouthernCompany(
                f"Unexpected account data format: {err}"
            ) from err
    for account in accounts:
        await account.get_service_point_number(self._jwt)
    self._accounts = accounts
    return accounts


async def _patched_get_service_point_number(self: Account, jwt: str) -> str:
    """Patched get_service_point_number: uses account's own company code and
    handles an empty meterAndServicePoints list gracefully."""
    headers = {
        "Authorization": f"bearer {jwt}",
        "content-type": "application/json, text/plain, */*",
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Origin": "https://customerservice2.southerncompany.com",
        "Referer": "https://customerservice2.southerncompany.com/Billing/Home",
    }
    company_code = self.company.name  # e.g. "GPC", "APC", "MPC"
    try:
        async with self.session.get(
            f"https://customerservice2api.southerncompany.com/api/MyPowerUsage/"
            f"getMPUBasicAccountInformation/{self.number}/{company_code}",
            headers=headers,
        ) as resp:
            try:
                service_info = await resp.json()
            except (ContentTypeError, json.JSONDecodeError) as err:
                try:
                    error_text = await resp.text()
                except aiohttp.ClientError:
                    error_text = str(err)
                raise CantReachSouthernCompany(
                    f"Incorrect mimetype while trying to get service point number. "
                    f"error:{error_text} Response headers:{resp.headers}"
                ) from err
            points = (service_info.get("Data") or {}).get("meterAndServicePoints") or []
            if points:
                self.service_point_number = points[0]["servicePointNumber"]
            else:
                preview = json.dumps(service_info)[:500]
                _LOGGER.warning(
                    "meterAndServicePoints empty for account %s (company %s); "
                    "monthly/hourly stats unavailable. Response preview: %s",
                    self.number,
                    company_code,
                    preview,
                )
                self.service_point_number = ""
    except aiohttp.ClientConnectorError as err:
        raise CantReachSouthernCompany("Failed to connect to api") from err
    return self.service_point_number or ""


def apply() -> None:
    """Install all patches. Safe to call multiple times."""
    import southern_company_api.parser as sc_parser

    if getattr(SouthernCompanyAPI._get_sc_web_token, "_hacs_patched", False):
        return
    _patched_get_sc_web_token._hacs_patched = True  # type: ignore[attr-defined,misc]
    SouthernCompanyAPI._get_sc_web_token = _patched_get_sc_web_token  # type: ignore[assignment]
    SouthernCompanyAPI._get_southern_jwt_cookie = _patched_get_southern_jwt_cookie  # type: ignore[assignment]
    SouthernCompanyAPI.get_jwt = _patched_get_jwt  # type: ignore[assignment]
    SouthernCompanyAPI.get_accounts = _patched_get_accounts  # type: ignore[assignment]
    SouthernCompanyAPI.request_token = _patched_request_token  # type: ignore[assignment,misc]
    # Patch the module-level function so authenticate()/connect() also use
    # browser headers.
    sc_parser.get_request_verification_token = _patched_get_request_verification_token  # type: ignore[assignment]
    Account.get_service_point_number = _patched_get_service_point_number  # type: ignore[assignment]
    _LOGGER.debug("Applied full auth chain patches")
