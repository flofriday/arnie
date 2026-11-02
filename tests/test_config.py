import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arnie import config
from arnie.errors import ArnieError


class VariantTests(unittest.TestCase):
    def test_split_variant_parses_build_and_ref(self):
        self.assertEqual(config.split_variant("graalvm@main"), ("graalvm", "main"))
        self.assertEqual(config.split_variant("native@v0.1.0"), ("native", "v0.1.0"))

    def test_split_variant_handles_refs_containing_at(self):
        self.assertEqual(
            config.split_variant("graalvm@feature@test"), ("graalvm", "feature@test")
        )

    def test_split_variant_returns_none_for_plain_builds(self):
        for name in ["original", "graalvm", "native"]:
            with self.subTest(name=name):
                self.assertEqual(config.split_variant(name), (name, None))

    def test_split_variant_prefers_longer_build_names(self):
        builds = {"graalvm": {}, "graalvm2": {}}
        with patch.object(config, "BUILD_CONFIGS", builds):
            self.assertEqual(
                config.split_variant("graalvm2@main"), ("graalvm2", "main")
            )
            self.assertEqual(config.split_variant("graalvm@main"), ("graalvm", "main"))

    def test_variant_name_roundtrips_through_split_variant(self):
        for build, ref in [("graalvm", "main"), ("native", "v0.1.0")]:
            with self.subTest(build=build, ref=ref):
                name = config.variant_name(build, ref)
                self.assertEqual(config.split_variant(name), (build, ref))

    def test_variant_name_sanitizes_slashes_in_refs(self):
        name = config.variant_name("native", "chore/frontend-polish")
        self.assertEqual(name, "native@chore~frontend-polish")
        self.assertNotIn("/", name)
        self.assertEqual(
            config.split_variant(name), ("native", "chore~frontend-polish")
        )

    def test_display_name_restores_slashes(self):
        self.assertEqual(
            config.display_name("native@chore~frontend-polish"),
            "native@chore/frontend-polish",
        )
        self.assertEqual(config.display_name("graalvm@main"), "graalvm@main")
        self.assertEqual(config.display_name("original"), "original")

    def test_variant_name_and_display_name_roundtrip(self):
        for ref in ["main", "chore/frontend-polish", "v0.1.0", "feature/a/b"]:
            with self.subTest(ref=ref):
                name = config.variant_name("native", ref)
                self.assertNotIn("/", name)
                self.assertEqual(config.display_name(name), f"native@{ref}")

    def test_build_config_and_family_resolve_variants(self):
        self.assertIs(config.build_config("graalvm"), config.BUILD_CONFIGS["graalvm"])
        self.assertIs(
            config.build_config("graalvm@main"), config.BUILD_CONFIGS["graalvm"]
        )
        self.assertEqual(config.build_family("native@feature"), "openvadl")
        self.assertEqual(config.build_family("original"), "original")

    def test_openvadl_builds_returns_only_openvadl_family(self):
        self.assertEqual(config.openvadl_builds(), ["graalvm", "native"])


class ConfigTests(unittest.TestCase):
    def test_only_requested_repositories_are_required(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(config, "load_config", return_value={"open_vadl_path": tmp}),
        ):
            self.assertEqual(
                config.require_repos({"open_vadl_path"}),
                {"open_vadl_path": Path(tmp).resolve()},
            )
            with self.assertRaisesRegex(ArnieError, "original_vadl_path"):
                config.require_repos({"original_vadl_path"})

    def test_empty_request_does_not_need_configuration(self):
        with patch.object(config, "load_config") as load:
            self.assertEqual(config.require_repos(set()), {})
        load.assert_not_called()

    def test_repository_paths_validate_type_and_existence(self):
        for value in [12, "/this/directory/does/not/exist"]:
            with (
                self.subTest(value=value),
                patch.object(
                    config, "load_config", return_value={"open_vadl_path": value}
                ),
                self.assertRaises(ArnieError),
            ):
                config.require_repos({"open_vadl_path"})

    def test_build_environment_extends_parent_without_mutating_it(self):
        with patch.dict(os.environ, {"ARNIE_TEST_ENV": "parent"}):
            env = config.resolve_build_env({"ARNIE_TEST_ENV": "child"})
            self.assertEqual(env["ARNIE_TEST_ENV"], "child")
            self.assertEqual(os.environ["ARNIE_TEST_ENV"], "parent")
        self.assertIsNone(config.resolve_build_env(None))
        with self.assertRaises(ArnieError):
            config.resolve_build_env("unknown-java")
