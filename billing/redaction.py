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


def audit_details(value):
    """Keep event metadata; exclude user content, billing payloads and credentials."""
    if isinstance(value,dict):
        blocked={'fields','service_notes','snapshot','notes','raw','data','body','headers','credentials','parameters'}
        return {key: ('[omitted]' if key.lower() in blocked or any(word in key.lower() for word in ('password','secret','token','cookie','authorization')) else audit_details(item)) for key,item in value.items()}
    if isinstance(value,(list,tuple)):
        return [audit_details(item) for item in value]
    return redact(value) if isinstance(value,str) else value


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
