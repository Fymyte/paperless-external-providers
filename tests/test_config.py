import pytest

from paperless_sources.config import ConfigError
from paperless_sources.config import UploadSpec
from paperless_sources.config import load
from paperless_sources.providers.edocperso import EDocPersoProvider

PROVIDERS = {"edocperso": EDocPersoProvider}

BASE = """
poll_interval = 600

[paperless]
url = "http://webserver:8000"

[[sources]]
name = "pay-slips"
provider = "edocperso"

[sources.config]
login = "me@example.com"
folders = ["Bulletins de paie"]

[sources.upload]
tags = ["Payslip", 4]
correspondent = "ACME"
"""


def _write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_load_with_secrets_from_env(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("s3cret\n")
    env = {
        "PAPERLESS_TOKEN": "tok",
        "SOURCE_PAY_SLIPS_PASSWORD_FILE": str(secret),
    }

    cfg = load(_write(tmp_path, BASE), PROVIDERS, env)

    assert cfg.paperless_token == "tok"
    assert cfg.poll_interval == 600
    assert cfg.max_attempts == 3
    (source,) = cfg.sources
    assert source.config == {
        "login": "me@example.com",
        "password": "s3cret",
        "folders": ["Bulletins de paie"],
    }
    assert source.upload == UploadSpec(correspondent="ACME", tags=("Payslip", 4))


def test_env_overrides_file(tmp_path):
    env = {
        "PAPERLESS_TOKEN": "tok",
        "PAPERLESS_URL": "http://other:8000",
        "SOURCE_PAY_SLIPS_PASSWORD": "pw",
        "SOURCE_PAY_SLIPS_LOGIN": "other@example.com",
    }
    cfg = load(_write(tmp_path, BASE), PROVIDERS, env)
    assert cfg.paperless_url == "http://other:8000"
    assert cfg.sources[0].config["login"] == "other@example.com"


def test_secret_in_file_is_rejected(tmp_path):
    text = BASE.replace('login = "me@example.com"', 'login = "me"\npassword = "pw"')
    with pytest.raises(ConfigError, match="SOURCE_PAY_SLIPS_PASSWORD"):
        load(_write(tmp_path, text), PROVIDERS, {"PAPERLESS_TOKEN": "tok"})


def test_missing_required_secret(tmp_path):
    with pytest.raises(ConfigError, match="password is required"):
        load(_write(tmp_path, BASE), PROVIDERS, {"PAPERLESS_TOKEN": "tok"})


def test_missing_token(tmp_path):
    with pytest.raises(ConfigError, match="PAPERLESS_TOKEN"):
        load(_write(tmp_path, BASE), PROVIDERS, {})


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ('provider = "edocperso"', 'provider = "nope"', "unknown provider"),
        ('name = "pay-slips"', 'name = "Pay Slips"', "must match"),
        ('folders = ["Bulletins de paie"]', "colour = 1", "unknown config keys"),
        ('correspondent = "ACME"', "owner = 1", "unknown upload keys"),
        ('correspondent = "ACME"', "correspondent = true", "IDs or names"),
        ("poll_interval = 600", "poll_interval = 0", "positive"),
    ],
)
def test_invalid(tmp_path, old, new, match):
    env = {"PAPERLESS_TOKEN": "tok", "SOURCE_PAY_SLIPS_PASSWORD": "pw"}
    with pytest.raises(ConfigError, match=match):
        load(_write(tmp_path, BASE.replace(old, new)), PROVIDERS, env)


def test_disabled_source_is_skipped(tmp_path):
    text = BASE.replace('provider = "edocperso"', 'provider = "nope"\nenabled = false')
    cfg = load(_write(tmp_path, text), PROVIDERS, {"PAPERLESS_TOKEN": "tok"})
    assert cfg.sources == ()
