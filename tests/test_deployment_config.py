from pathlib import Path


def test_systemd_service_enables_secure_cookie_and_proxy_settings():
    service = Path(__file__).parents[1] / "deploy" / "podcast-article.service"
    lines = service.read_text(encoding="utf-8").splitlines()

    assert "Environment=PA_COOKIE_SECURE=1" in lines
    assert "Environment=PA_COOKIE_DOMAIN=podcast.squareconf.cn" in lines
    assert "Environment=PA_TRUSTED_PROXY=1" in lines
