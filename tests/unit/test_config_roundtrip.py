"""write_config → _load_settings round trip must not freeze the derived URL.

brain_url auto-derives from port_brain_api. Persisting the derived literal
into config.yaml would mask every later PORT_BRAIN_API override — the exact
workflow docs/CLI.md prescribes.
"""

import yaml

from agentibrain import bootstrap
from agentibrain.config import BrainSettings


def _settings(tmp_path, **kw) -> BrainSettings:
    return BrainSettings(
        mode="local", vault_path=tmp_path / "vault", config_dir=tmp_path / "cfg", **kw
    )


def test_derived_brain_url_is_not_persisted(tmp_path):
    cfg_path = bootstrap.write_config(_settings(tmp_path))
    payload = yaml.safe_load(cfg_path.read_text())
    assert "brain_url" not in payload


def test_explicit_brain_url_is_persisted(tmp_path):
    cfg_path = bootstrap.write_config(_settings(tmp_path, brain_url="http://remote:9090"))
    payload = yaml.safe_load(cfg_path.read_text())
    assert payload["brain_url"] == "http://remote:9090"


def test_port_override_survives_config_reload(tmp_path, monkeypatch):
    """After init wrote config.yaml, PORT_BRAIN_API must still move the URL."""
    cfg_path = bootstrap.write_config(_settings(tmp_path))
    payload = {
        k: v for k, v in (yaml.safe_load(cfg_path.read_text()) or {}).items() if v is not None
    }
    monkeypatch.setenv("PORT_BRAIN_API", "9000")
    reloaded = BrainSettings(**payload, _env_file=None)
    assert reloaded.brain_url == "http://localhost:9000"
