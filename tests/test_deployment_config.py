from pathlib import Path


def test_systemd_service_enables_secure_cookie_and_proxy_settings():
    service = Path(__file__).parents[1] / "deploy" / "podcast-article.service"
    lines = service.read_text(encoding="utf-8").splitlines()

    assert "Environment=PA_COOKIE_SECURE=1" in lines
    assert "Environment=PA_COOKIE_DOMAIN=podcast.squareconf.cn" in lines
    assert "Environment=PA_TRUSTED_PROXY=1" in lines


def test_deploy_smoke_check_uses_public_auth_endpoint():
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "deploy.yml"
    source = workflow.read_text(encoding="utf-8")

    assert "http://172.17.0.1:8788/api/auth/me" in source
    assert "http://172.17.0.1:8788/api/config" not in source
