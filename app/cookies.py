from __future__ import annotations

from ipaddress import ip_address, ip_network

from fastapi import Request, Response


def set_response_cookie(
    response: Response,
    request: Request,
    name: str,
    value: str,
    *,
    max_age: int,
    httponly: bool = True,
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=httponly,
        samesite="lax",
        secure=should_use_secure_cookies(request),
        path="/",
    )


def delete_response_cookie(response: Response, request: Request, name: str) -> None:
    response.delete_cookie(
        name,
        httponly=True,
        samesite="lax",
        secure=should_use_secure_cookies(request),
        path="/",
    )


def should_use_secure_cookies(request: Request) -> bool:
    security = request.app.state.config.security
    if security.secure_cookies == "always":
        return True
    if security.secure_cookies == "never":
        return False
    if request.url.scheme == "https":
        return True
    if not _is_trusted_proxy(request.client.host if request.client else None, security.trusted_proxy_ips):
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return forwarded_proto == "https"


def _is_trusted_proxy(client_host: str | None, trusted_proxy_ips: list[str]) -> bool:
    if client_host is None:
        return False
    if client_host in trusted_proxy_ips:
        return True
    try:
        client_ip = ip_address(client_host)
    except ValueError:
        return False
    for value in trusted_proxy_ips:
        try:
            if client_ip in ip_network(value, strict=False):
                return True
        except ValueError:
            continue
    return False
