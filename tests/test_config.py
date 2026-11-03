from __future__ import annotations

import contextlib
import getpass
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from worklog_hub.cli import main
from worklog_hub.config import AppConfig, ConfigurationError, GitLabConfig, GroupScope
from worklog_hub.credentials import SERVICE_NAME, load_token, save_token

VALID = """
[database]
path = "data/log.sqlite3"
[gitlab]
url = "https://gitlab.example/"
groups = ["team/backend", "team/backend"]
include_subgroups = false
timeout_seconds = 12
"""


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "app.toml"

    def write(self, text: str = VALID) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_loads_config_relative_to_its_directory(self) -> None:
        self.write()
        config = AppConfig.from_file(self.path)
        self.assertEqual(config.database_path, self.path.parent / "data/log.sqlite3")
        self.assertEqual(config.gitlab_url, "https://gitlab.example")
        self.assertEqual(config.scope, GroupScope(("team/backend",), False))
        self.assertEqual(config.timeout_seconds, 12)

    def test_rejects_empty_invalid_or_secret_configuration(self) -> None:
        invalid = [
            VALID.replace('["team/backend", "team/backend"]', "[]"),
            VALID.replace('["team/backend", "team/backend"]', '["team//backend"]'),
            VALID.replace(
                '"https://gitlab.example/"', '"https://user:secret@gitlab.example"'
            ),
            VALID.replace("timeout_seconds = 12", "timeout_seconds = nan"),
            VALID.replace("timeout_seconds = 12", "timeout_seconds = true"),
            VALID.replace("include_subgroups = false", 'include_subgroups = "false"'),
            VALID + '\ntoken = "secret"',
            "[gitlab",
        ]
        for text in invalid:
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(ConfigurationError) as caught:
                    AppConfig.from_file(self.path)
                self.assertNotIn("secret", str(caught.exception))

    def test_path_boundary_does_not_match_similar_group(self) -> None:
        scope = GroupScope(("team/back",))
        self.assertTrue(scope.contains("team/back/child"))
        self.assertFalse(scope.contains("team/backend"))
        self.assertFalse(GroupScope(("team/back",), False).contains("team/back/child"))

    def test_token_is_not_in_config_repr(self) -> None:
        self.assertNotIn(
            "hidden-token", repr(GitLabConfig("https://example", "hidden-token"))
        )

    def test_cli_login_stores_hidden_input_without_network_or_database(self) -> None:
        self.write(VALID.replace('["team/backend", "team/backend"]', "[]"))
        output = io.StringIO()
        with (
            patch("worklog_hub.cli.getpass.getpass", return_value="hidden-token"),
            patch("worklog_hub.cli.save_token") as save,
            patch("worklog_hub.cli.load_token") as load,
            patch("worklog_hub.cli.GitLabGraphQLClient") as client,
            contextlib.redirect_stdout(output),
        ):
            result = main(["--config", str(self.path), "gitlab", "login"])
        self.assertEqual(result, 0)
        save.assert_called_once_with("https://gitlab.example", "hidden-token")
        load.assert_not_called()
        client.assert_not_called()
        self.assertNotIn("hidden-token", output.getvalue())
        self.assertFalse((self.path.parent / "data").exists())

    def test_cli_refuses_visible_token_fallback(self) -> None:
        self.write()
        with (
            patch(
                "worklog_hub.cli.getpass.getpass", side_effect=getpass.GetPassWarning
            ),
            patch("worklog_hub.cli.save_token") as save,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["--config", str(self.path), "gitlab", "login"]), 1)
        save.assert_not_called()

    def test_missing_config_is_cli_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "gitlab", "check"]), 1)

    def test_status_does_not_require_keyring(self) -> None:
        self.write()
        with (
            patch("worklog_hub.cli.load_token") as load,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["--config", str(self.path), "sync-status"]), 0)
        load.assert_not_called()

    def test_groups_accepts_unconfigured_scope_and_prints_available_paths(self) -> None:
        self.write(VALID.replace('groups = ["team/backend", "team/backend"]', ""))
        output = io.StringIO()
        with (
            patch("worklog_hub.cli.load_token", return_value="hidden-token"),
            patch("worklog_hub.cli.GitLabGraphQLClient") as client,
            contextlib.redirect_stdout(output),
        ):
            client.return_value.execute.return_value = {
                "groups": {
                    "nodes": [
                        {
                            "id": "group-1",
                            "name": "Backend",
                            "fullPath": "team/backend",
                            "webUrl": "https://gitlab.example/team/backend",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
            result = main(
                ["--config", str(self.path), "gitlab", "groups", "--search", "backend"]
            )
        self.assertEqual(result, 0)
        self.assertIn("team/backend\tBackend\tgroup-1", output.getvalue())
        self.assertNotIn("hidden-token", output.getvalue())
        self.assertFalse((self.path.parent / "data").exists())
        self.assertEqual(
            client.return_value.execute.call_args.args[1]["search"], "backend"
        )

    def test_sync_still_requires_nonempty_groups_before_accessing_keyring(self) -> None:
        self.write(VALID.replace('["team/backend", "team/backend"]', "[]"))
        with (
            patch("worklog_hub.cli.load_token") as load,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = main(
                [
                    "--config",
                    str(self.path),
                    "gitlab",
                    "sync",
                    "--from",
                    "2026-10-01",
                    "--to",
                    "2026-10-31",
                ]
            )
        self.assertEqual(result, 1)
        load.assert_not_called()

    def test_groups_empty_response_and_api_errors_are_reported(self) -> None:
        self.write()
        with (
            patch("worklog_hub.cli.load_token", return_value="hidden-token"),
            patch("worklog_hub.cli.GitLabGraphQLClient") as client,
        ):
            client.return_value.execute.return_value = {
                "groups": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["--config", str(self.path), "gitlab", "groups"]), 0
                )
            self.assertIn("Доступных групп не найдено", output.getvalue())
            client.return_value.execute.side_effect = RuntimeError("error hidden-token")
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(
                    main(["--config", str(self.path), "gitlab", "groups"]), 1
                )
            self.assertNotIn("hidden-token", errors.getvalue())


class CredentialTests(unittest.TestCase):
    def test_jira_token_is_separate_from_gitlab_for_same_server(self) -> None:
        backend = Mock()
        backend.get_password.return_value = "fake-jira-pat"
        with patch("worklog_hub.credentials._backend", return_value=backend):
            save_token("https://example", "fake-jira-pat", source="jira")
            self.assertEqual(
                load_token("https://example", source="jira"), "fake-jira-pat"
            )
        backend.set_password.assert_called_once_with(
            "worklog-hub/jira", "https://example", "fake-jira-pat"
        )
        backend.get_password.assert_called_once_with(
            "worklog-hub/jira", "https://example"
        )

    def test_read_and_write_use_service_and_instance_url(self) -> None:
        backend = Mock()
        backend.get_password.return_value = "token"
        with patch("worklog_hub.credentials._backend", return_value=backend):
            save_token("https://gitlab.example", " token ")
            self.assertEqual(load_token("https://gitlab.example"), "token")
        backend.set_password.assert_called_once_with(
            SERVICE_NAME, "https://gitlab.example", "token"
        )
        backend.get_password.assert_called_once_with(
            SERVICE_NAME, "https://gitlab.example"
        )

    def test_missing_or_unavailable_keyring_never_falls_back(self) -> None:
        backend = Mock()
        with patch("worklog_hub.credentials._backend", return_value=backend):
            backend.get_password.return_value = None
            with self.assertRaisesRegex(ConfigurationError, "gitlab login"):
                load_token("https://gitlab.example")
            backend.get_password.side_effect = RuntimeError("secret")
            backend.set_password.side_effect = RuntimeError("secret")
            for operation in (
                lambda: load_token("https://gitlab.example"),
                lambda: save_token("https://gitlab.example", "token"),
            ):
                with self.assertRaises(ConfigurationError) as caught:
                    operation()
                self.assertNotIn("secret", str(caught.exception))

    def test_windows_backend_is_native_despite_keyring_overrides(self) -> None:
        with (
            patch("worklog_hub.credentials.sys.platform", "win32"),
            patch("keyring.backends.Windows.WinVaultKeyring") as native,
            patch("keyring.get_keyring") as configured,
        ):
            load_token("https://gitlab.example")
        native.assert_called_once()
        configured.assert_not_called()


class JiraConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "app.toml"
        self.path.write_text(
            '[jira]\nurl = "https://jira.example/context"\nprojects = []\n',
            encoding="utf-8",
        )

    def test_jira_only_config_does_not_require_gitlab_or_projects_for_discovery(
        self,
    ) -> None:
        config = AppConfig.from_file(self.path, source="jira", require_groups=False)
        self.assertEqual(config.jira_url, "https://jira.example/context")
        self.assertEqual(config.jira_projects, ())
        self.assertEqual(config.gitlab_url, "")
        with self.assertRaises(ConfigurationError):
            AppConfig.from_file(
                self.path,
                source="jira",
                require_groups=False,
                require_jira_projects=True,
            )

    def test_jira_cli_projects_uses_separate_keyring_token_and_does_not_create_db(
        self,
    ) -> None:
        output = io.StringIO()
        with (
            patch("worklog_hub.cli.load_token", return_value="fake-jira-pat") as load,
            patch("worklog_hub.cli.JiraRESTClient") as client,
            contextlib.redirect_stdout(output),
        ):
            client.return_value.get.return_value = [
                {"id": "100", "key": "ABC", "name": "Тест"}
            ]
            result = main(["--config", str(self.path), "jira", "projects"])
        self.assertEqual(result, 0)
        self.assertIn("ABC\tТест\t100", output.getvalue())
        load.assert_called_once_with("https://jira.example/context", source="jira")
        self.assertFalse((self.path.parent / "worklog-hub.sqlite3").exists())

    def test_jira_cli_check_validates_pat_without_project_scope(self) -> None:
        with (
            patch("worklog_hub.cli.load_token", return_value="fake-jira-pat"),
            patch("worklog_hub.cli.JiraRESTClient") as client,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            client.return_value.get.return_value = {"name": "tester"}
            self.assertEqual(main(["--config", str(self.path), "jira", "check"]), 0)
        client.return_value.get.assert_called_once_with("/rest/api/2/myself")

    def test_jira_cli_sync_rejects_empty_scope_before_loading_token(self) -> None:
        with (
            patch("worklog_hub.cli.load_token") as load,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                main(
                    [
                        "--config",
                        str(self.path),
                        "jira",
                        "sync",
                        "--from",
                        "2026-10-01",
                        "--to",
                        "2026-10-31",
                    ]
                ),
                1,
            )
        load.assert_not_called()
