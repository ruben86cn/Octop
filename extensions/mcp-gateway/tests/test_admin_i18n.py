from admin_i18n import locale_for, render_admin
from starlette.requests import Request


def test_three_locales_render_the_same_controls():
    for locale, heading in [
        ("zh", "中间件授权控制台"),
        ("en", "Gateway access console"),
        ("ja", "ゲートウェイ権限管理"),
    ]:
        html = render_admin(locale, "test-nonce")
        assert heading in html
        assert f'<html lang="{locale}">' in html
        assert "__NONCE__" not in html and "__LANG__" not in html
        assert 'id="feedback"' in html and 'id="uiLanguage"' in html
        assert 'nonce="test-nonce"' in html


def test_locale_is_allowlisted_and_query_wins():
    request = Request(
        {"type": "http", "query_string": b"lang=ja", "headers": [(b"accept-language", b"en-US")]}
    )
    assert locale_for(request) == "ja"
    request = Request({"type": "http", "query_string": b"lang=%3Cscript%3E", "headers": []})
    assert locale_for(request) == "zh"
