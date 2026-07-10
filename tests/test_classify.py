from synapse.cascade.breaker import ErrorKind
from synapse.cascade.classify import classify_error


def test_google_429_per_minute_with_retry_delay():
    body = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"},
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {
                            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                            "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                        }
                    ],
                },
            ],
        }
    }
    kind, retry_after = classify_error(429, body, {})
    assert kind == ErrorKind.RPM
    assert retry_after == 34.0


def test_google_429_per_day():
    body = {
        "error": {
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
                }
            ]
        }
    }
    kind, retry_after = classify_error(429, body, {})
    assert kind == ErrorKind.RPD


def test_openrouter_429_uses_retry_after_header_and_defaults_to_rpm():
    kind, retry_after = classify_error(429, {}, {"Retry-After": "20"})
    assert kind == ErrorKind.RPM
    assert retry_after == 20.0


def test_openrouter_429_no_headers_defaults_to_rpm_with_no_retry_after():
    kind, retry_after = classify_error(429, {}, {})
    assert kind == ErrorKind.RPM
    assert retry_after is None


def test_401_and_403_are_auth():
    assert classify_error(401)[0] == ErrorKind.AUTH
    assert classify_error(403)[0] == ErrorKind.AUTH


def test_no_response_is_timeout():
    kind, retry_after = classify_error(None)
    assert kind == ErrorKind.TIMEOUT
    assert retry_after is None


def test_5xx_is_error():
    assert classify_error(500)[0] == ErrorKind.ERROR
    assert classify_error(503)[0] == ErrorKind.ERROR
