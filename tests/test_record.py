"""Tests for the SubagentStop `record` hook, in throwaway git repos.

Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("hk", os.path.join(HERE, "..", ".claude", "hooks", "harness-kit.py"))
hk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hk)

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, env=ENV, check=True, capture_output=True)


def make_repo(path):
    os.makedirs(path)
    sh(path, "git", "init", "-q")
    name = os.path.basename(path)  # distinct content, so every repo has its own fingerprint
    with open(os.path.join(path, "a.txt"), "w") as f:
        f.write(f"one {name}\n")
    sh(path, "git", "add", "-A")
    sh(path, "git", "commit", "-q", "-m", f"first {name}")
    with open(os.path.join(path, "a.txt"), "w") as f:
        f.write(f"two {name}\n")  # a pending change


def verdict(text, fp, findings=""):
    return f"VERDICT: {text}\nREVIEWED: {fp}\nFINDINGS:\n{findings}"


class Record(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logfile = os.path.join(tempfile.gettempdir(), f"hk-test-{os.getpid()}.log")
        os.environ["HARNESS_KIT_LOG"] = self.logfile
        self.parent = os.path.realpath(self.tmp.name)  # not a repo
        self.a = os.path.join(self.parent, "app")
        self.b = os.path.join(self.parent, "blog")
        make_repo(self.a)
        make_repo(self.b)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("HARNESS_KIT_LOG", None)
        if os.path.exists(self.logfile):
            os.unlink(self.logfile)

    def run_record(self, cwd, message, agent="skeptic"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = hk.record({"agent_type": agent, "cwd": cwd, "last_assistant_message": message})
        self.assertEqual(rc, 0)
        return out.getvalue()

    def transcript(self, *entries):
        """A subagent transcript (JSONL) like Claude Code writes; each entry is (role, content)."""
        path = os.path.join(self.parent, "agent.jsonl")
        with open(path, "w") as f:
            for role, content in entries:
                f.write(json.dumps({"type": role, "message": {"role": role, "content": content}}) + "\n")
        return path

    def run_record_from_transcript(self, cwd, path, agent="skeptic"):
        """What current Claude Code sends: a transcript path and no last_assistant_message."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hk.record({"agent_type": agent, "cwd": cwd, "agent_transcript_path": path, "hook_event_name": "SubagentStop"})
        return out.getvalue()

    def test_the_final_message_is_read_from_the_transcript_when_the_harness_sends_only_a_path(self):
        fp = hk.fingerprint(self.a)
        path = self.transcript(
            ("user", "review this"),
            ("assistant", [{"type": "thinking", "thinking": "hmm"}, {"type": "tool_use", "name": "Bash", "input": {}}]),
            ("user", [{"type": "tool_result", "content": "ok"}]),
            ("assistant", [{"type": "text", "text": verdict("approved", fp, "- [minor] x:1 - nit")}]),
        )
        out = self.run_record_from_transcript(self.parent, path)
        self.assertIn("approval recorded", out)
        self.assertTrue(hk.is_approved(self.a))

    def test_the_report_in_a_subagent_handback_tool_call_is_read(self):
        # What current Claude Code writes: the final report is the input of a SubagentHandback call.
        fp = hk.fingerprint(self.a)
        path = self.transcript(
            ("user", "review this"),
            ("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}]),
            ("user", [{"type": "tool_result", "content": "ok"}]),
            ("assistant", [{"type": "thinking", "thinking": "done"},
                           {"type": "tool_use", "name": "SubagentHandback", "input": {"message": verdict("approved", fp)}}]),
            ("user", [{"type": "tool_result", "content": "delivered"}]),
        )
        out = self.run_record_from_transcript(self.parent, path)
        self.assertIn("approval recorded", out)
        self.assertTrue(hk.is_approved(self.a))

    def test_a_later_handback_rejection_beats_an_earlier_handback_approval(self):
        fp = hk.fingerprint(self.a)
        path = self.transcript(
            ("assistant", [{"type": "tool_use", "name": "SubagentHandback", "input": {"message": verdict("approved", fp)}}]),
            ("assistant", [{"type": "tool_use", "name": "SubagentHandback",
                            "input": {"message": verdict("rejected", fp, "- [major] x:1 - broken")}}]),
        )
        self.run_record_from_transcript(self.parent, path)
        self.assertFalse(hk.is_approved(self.a))

    def test_a_bash_tool_call_is_not_mistaken_for_a_report(self):
        fp = hk.fingerprint(self.a)
        path = self.transcript(
            ("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "echo " + verdict("approved", fp)}}]),
        )
        out = self.run_record_from_transcript(self.parent, path)
        self.assertIn("did not approve", out)
        self.assertFalse(hk.is_approved(self.a))

    def test_the_last_message_wins_over_an_earlier_approval_in_the_transcript(self):
        fp = hk.fingerprint(self.a)
        path = self.transcript(
            ("assistant", [{"type": "text", "text": verdict("approved", fp)}]),
            ("user", "now look again"),
            ("assistant", [{"type": "text", "text": verdict("rejected", fp, "- [major] x:1 - broken")}]),
        )
        out = self.run_record_from_transcript(self.parent, path)
        self.assertIn("did not approve", out)
        self.assertFalse(hk.is_approved(self.a))

    def test_a_missing_or_unreadable_transcript_records_nothing(self):
        out = self.run_record_from_transcript(self.parent, os.path.join(self.parent, "nope.jsonl"))
        self.assertIn("did not approve", out)
        self.assertFalse(hk.is_approved(self.a))
        out = self.run_record_from_transcript(self.parent, None)
        self.assertIn("did not approve", out)

    def test_a_string_content_message_in_the_transcript_is_read_too(self):
        fp = hk.fingerprint(self.a)
        path = self.transcript(("assistant", verdict("approved", fp)))
        self.run_record_from_transcript(self.parent, path)
        self.assertTrue(hk.is_approved(self.a))

    def test_session_started_in_a_parent_folder_approves_the_matching_repo_only(self):
        fp = hk.fingerprint(self.a)
        out = self.run_record(self.parent, verdict("approved", fp))
        self.assertIn("approval recorded", out)
        self.assertIn(self.a, out)
        self.assertTrue(hk.is_approved(self.a))
        self.assertFalse(hk.is_approved(self.b))

    def test_the_other_repo_is_chosen_by_its_own_fingerprint(self):
        self.run_record(self.parent, verdict("approved", hk.fingerprint(self.b)))
        self.assertTrue(hk.is_approved(self.b))
        self.assertFalse(hk.is_approved(self.a))

    def test_session_started_in_the_repo_still_works(self):
        self.run_record(self.a, verdict("approved", hk.fingerprint(self.a)))
        self.assertTrue(hk.is_approved(self.a))

    def test_a_stale_fingerprint_records_nothing(self):
        fp = hk.fingerprint(self.a)
        with open(os.path.join(self.a, "a.txt"), "w") as f:
            f.write("three\n")  # edited after the review
        out = self.run_record(self.parent, verdict("approved", fp))
        self.assertIn("differs from what it reviewed", out)
        self.assertFalse(hk.is_approved(self.a))

    def test_a_missing_reviewed_line_records_nothing(self):
        out = self.run_record(self.parent, "VERDICT: approved\nFINDINGS:\n")
        self.assertIn("differs from what it reviewed", out)
        self.assertFalse(hk.is_approved(self.a))

    def test_rejected_or_serious_findings_record_nothing_and_clear_an_old_approval(self):
        fp = hk.fingerprint(self.a)
        hk.write_marker(self.a, fp)
        self.assertTrue(hk.is_approved(self.a))
        out = self.run_record(self.parent, verdict("rejected", fp))
        self.assertIn("did not approve", out)
        self.assertFalse(hk.is_approved(self.a))
        out = self.run_record(self.parent, verdict("approved", fp, "- [major] x.py:1 - broken"))
        self.assertIn("did not approve", out)
        self.assertFalse(hk.is_approved(self.a))

    def test_minor_findings_still_approve(self):
        fp = hk.fingerprint(self.a)
        self.run_record(self.parent, verdict("approved", fp, "- [minor] x.py:1 - nit"))
        self.assertTrue(hk.is_approved(self.a))

    def test_no_repo_anywhere_says_so_and_does_not_crash(self):
        with tempfile.TemporaryDirectory() as empty:
            out = self.run_record(os.path.realpath(empty), verdict("approved", hk.fingerprint(self.a)))
        self.assertIn("no git repository was found", json.loads(out)["systemMessage"])
        self.assertFalse(hk.is_approved(self.a))

    def test_repos_two_levels_down_are_found_but_dot_folders_are_not(self):
        nested = os.path.join(self.parent, "group", "deep")
        make_repo(nested)
        hidden = os.path.join(self.parent, ".cache", "x")
        make_repo(hidden)
        found = hk.child_repos(self.parent)
        self.assertIn(nested, found)
        self.assertNotIn(hidden, found)

    def test_a_same_size_edit_right_after_the_index_was_written_changes_the_fingerprint(self):
        # A general guard. It does not fail with the old shutil.copyfile on its own: the flake that copy2
        # fixed (the whole suite failing 6 runs in 40, 0 in 40 after) came from the other tests' timing.
        for i in range(25):
            repo = os.path.join(self.parent, f"racy{i}")
            os.makedirs(repo)
            sh(repo, "git", "init", "-q")
            path = os.path.join(repo, "f.txt")
            with open(path, "w") as f:
                f.write("aaaa\n")
            sh(repo, "git", "add", "-A")
            before = hk.fingerprint(repo)
            with open(path, "w") as f:
                f.write("bbbb\n")  # same size, same second
            self.assertNotEqual(hk.fingerprint(repo), before, f"edit missed in repo {i}")

    # ------------------------------------------------------------ review findings

    def handback(self, message):
        return ("assistant", [{"type": "tool_use", "name": "SubagentHandback", "input": {"message": message}}])

    def test_an_earlier_draft_approval_is_not_the_final_report_when_the_agent_never_finished(self):
        fp = hk.fingerprint(self.a)
        draft = ("assistant", [{"type": "text", "text": verdict("approved", fp)}])
        for tail in (
            [("assistant", [{"type": "tool_use", "name": "Bash", "input": {}}]),
             ("user", [{"type": "tool_result", "content": "BUG FOUND"}]),
             ("assistant", [{"type": "thinking", "thinking": "hmm"}, {"type": "tool_use", "name": "Bash", "input": {}}])],
            [("assistant", [{"type": "thinking", "thinking": "hmm"}])],
            [self.handback("   ")],  # a blank handback
            [("assistant", [{"type": "tool_use", "name": "SubagentHandback", "input": "not a dict"}])],
        ):
            path = self.transcript(draft, *tail)
            out = self.run_record_from_transcript(self.parent, path)
            self.assertIn("did not approve", out)
            self.assertFalse(hk.is_approved(self.a), tail)

    def test_a_rejection_with_an_unusable_reviewed_line_clears_every_candidate(self):
        hk.write_marker(self.a, hk.fingerprint(self.a))
        hk.write_marker(self.b, hk.fingerprint(self.b))
        out = self.run_record(self.parent, "VERDICT: rejected\nREVIEWED: (hash failed)\nFINDINGS:\n- [blocker] x:1 - bad")
        self.assertIn("did not approve", out)
        self.assertFalse(hk.is_approved(self.a))
        self.assertFalse(hk.is_approved(self.b))

    def test_a_rejection_of_one_repo_leaves_the_approval_of_another_alone(self):
        hk.write_marker(self.b, hk.fingerprint(self.b))
        self.run_record(self.parent, verdict("rejected", hk.fingerprint(self.a), "- [major] x:1 - bad"))
        self.assertTrue(hk.is_approved(self.b))
        self.assertFalse(hk.is_approved(self.a))

    def test_a_stale_approval_clears_only_the_reviewed_repo(self):
        fp = hk.fingerprint(self.a)
        hk.write_marker(self.a, fp)
        hk.write_marker(self.b, hk.fingerprint(self.b))
        with open(os.path.join(self.a, "a.txt"), "w") as f:
            f.write("changed after the review\n")
        self.run_record(self.parent, verdict("approved", fp))
        self.assertFalse(hk.is_approved(self.a))
        self.assertTrue(hk.is_approved(self.b))

    def test_only_repos_with_the_reviewed_head_are_fingerprinted(self):
        fp = hk.fingerprint(self.a)
        with mock.patch.object(hk, "fingerprint", wraps=hk.fingerprint) as spy:
            self.run_record(self.parent, verdict("approved", fp))
        self.assertEqual([c.args[0] for c in spy.call_args_list], [self.a])

    def test_repos_in_the_identical_reviewed_state_are_all_approved(self):
        clone = os.path.join(self.parent, "clone")
        sh(self.parent, "git", "clone", "-q", self.a, clone)
        with open(os.path.join(clone, "a.txt"), "w") as f:
            f.write(f"two {os.path.basename(self.a)}\n")  # the same pending change as in a
        fp = hk.fingerprint(self.a)
        self.assertEqual(fp, hk.fingerprint(clone))
        self.run_record(self.parent, verdict("approved", fp))
        self.assertTrue(hk.is_approved(self.a))
        self.assertTrue(hk.is_approved(clone))
        self.assertFalse(hk.is_approved(self.b))

    def test_symlinked_folders_are_not_followed(self):
        os.symlink(self.b, os.path.join(self.parent, "link"))
        self.assertNotIn(os.path.join(self.parent, "link"), hk.child_repos(self.parent))

    def test_malformed_transcripts_do_not_crash_the_hook(self):
        raw = os.path.join(self.parent, "raw.jsonl")
        with open(raw, "wb") as f:
            f.write(b'\xff\xfe not utf-8\n{"message": {"role": "assistant", "content": [{"type": "text", "text": 5}]}}\n')
        out = self.run_record_from_transcript(self.parent, raw)
        self.assertIn("did not approve", out)
        self.assertFalse(hk.is_approved(self.a))

    def test_every_call_is_logged_without_message_content(self):
        fp = hk.fingerprint(self.a)
        self.run_record(self.parent, verdict("approved", fp, "- [minor] secret-looking text"), agent="verifier")
        self.run_record(self.parent, verdict("approved", fp))
        with open(self.logfile) as f:
            text = f.read()
        self.assertIn("agent_type='verifier'", text)
        self.assertIn("agent_type='skeptic'", text)
        self.assertIn("approval recorded", text)
        self.assertNotIn("secret-looking", text)

    def test_other_agents_are_ignored(self):
        out = self.run_record(self.parent, verdict("approved", hk.fingerprint(self.a)), agent="verifier")
        self.assertEqual(out, "")
        self.assertFalse(hk.is_approved(self.a))

    def test_an_edit_after_approval_voids_it(self):
        self.run_record(self.parent, verdict("approved", hk.fingerprint(self.a)))
        self.assertTrue(hk.is_approved(self.a))
        with open(os.path.join(self.a, "new.txt"), "w") as f:
            f.write("x\n")
        self.assertFalse(hk.is_approved(self.a))


if __name__ == "__main__":
    unittest.main()
