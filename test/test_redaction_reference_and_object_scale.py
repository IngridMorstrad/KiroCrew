"""Placeholder references in docs, and many secret-named objects in one text."""

from __future__ import annotations

import time

import pytest

from kiro_crew.deploy.scan import scan_content
from kiro_crew.security.redaction import redact_credentials


@pytest.mark.parametrize(
    "doc",
    [
        "api_key: <your-api-key>\n",
        "password: ********\n",
    ],
)
def test_a_reference_in_place_of_a_secret_is_an_overridable_placeholder(doc: str) -> None:
    findings = [f for f in scan_content(doc) if f.kind.startswith("credential")]
    assert findings, doc
    assert all(
        f.kind == "credential-placeholder" and f.severity == "info" for f in findings
    ), findings


@pytest.mark.parametrize(
    "doc", ["api_key: Zq9vT2mXw8KpL4rB\n", "<api_key><![CDATA[Zq9vT2mXw8KpL4rB]]></api_key>\n"]
)
def test_a_real_value_behind_the_same_label_stays_a_credential(doc: str) -> None:
    findings = scan_content(doc)
    assert any(f.severity == "credential" for f in findings), findings


@pytest.mark.parametrize(
    "doc",
    [
        "Set api_key: <your-api-key> in your config.\n",
        "Put <your-api-key> in the header.\n",
        "see <db-password> for details\n",
    ],
)
def test_an_angle_bracket_name_in_prose_holds_no_value(doc: str) -> None:
    assert redact_credentials(doc)[0] == doc
    assert not [f for f in scan_content(doc) if f.severity == "credential"]


@pytest.mark.parametrize(
    "doc",
    [
        "<password> s3cr3tVal9 </password>\n",
        "<password>s3cr3tVal9</password>\n",
        "<password>\n  s3cr3tVal9\n</password>\n",
    ],
)
def test_an_xml_element_value_is_still_redacted(doc: str) -> None:
    assert "s3cr3tVal9" not in redact_credentials(doc)[0]


def _entries(count: int) -> str:
    return "[" + ",".join('{"name":"x_token","value":"v%d"}' % i for i in range(count)) + "]"


def test_many_secret_named_objects_are_read_in_linear_time() -> None:
    small, large = _entries(1000), _entries(8000)
    redact_credentials(small)
    started = time.perf_counter()
    redact_credentials(small)
    small_s = time.perf_counter() - started
    started = time.perf_counter()
    out = redact_credentials(large)[0]
    large_s = time.perf_counter() - started
    assert '"value":"v7999"' not in out
    # Eight times the text: a per-field rescan of the object reach makes this
    # far more than eight times as slow, and seconds long.
    assert large_s < 3.0, large_s
    assert large_s < 24 * small_s + 0.5, (small_s, large_s)


def test_each_object_keeps_its_own_fields() -> None:
    text = (
        '[{"name":"DB_PASSWORD","x":{"a":"}"},"value":"hunter2Zq9"},'
        '{"value":"keepMe"},{"value":"Zq8hunter","name":"API_TOKEN"}]'
    )
    out = redact_credentials(text)[0]
    assert "hunter2Zq9" not in out and "Zq8hunter" not in out
    assert '"value":"keepMe"' in out


def _views(text: str, size: int) -> list[str]:
    from kiro_crew.log_redaction import SecretRedactionFilter
    from kiro_crew.security.text_redaction import StreamRedactor

    redactor = StreamRedactor()
    streamed = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    return [
        redact_credentials(text)[0],
        streamed + redactor.flush(),
        SecretRedactionFilter([]).redact(text),
    ]


@pytest.mark.parametrize("delim", ["%2C", "%3B", "%2c", "%3b"])
@pytest.mark.parametrize("size", [1, 7, 4096])
def test_an_encoded_password_with_a_blank_and_a_delimiter_is_redacted_whole(
    delim: str, size: int
) -> None:
    text = f"https://x/cb?next=pass%77ord%3DQx7ab%20cd{delim}efKp9Z%26u%3D1 done"
    # The log filter reads no encoded views (an encoded label is not one there).
    for out in _views(text, size)[:2]:
        assert "Qx7ab" not in out and "efKp9Z" not in out, out
        assert out.endswith("%26u%3D1 done"), out
    assert [f for f in scan_content(text) if f.severity == "credential"]


_TWILIO_SID = "SK" + "0123456789abcdef" * 2


@pytest.mark.parametrize(
    "text",
    [
        '{"name":"TWILIO_API_KEY","value":"%s"}' % _TWILIO_SID,
        '{"value":"%s","name":"TWILIO_API_KEY"}' % _TWILIO_SID,
        '[{"Name":"TWILIO_API_KEY","Value":"%s"}]' % _TWILIO_SID,
        '{"name":"TWILIO_API_KEY","type":"PLAINTEXT","value":"%s"}' % _TWILIO_SID,
    ],
)
@pytest.mark.parametrize("size", [1, 7, 4096])
def test_a_twilio_key_sid_in_a_sibling_value_keeps_its_identifier_tier(
    text: str, size: int
) -> None:
    for out in _views(text, size):
        assert _TWILIO_SID in out, out
    assert not [f for f in scan_content(text) if f.severity == "credential"]


def test_a_sibling_secret_beside_a_twilio_name_is_still_redacted() -> None:
    text = '{"name":"TWILIO_AUTH_TOKEN","value":"Zq9vT2mXw8KpL4rBzz"}'
    assert "Zq9vT2mXw8" not in redact_credentials(text)[0]


@pytest.mark.parametrize(
    "text",
    [
        "first_pass: completed\n",
        "pwd: /usr/local\n",
        "refresh token: run kinit\n",
        "second_pass: done\n",
    ],
)
@pytest.mark.parametrize("size", [1, 7, 4096])
def test_a_status_or_path_behind_a_weak_label_stays(text: str, size: int) -> None:
    for out in _views(text, size):
        assert out == text, out
    assert not [f for f in scan_content(text) if f.severity == "credential"]


@pytest.mark.parametrize(
    "text,secret",
    [
        ("first_pass: Zq9vT2mXw8\n", "Zq9vT2mXw8"),
        ("DB_PASS=hunter\n", "hunter"),
        ("refresh token: Zq9vT2mXw8\n", "Zq9vT2mXw8"),
        ("password: run kinit\n", "run"),
    ],
)
def test_a_secret_behind_the_same_labels_is_still_redacted(text: str, secret: str) -> None:
    for out in _views(text, 7):
        assert secret not in out, out


@pytest.mark.parametrize("size", [1, 7, 4096])
def test_a_url_password_ends_at_the_host_and_a_query_value_at_the_fragment(size: int) -> None:
    token = "ghp_" + "a" * 36
    clone = f"git clone https://x-access-token:{token}@github.com/org/repo.git done"
    query = "https://host/cb?access_token=abcd1234efgh5678#frag"
    # The log filter masks a URL's user info its own way (``x-access-token:[REDACTED]``).
    for out in _views(clone, size)[:2]:
        assert token not in out and out.endswith("github.com/org/repo.git done"), out
    for out in _views(query, size)[:2]:
        assert "abcd1234" not in out and out.endswith("#frag"), out
