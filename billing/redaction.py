"""Conservative log filtering. Never log request bodies, headers or AWS responses."""
import logging
import re

PATTERNS = [
    (re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'), '[REDACTED_ACCESS_KEY]'),
    (re.compile(r'(?i)(authorization|password|secret(?:accesskey|_key)?|sessiontoken|access_token|id_token|refresh_token|cookie|csrftoken)([\s\"\x27:=]+)([^\s,;\"\x27}]+)'), r'\1\2[REDACTED]'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b'), '[REDACTED_JWT]'),
]


def redact(value):
    result = str(value)
    for pattern, replacement in PATTERNS:
        result = pattern.sub(replacement, result)
    return result


class RedactionFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        # Exceptions can contain arbitrary third-party response/request bodies.
        if record.exc_info:
            record.msg += ' [exception type: ' + record.exc_info[0].__name__ + ']'
            record.exc_info = None
            record.exc_text = None
        return True
