"""parsers/exceptions.py — custom parser exception hierarchy."""


class ParserException(Exception):
    """Base class for all parser errors."""


class CaptchaFailedException(ParserException):
    """Raised when captcha could not be solved automatically."""


class PageLoadException(ParserException):
    """Raised when the target page fails to load within timeout."""


class NoReviewsFoundException(ParserException):
    """Raised when the page loaded but zero reviews were found."""
