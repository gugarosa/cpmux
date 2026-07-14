# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from cpmux.engine.interact import followup_argv, resume_interactive_argv


def test_resume_interactive_argv_targets_session_and_worktree():
    assert resume_interactive_argv("sid", "/wt") == ["copilot", "--resume=sid", "-C", "/wt"]


def test_followup_argv_carries_message_and_stays_non_interactive():
    argv = followup_argv("sid", "/wt", "gpt-5.5", ["--allow-tool=write"], "do it")

    assert argv[0] == "copilot"
    assert "--resume=sid" in argv
    assert argv[argv.index("-p") + 1] == "do it"
    assert argv[argv.index("--model") + 1] == "gpt-5.5"
    assert argv[argv.index("-C") + 1] == "/wt"
    assert "--allow-tool=write" in argv
    assert "--output-format" in argv
    assert "--no-ask-user" in argv


def test_followup_argv_keeps_single_no_ask_user():
    argv = followup_argv("s", "/w", "m", ["--no-ask-user"], "x")
    assert argv.count("--no-ask-user") == 1
