import httpx


def origin_of(value: str, *, require_origin: bool = False) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise ValueError("Invalid webhook URL") from exc
    if url.scheme not in {"http", "https"} or not url.host:
        raise ValueError("Webhook URL must use HTTP or HTTPS")
    if url.userinfo or url.fragment:
        raise ValueError("Credentials and fragments are not allowed in webhook URLs")
    if require_origin and (url.path not in {"", "/"} or url.query):
        raise ValueError("An allowed origin must contain only scheme, host and port")
    return str(url.copy_with(path="", query=None, fragment=None)).rstrip("/")
