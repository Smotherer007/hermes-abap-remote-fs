"""Registration, guard rails, manifest."""

from __future__ import annotations

import pytest
import yaml

from conftest import ROOT, plugin, sub

safety = sub("safety")
config = sub("config")
models = sub("models")
tools = sub("tools")


class FakeCtx:
    def __init__(self, settings=None):
        self.tools, self.hooks, self.sections = {}, {}, {}
        self.settings = settings or {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, handler)

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_system_prompt_section(self, id, content, **kw):
        self.sections[id] = content

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


def manifest():
    return yaml.safe_load((ROOT / "plugin.yaml").read_text())


@pytest.fixture
def ctx():
    c = FakeCtx()
    plugin.register(c)
    return c


class TestRegistration:
    def test_every_declared_tool_is_registered(self, ctx):
        assert sorted(ctx.tools) == sorted(manifest()["provides_tools"])
        assert len(ctx.tools) == 27
        assert {t for t, _, _ in ctx.tools.values()} == {"abap"}

    def test_schemas_are_consistent(self, ctx):
        for name, (_, schema, _) in ctx.tools.items():
            assert schema["name"] == name
            params = schema["parameters"]
            assert params["type"] == "object"
            for required in params["required"]:
                assert required in params["properties"], f"{name}: {required}"

    def test_hook_and_prompt_section(self, ctx):
        assert list(ctx.hooks) == manifest()["provides_hooks"]
        config.save_profile("dev", models.AbapProfile(url="http://sap:8000", username="DEV", password="TOPSECRET",
                                                      client="100"))
        text = ctx.sections["abap-remote-fs.overview"]({})
        assert "- dev (active): DEV@http://sap:8000, client 100" in text
        assert "TOPSECRET" not in text

    def test_older_hermes_without_prompt_sections(self):
        class Older(FakeCtx):
            register_system_prompt_section = None

        c = Older()
        plugin.register(c)
        assert c.tools and not c.sections

    def test_manifest_settings_match_the_code(self):
        schema = manifest()["config_schema"]
        assert schema["safety_level"]["choices"] == list(safety.LEVELS)
        assert schema["activation"]["choices"] == list(safety.ACTIVATION)
        assert manifest()["name"] == safety.PLUGIN


class TestSafety:
    def hook(self, **settings):
        return safety.make_pre_tool_call_hook(lambda key, default: settings.get(key, default), lambda: "dev")

    def test_open_by_default(self):
        hook = self.hook()
        assert hook(tool_name="abap_write", args={"name": "ZCL_A", "source": "x"}) is None
        assert hook(tool_name="abap_activate", args={"name": "ZCL_A"}) is None

    def test_reads_are_never_touched(self):
        hook = self.hook(safety_level="readonly", readonly_profiles=["dev"])
        for tool in ("abap_read", "abap_search", "abap_query", "abap_unlock", "abap_atc", "abap_unit_test"):
            assert hook(tool_name=tool, args={"name": "ZCL_A"}) is None

    def test_confirm_asks_with_a_readable_line_and_a_narrow_rule(self):
        directive = self.hook(safety_level="confirm")(
            tool_name="abap_write", args={"name": "ZCL_A", "source": "a\nb", "transport": "DEVK9"})
        assert directive["action"] == "approve"
        assert directive["message"] == "Write 2 lines of source to ZCL_A (transport DEVK9)"
        assert directive["rule_key"] == "abap-remote-fs:abap_write:dev:zcl_a"

    def test_readonly_blocks_changes(self):
        hook = self.hook(safety_level="readonly")
        for tool in ("abap_write", "abap_lock", "abap_create_object", "abap_set_text_elements", "abap_activate"):
            assert hook(tool_name=tool, args={"name": "X"})["action"] == "block"

    def test_dangerous_unit_tests_count_as_a_change(self):
        hook = self.hook(safety_level="confirm")
        assert hook(tool_name="abap_unit_test", args={"name": "X"}) is None
        assert hook(tool_name="abap_unit_test", args={"name": "X", "dangerous": True})["action"] == "approve"
        assert hook(tool_name="abap_unit_test", args={"name": "X", "critical": "true"})["action"] == "approve"

    def test_activation_block_is_the_neo_rule(self):
        hook = self.hook(activation="block")
        assert hook(tool_name="abap_write", args={"name": "X", "source": ""}) is None
        directive = hook(tool_name="abap_activate", args={"name": "X"})
        assert directive["action"] == "block" and "a person in SAP" in directive["message"]

    def test_activation_confirm_overrides_open(self):
        assert self.hook(activation="confirm")(tool_name="abap_activate", args={"name": "X"})["action"] == "approve"

    def test_readonly_profiles_win_over_everything(self):
        hook = self.hook(readonly_profiles=["prod"])
        assert hook(tool_name="abap_write", args={"name": "X", "profile": "prod"})["action"] == "block"
        assert hook(tool_name="abap_write", args={"name": "X", "profile": "dev"}) is None

    def test_readonly_profiles_apply_to_the_active_profile(self):
        hook = safety.make_pre_tool_call_hook(
            lambda key, default: {"readonly_profiles": "prod, qas"}.get(key, default), lambda: "qas")
        assert hook(tool_name="abap_activate", args={"name": "X"})["action"] == "block"

    def test_unknown_values_fail_closed(self):
        assert self.hook(safety_level="yolo")(tool_name="abap_write", args={"name": "X"})["action"] == "approve"

    def test_settings_are_read_at_call_time(self):
        c = FakeCtx(settings={"safety_level": "readonly"})
        plugin.register(c)
        assert c.hooks["pre_tool_call"](tool_name="abap_write", args={"name": "X"})["action"] == "block"
        c.settings["safety_level"] = "open"
        assert c.hooks["pre_tool_call"](tool_name="abap_write", args={"name": "X"}) is None


class TestConfig:
    def test_same_file_format_as_pi(self, isolated_config):
        isolated_config.write_text('{"profiles": {"dev": {"url": "http://172.31.1.12:8000", "username": "NEO", '
                                   '"password": "p", "client": "900", "allowUnauthorized": true}}, '
                                   '"activeProfile": "dev"}')
        config.load_config()
        profile = config.resolve_config()
        assert profile.client == "900" and profile.allow_unauthorized is True and profile.username == "NEO"

    def test_environment_profile(self, monkeypatch, isolated_config):
        monkeypatch.setenv("ABAP_URL", "http://sap:8000/sap/bc/adt/")
        monkeypatch.setenv("ABAP_USER", "NEO")
        monkeypatch.setenv("ABAP_PASSWORD", "pw")
        monkeypatch.setenv("ABAP_CLIENT", "900")
        config.load_config()
        profile = config.resolve_config()
        assert profile.url == "http://sap:8000" and profile.client == "900"
        assert config.is_env_profile("default")
        config.save_profile("other", models.AbapProfile(url="http://x:1", username="u", password="p"))
        assert "pw" not in isolated_config.read_text()

    def test_url_needs_a_scheme(self):
        with pytest.raises(ValueError, match="Include the scheme"):
            config.normalize_base_url("sap.example.com:8000")
