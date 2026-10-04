"""Tests for [mold.tasks.*]: pulling a task in from the loaded mold."""

import pytest

from bloomery.config import resolve_tasks
from bloomery.errors import MoldTaskError


def test_no_mold_tasks_declared_is_a_no_op():
    config = {"tasks": {"build": {"command": "echo hi"}}}
    assert resolve_tasks(config, mold_config=None) == config["tasks"]


def test_bare_declaration_pulls_the_mold_task_verbatim():
    config = {"mold": {"tasks": {"build": {}}}}
    mold = {"tasks": {"build": {"command": "g++", "flags": "-std=c++17"}}}
    tasks = resolve_tasks(config, mold)
    assert tasks["build"] == {"command": "g++", "flags": "-std=c++17"}


def test_declared_keys_override_the_mold_tasks_keys():
    config = {"mold": {"tasks": {"build": {"flags": "-g -O0"}}}}
    mold = {"tasks": {"build": {"command": "g++", "flags": "-std=c++17"}}}
    tasks = resolve_tasks(config, mold)
    assert tasks["build"] == {"command": "g++", "flags": "-g -O0"}


def test_plain_tasks_pass_through_untouched():
    config = {
        "tasks": {"clean": {"command": "rm -f out"}},
        "mold": {"tasks": {"build": {}}},
    }
    mold = {"tasks": {"build": {"command": "g++"}}}
    tasks = resolve_tasks(config, mold)
    assert tasks["clean"] == {"command": "rm -f out"}
    assert tasks["build"] == {"command": "g++"}


def test_name_declared_in_both_tasks_and_mold_tasks_is_an_error():
    config = {
        "tasks": {"build": {"command": "custom"}},
        "mold": {"tasks": {"build": {}}},
    }
    mold = {"tasks": {"build": {"command": "g++"}}}
    with pytest.raises(MoldTaskError, match="declared in both"):
        resolve_tasks(config, mold)


def test_mold_missing_the_declared_task_lists_whats_available():
    config = {"mold": {"tasks": {"nope": {}}}}
    mold = {"tasks": {"build": {}, "run": {}}}
    with pytest.raises(MoldTaskError, match="build, run"):
        resolve_tasks(config, mold)


def test_mold_tasks_declared_without_a_loaded_mold_is_an_error():
    config = {"mold": {"tasks": {"build": {}}}}
    with pytest.raises(MoldTaskError, match="no mold is loaded"):
        resolve_tasks(config, mold_config=None)
