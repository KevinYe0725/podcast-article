from pathlib import Path


def test_systemd_service_enables_secure_cookie_and_proxy_settings():
    service = Path(__file__).parents[1] / "deploy" / "podcast-article.service"
    lines = service.read_text(encoding="utf-8").splitlines()

    assert "Environment=PA_COOKIE_SECURE=1" in lines
    assert "Environment=PA_COOKIE_DOMAIN=podcast.squareconf.cn" in lines
    assert "Environment=PA_TRUSTED_PROXY=1" in lines
    assert "Environment=PA_SCHEDULER=1" in lines


def test_deploy_smoke_check_uses_public_auth_endpoint():
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "deploy.yml"
    source = workflow.read_text(encoding="utf-8")

    assert "http://172.17.0.1:8788/api/auth/me" in source
    assert "http://172.17.0.1:8788/api/config" not in source


def test_deployment_permissions_preserve_private_account_data():
    root = Path(__file__).parents[1]
    for path in (root / ".github/workflows/deploy.yml", root / "scripts/deploy-server.sh"):
        source = path.read_text(encoding="utf-8")
        assert "/data' -prune" in source
        assert "chmod -R u+rwX,go+rX" not in source


def test_manual_deployment_does_not_overwrite_server_library():
    source = (Path(__file__).parents[1] / "scripts/deploy-server.sh").read_text(encoding="utf-8")
    assert "--exclude library.json" in source and "--exclude data" in source
    assert 'rsync -az -e "$RSYNC_SSH" library.json' not in source
    assert 'output/ "$SERVER:$ROOT/data/output/"' not in source
