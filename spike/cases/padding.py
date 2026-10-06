from __future__ import annotations

from enum import IntEnum

__all__ = ["codes"]


class codes(IntEnum):
    """HTTP status codes and reason phrases

    Status codes from the following RFCs are all observed:

        * RFC 7231: Hypertext Transfer Protocol (HTTP/1.1), obsoletes 2616
        * RFC 6585: Additional HTTP Status Codes
        * RFC 3229: Delta encoding in HTTP
        * RFC 4918: HTTP Extensions for WebDAV, obsoletes 2518
        * RFC 5842: Binding Extensions to WebDAV
        * RFC 7238: Permanent Redirect
        * RFC 2295: Transparent Content Negotiation in HTTP
        * RFC 2774: An HTTP Extension Framework
        * RFC 7540: Hypertext Transfer Protocol Version 2 (HTTP/2)
        * RFC 2324: Hyper Text Coffee Pot Control Protocol (HTCPCP/1.0)
        * RFC 7725: An HTTP Status Code to Report Legal Obstacles
        * RFC 8297: An HTTP Status Code for Indicating Hints
        * RFC 8470: Using Early Data in HTTP
    """

    def __new__(cls, value: int, phrase: str = "") -> codes:
        obj = int.__new__(cls, value)
        obj._value_ = value

        obj.phrase = phrase  # type: ignore[attr-defined]
        return obj

    def __str__(self) -> str:
        return str(self.value)

    @classmethod
    def get_reason_phrase(cls, value: int) -> str:
        try:
            return codes(value).phrase  # type: ignore
        except ValueError:
            return ""

    @classmethod
    def is_informational(cls, value: int) -> bool:
        """
        Returns `True` for 1xx status codes, `False` otherwise.
        """
        return 100 <= value <= 199

    @classmethod
    def is_success(cls, value: int) -> bool:
        """
        Returns `True` for 2xx status codes, `False` otherwise.
        """
        return 200 <= value <= 299

    @classmethod
    def is_redirect(cls, value: int) -> bool:
        """
        Returns `True` for 3xx status codes, `False` otherwise.
        """
        return 300 <= value <= 399

    @classmethod
    def is_client_error(cls, value: int) -> bool:
        """
        Returns `True` for 4xx status codes, `False` otherwise.
        """
        return 400 <= value <= 499

    @classmethod
    def is_server_error(cls, value: int) -> bool:
        """
        Returns `True` for 5xx status codes, `False` otherwise.
        """
        return 500 <= value <= 599

    @classmethod
    def is_error(cls, value: int) -> bool:
        """
        Returns `True` for 4xx or 5xx status codes, `False` otherwise.
        """
        return 400 <= value <= 599

    # informational
    CONTINUE = 100, "Continue"
    SWITCHING_PROTOCOLS = 101, "Switching Protocols"
    PROCESSING = 102, "Processing"
    EARLY_HINTS = 103, "Early Hints"

    # success
    OK = 200, "OK"
    CREATED = 201, "Created"
    ACCEPTED = 202, "Accepted"
    NON_AUTHORITATIVE_INFORMATION = 203, "Non-Authoritative Information"
    NO_CONTENT = 204, "No Content"
    RESET_CONTENT = 205, "Reset Content"
    PARTIAL_CONTENT = 206, "Partial Content"
    MULTI_STATUS = 207, "Multi-Status"
    ALREADY_REPORTED = 208, "Already Reported"
    IM_USED = 226, "IM Used"

    # redirection
    MULTIPLE_CHOICES = 300, "Multiple Choices"
    MOVED_PERMANENTLY = 301, "Moved Permanently"
    FOUND = 302, "Found"
    SEE_OTHER = 303, "See Other"
    NOT_MODIFIED = 304, "Not Modified"
    USE_PROXY = 305, "Use Proxy"
    TEMPORARY_REDIRECT = 307, "Temporary Redirect"
    PERMANENT_REDIRECT = 308, "Permanent Redirect"

    # client error
    BAD_REQUEST = 400, "Bad Request"
    UNAUTHORIZED = 401, "Unauthorized"
    PAYMENT_REQUIRED = 402, "Payment Required"
    FORBIDDEN = 403, "Forbidden"
    NOT_FOUND = 404, "Not Found"
