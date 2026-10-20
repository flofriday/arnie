import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arnie import config
from arnie.errors import ArnieError


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
