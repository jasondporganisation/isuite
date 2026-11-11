import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isuite import run, setup
from isuite.config import load_env_file

TOKEN = "123456789:AAFakeTokenForTestsOnly_x-y"
KEY = "sk-ant-fake-key-for-tests-only"
OWNER = "424242"


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class Session:
    """Fake terminal: records what was shown, feeds answers, never prints a secret."""

    def __init__(self, visible=(), hidden=()):
        self.visible, self.hidden = list(visible), list(hidden)
        self.shown, self.hidden_prompts, self.visible_prompts = [], [], []

    def out(self, text=""):
        self.shown.append(text)

    def input(self, prompt=""):
        self.visible_prompts.append(prompt)
        return self.visible.pop(0)

    def getpass(self, prompt=""):
        self.hidden_prompts.append(prompt)
        return self.hidden.pop(0)

    def everything(self):
        return "\n".join(self.shown + self.hidden_prompts + self.visible_prompts)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / ".isuite"
        self.file = self.dir / "config.env"

    def run_setup(self, session, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = setup.main([], config_dir=self.dir, input_fn=session.input,
                              getpass_fn=session.getpass, out=session.out, **kw)
        session.err = err.getvalue()
        return code

    def test_writes_the_three_values_with_private_modes(self):
        s = Session(visible=[OWNER], hidden=[TOKEN, KEY])
        self.assertEqual(self.run_setup(s), 0)
        self.assertEqual(mode(self.file), 0o600)
        self.assertEqual(mode(self.dir), 0o700)
        values = load_env_file(self.file)
        self.assertEqual(values["ISUITE_TELEGRAM_TOKEN"], TOKEN)
        self.assertEqual(values["ANTHROPIC_API_KEY"], KEY)
        self.assertEqual(values["ISUITE_OWNER"], OWNER)
        self.assertEqual(values["ISUITE_DB"], str(self.dir / "isuite.db"))
        self.assertEqual(os.listdir(self.dir), ["config.env"])  # no temp file left behind

    def test_secrets_are_asked_hidden_and_never_shown(self):
        s = Session(visible=[OWNER], hidden=[TOKEN, KEY])
        self.run_setup(s)
        self.assertEqual(len(s.hidden_prompts), 2)
        self.assertEqual(len(s.visible_prompts), 1)
        for secret in (TOKEN, KEY):
            self.assertNotIn(secret, s.everything())
            self.assertNotIn(secret, s.err)
            self.assertNotIn(secret[:12], s.everything())

    def test_a_bad_owner_id_is_asked_again_without_echoing_it(self):
        s = Session(visible=["not-a-number-99", "-4", OWNER], hidden=[TOKEN, KEY])
        self.assertEqual(self.run_setup(s), 0)
        self.assertNotIn("not-a-number-99", s.everything())
        self.assertEqual(load_env_file(self.file)["ISUITE_OWNER"], OWNER)

    def test_gives_up_after_three_bad_answers_and_writes_nothing(self):
        s = Session(visible=["x", "y", "z"], hidden=[TOKEN, KEY])
        self.assertEqual(self.run_setup(s), 1)
        self.assertFalse(self.file.exists())

    def test_values_that_could_break_the_file_are_refused(self):
        s = Session(visible=[OWNER], hidden=["abc def", "tok\nINJECT=1", TOKEN,
                                             "k\nX=1", "key with space", KEY])
        self.assertEqual(self.run_setup(s), 0)
        text = self.file.read_text()
        self.assertEqual(len(text.strip().splitlines()), 4)
        self.assertNotIn("INJECT", text)

    def test_existing_file_is_kept_unless_the_user_says_yes(self):
        self.dir.mkdir(mode=0o700)
        self.file.write_text("ISUITE_OWNER=1\n")
        os.chmod(self.file, 0o600)
        s = Session(visible=["n"], hidden=[])
        self.assertEqual(self.run_setup(s), 0)
        self.assertEqual(self.file.read_text(), "ISUITE_OWNER=1\n")
        s = Session(visible=["y", OWNER], hidden=[TOKEN, KEY])
        self.assertEqual(self.run_setup(s), 0)
        self.assertEqual(load_env_file(self.file)["ISUITE_OWNER"], OWNER)
        self.assertEqual(mode(self.file), 0o600)

    def run_with_tmp(self, make_tmp):
        self.dir.mkdir(mode=0o700)
        tmp = self.dir / "config.env.tmp"
        make_tmp(tmp)
        s = Session(visible=[OWNER], hidden=[TOKEN, KEY])
        return s, self.run_setup(s), tmp

    def test_a_stale_temp_file_is_removed_and_setup_carries_on(self):
        s, code, tmp = self.run_with_tmp(lambda t: t.write_text("leftover"))
        self.assertEqual(code, 0)
        self.assertFalse(tmp.exists())
        self.assertEqual(load_env_file(self.file)["ISUITE_OWNER"], OWNER)

    def test_a_symlink_as_the_temp_file_is_refused_and_never_written_through(self):
        target = Path(self.tmp.name) / "victim.txt"
        target.write_text("untouched")
        s, code, tmp = self.run_with_tmp(lambda t: os.symlink(target, t))
        self.assertEqual(code, 1)
        self.assertEqual(target.read_text(), "untouched")
        self.assertTrue(tmp.is_symlink())  # left alone for the user to look at
        self.assertFalse(self.file.exists())
        self.assertNotIn("Traceback", s.err)
        self.assertEqual(len([l for l in s.err.splitlines() if l.strip()]), 1)
        for secret in (TOKEN, KEY):
            self.assertNotIn(secret, s.err + s.everything())

    def test_a_temp_path_that_cannot_be_removed_is_one_plain_line_and_exit_1(self):
        def make_dir(t):
            t.mkdir()
            (t / "inner").write_text("x")
        s, code, tmp = self.run_with_tmp(make_dir)
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", s.err)
        self.assertEqual(len([l for l in s.err.splitlines() if l.strip()]), 1)
        self.assertFalse(self.file.exists())

    def test_the_file_is_private_from_the_moment_it_is_created(self):
        old = os.umask(0)  # a wide open umask: only the creation mode can keep it private
        self.addCleanup(os.umask, old)
        with mock.patch.object(setup.os, "chmod"):  # no after-the-fact fix allowed
            s = Session(visible=[OWNER], hidden=[TOKEN, KEY])
            self.assertEqual(self.run_setup(s), 0)
        self.assertEqual(mode(self.file), 0o600)
        self.assertEqual(mode(self.dir), 0o700)

    def test_loose_existing_directory_is_tightened(self):
        self.dir.mkdir()
        os.chmod(self.dir, 0o755)
        self.run_setup(Session(visible=[OWNER], hidden=[TOKEN, KEY]))
        self.assertEqual(mode(self.dir), 0o700)

    def test_ctrl_c_writes_nothing_and_exits_cleanly(self):
        def boom(prompt=""):
            raise KeyboardInterrupt
        s = Session()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = setup.main([], config_dir=self.dir, input_fn=boom, getpass_fn=boom,
                              out=s.out)
        self.assertEqual(code, 1)
        self.assertFalse(self.file.exists())

    def test_tells_the_user_the_next_step(self):
        s = Session(visible=[OWNER], hidden=[TOKEN, KEY])
        self.run_setup(s)
        self.assertIn("python3 -m isuite.run", s.everything())


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.env"

    def write(self, text, m=0o600):
        self.path.write_text(text)
        os.chmod(self.path, m)

    def test_missing_file_is_empty(self):
        self.assertEqual(load_env_file(self.path), {})

    def test_reads_known_keys_and_ignores_comments_blank_lines_and_unknown_keys(self):
        self.write("# note\n\nISUITE_OWNER=5\nPATH=/evil\nANTHROPIC_API_KEY = \"abc\"\n")
        self.assertEqual(load_env_file(self.path),
                         {"ISUITE_OWNER": "5", "ANTHROPIC_API_KEY": "abc"})

    def test_a_file_others_can_read_is_refused_without_showing_contents(self):
        self.write("ANTHROPIC_API_KEY=topsecretvalue\n", 0o644)
        with self.assertRaises(ValueError) as cm:
            load_env_file(self.path)
        self.assertIn("chmod 600", str(cm.exception))
        self.assertNotIn("topsecretvalue", str(cm.exception))


class RunLoadsConfigFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.env"
        self.path.write_text(f"ISUITE_OWNER={OWNER}\nISUITE_TELEGRAM_TOKEN={TOKEN}\n"
                             "ANTHROPIC_API_KEY=file-key\n")
        os.chmod(self.path, 0o600)

    def test_file_fills_in_when_no_solo_config_is_in_the_environment(self):
        env = run.effective_env({}, self.path)
        self.assertEqual(env["ISUITE_OWNER"], OWNER)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "file-key")

    def test_any_isuite_variable_or_api_key_disables_the_file_completely(self):
        for key in ("ISUITE_OWNER", "ISUITE_ADMINS", "ISUITE_DB", "ISUITE_TELEGRAM_TOKEN",
                    "ISUITE_ANYTHING", "ANTHROPIC_API_KEY"):
            for value in ("9", ""):  # even a blank one counts: no per-key merge
                env = run.effective_env({key: value}, self.path)
                self.assertEqual(env, {key: value}, (key, value))

    def test_a_team_set_up_with_only_admins_never_turns_solo(self):
        env = run.effective_env({"ISUITE_ADMINS": "1,2", "ISUITE_TELEGRAM_TOKEN": "t"}, self.path)
        self.assertNotIn("ISUITE_OWNER", env)

    def test_unrelated_variables_do_not_block_the_file_and_are_kept(self):
        env = run.effective_env({"PATH": "/bin", "HOME": "/x"}, self.path)
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["ISUITE_OWNER"], OWNER)

    def test_no_file_path_or_no_file_changes_nothing(self):
        env = {"A": "1"}
        self.assertEqual(run.effective_env(env, None), env)
        self.assertEqual(run.effective_env(env, Path(self.tmp.name) / "nope.env"), env)

    def main(self, env, **kw):
        """run.main with a serve that fails the test: the real polling loop can never start."""
        started = []

        def no_serve(cfg):
            started.append(cfg)
            self.fail("run.main reached the polling loop")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run.main([], env=env, serve=no_serve, **kw)
        return code, err.getvalue(), started

    def test_main_uses_the_file_and_never_prints_its_secrets(self):
        self.path.write_text(f"ISUITE_OWNER={OWNER}\nISUITE_TELEGRAM_TOKEN={TOKEN}\n")
        os.chmod(self.path, 0o600)
        code, err, _ = self.main({}, config_file=self.path)
        self.assertEqual(code, 2)  # stops before serving: the key is still missing
        self.assertIn("ANTHROPIC_API_KEY", err)
        self.assertNotIn(TOKEN, err)

    def test_main_hands_a_complete_file_to_serve_as_a_solo_config(self):
        seen = []
        code = run.main([], env={}, config_file=self.path,
                        serve=lambda cfg: seen.append(cfg) or 0)
        self.assertEqual(code, 0)
        self.assertEqual((seen[0].owner_id, seen[0].admin_ids, seen[0].telegram_token),
                         (int(OWNER), {int(OWNER)}, TOKEN))

    def test_main_with_a_loose_file_is_a_clean_exit(self):
        os.chmod(self.path, 0o644)
        code, err, started = self.main({}, config_file=self.path)
        self.assertEqual((code, started), (2, []))
        self.assertIn("chmod 600", err)
        self.assertNotIn(TOKEN, err)

    def test_main_owner_and_admins_both_set_is_a_clean_exit(self):
        code, err, started = self.main({"ISUITE_OWNER": "1", "ISUITE_ADMINS": "2",
                                        "ISUITE_TELEGRAM_TOKEN": "t", "ANTHROPIC_API_KEY": "k"})
        self.assertEqual((code, started), (2, []))
        self.assertIn("not both", err)

    def test_main_any_environment_config_means_the_file_is_not_merged(self):
        # the file holds a token, owner and key; the environment has only an API key
        code, err, started = self.main({"ANTHROPIC_API_KEY": "env-key"}, config_file=self.path)
        self.assertEqual((code, started), (2, []))
        self.assertIn("ISUITE_TELEGRAM_TOKEN is not set", err)

    def test_cwd_dot_env_is_never_read(self):
        here = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".env").write_text("ISUITE_TELEGRAM_TOKEN=fromdotenv\n")
            os.chdir(d)
            try:
                code, err, started = self.main({})
            finally:
                os.chdir(here)
        self.assertEqual((code, started), (2, []))
        self.assertIn("ISUITE_TELEGRAM_TOKEN is not set", err)


if __name__ == "__main__":
    unittest.main()
