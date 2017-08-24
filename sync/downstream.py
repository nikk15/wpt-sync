# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import absolute_import, print_function

"""Functionality to support VCS syncing for WPT."""

import os
import re
import subprocess
import shutil
import traceback
import uuid
from collections import defaultdict

import git

from . import (
    settings,
    log,
    model
)

from .model import (
    session_scope,
    PullRequest,
    Repository,
    Sync,
    SyncDirection,
)
from .projectutil import Mach, WPT
from .worktree import ensure_worktree, remove_worktrees

rev_re = re.compile("revision=(?P<rev>[0-9a-f]{40})")
logger = log.get_logger("downstream")


def new_wpt_pr(config, session, git_gecko, git_wpt, bz, body):
    pr_id = body['payload']['pull_request']['number']
    pr_data = body["payload"]["pull_request"]
    bug = bz.new(summary="[wpt-sync] PR {} - {}".format(pr_id, pr_data["title"]),
                 comment=pr_data["body"],
                 product="Testing",
                 component="web-platform-tests")

    with session_scope(session):
        pr = PullRequest(id=pr_id)
        session.add(pr)
        sync = Sync(direction=SyncDirection.downstream, pr=pr,
                    repository=Repository.by_name(session, "web-platform-tests"))
        session.add(sync)
        sync.bug = bug



def commit_manifest_update(gecko_work):
    gecko_work.git.reset("HEAD", hard=True)
    mach = Mach(gecko_work.working_dir)
    mach.wpt_manifest_update()
    if gecko_work.is_dirty:
        gecko_work.git.add("testing/web-platform/meta")
        gecko_work.git.commit(message="[wpt-sync] downstream {}: update manifest".format(
            gecko_work.active_branch.name))


def get_files_changed(project_path):
    wpt = WPT(project_path)
    output = wpt.files_changed()
    return set(output.split("\n"))


def choose_bug_component(config, git_gecko, files_changed, default):
    if not files_changed:
        return default

    path_prefix = config["gecko"]["path"]["wpt"]
    paths = [os.path.join(path_prefix, item) for item in files_changed]

    mach = Mach(git_gecko.working_dir)
    output = mach.file_info("bugzilla-component", *paths)

    components = defaultdict(int)
    current = None
    for line in output.split("\n"):
        if line.startswith(" "):
            assert current is not None
            components[current] += 1
        else:
            current = line.strip()

    if not components:
        return default

    components = sorted(components.items(), key=lambda x:-x[1])
    component = components[0][0]
    if component == "UNKNOWN" and len(components) > 1:
        component = components[1][0]

    if component == "UNKNOWN":
        return default

    return component.split(" :: ")


def is_worktree_tip(repo, worktree_path, rev):
    if not worktree_path:
        return False
    branch_name = os.path.basename(worktree_path)
    if branch_name in repo.heads:
        return repo.heads[branch_name].commit.hexsha == rev
    return False


def status_changed(config, session, bz, git_gecko, git_wpt, sync, event):
    if event["context"] != "continuous-integration/travis-ci/pr":
        logger.info("Ignoring status for context {}".format(event["context"]))
        return
    if event["state"] == "pending":
        if not is_worktree_tip(git_wpt, sync.wpt_worktree, event["sha"]):
            update_sync(config, session, git_gecko, git_wpt, sync, bz)
            # TODO address any existing try pushes for this sync
    elif event["state"] == "passed":
        return
        # TODO: if the status is "passed" this should start a try run
        # TODO: check only for status of Firefox job(s)


def update_sync(config, session, git_gecko, git_wpt, sync, bz):
    with session_scope(session):
        try:
            wpt_work, wpt_branch = get_pr(config, session, git_wpt, sync)
        except git.GitCommandError as e:
            logger.error("Failed to obtain web-platform-tests PR {}:\n{}".format(sync.pr_id, e))
            bz.comment(sync.bug,
                       "Downstreaming from web-platform-tests failed because obtaining "
                       "PR {} failed:\n{}".format(sync.pr_id, e))
            return False

        files_changed = get_files_changed(wpt_work.working_dir)

        logger.info("Fetching mozilla-unified")
        git_gecko.git.fetch("mozilla")
        logger.info("Fetch done")
        gecko_work, gecko_branch = ensure_worktree(
            config, session, git_gecko, "gecko", sync,
            "PR_" + str(sync.pr_id), config["gecko"]["refs"]["central"])
        gecko_work.index.reset(config["gecko"]["refs"]["central"], hard=True)
        success = wpt_to_gecko_commits(config, wpt_work, gecko_work, sync, bz)
        if not success:
            return
        commit_manifest_update(gecko_work)

        # Getting the component now doesn't really work well because a PR that only adds files
        # won't have a component before we move the commits. But we would really like to start
        # a bug for logging now, so get a component here and change it if needed after we have
        # put all the new commits onto the gecko tree.
        new_component = choose_bug_component(config, gecko_work, files_changed,
                                             default=("Testing", "web-platform-tests"))
        bz.set_component(sync.bug, *new_component)


def get_pr(config, session, git_wpt, sync):
    logger.info("Fetching web-platform-tests origin/master")
    git_wpt.git.fetch("origin", "master", no_tags=True)
    logger.info("Fetch done")
    wpt_work, branch_name = ensure_worktree(config, session, git_wpt, "web-platform-tests", sync,
                                            "PR_" + str(sync.pr_id), "origin/master")
    wpt_work.index.reset("origin/master", hard=True)
    wpt_work.git.fetch("origin", "pull/{}/head:heads/pull_{}".format(sync.pr_id, sync.pr_id),
                       no_tags=True)
    wpt_work.git.merge("heads/pull_{}".format(sync.pr_id))
    return wpt_work, branch_name


def wpt_to_gecko_commits(config, wpt_work, gecko_work, sync, bz):
    """Create a patch based on wpt branch, apply it to corresponding gecko branch"""
    assert wpt_work.active_branch.name == os.path.basename(sync.wpt_worktree)
    assert gecko_work.active_branch.name == os.path.basename(sync.gecko_worktree)

    for c in wpt_work.iter_commits("origin/master..", reverse=True):
        try:
            patch = wpt_work.git.show(c, pretty="email") + "\n"
            if patch.endswith("\n\n\n"):
                # skip empty patch
                continue
        except git.GitCommandError as e:
            logger.error("Failed to create patch from {}:\n{}".format(c.hexsha, e))
            bz.comment(sync.bug,
                       "Downstreaming from web-platform-tests failed because creating patch "
                       "from {} failed:\n{}".format(c.hexsha, e))
            return False

        try:
            proc = gecko_work.git.am("--directory=" + config["gecko"]["path"]["wpt"], "-",
                                     istream=subprocess.PIPE,
                                     as_process=True)
            stdout, stderr = proc.communicate(patch)
            if proc.returncode != 0:
                raise git.GitCommandError(
                    ["am", "--directory=" + config["gecko"]["path"]["wpt"], "-"],
                    proc.returncode, stderr, stdout)
        except git.GitCommandError as e:
            logger.error("Failed to import patch downstream {}\n\n{}\n\n{}".format(
                c.hexsha, patch, e))
            bz.comment(sync.bug,
                       "Downstreaming from web-platform-tests failed because applying patch "
                       "from {} failed:\n{}".format(c.hexsha, e))
            return False
    return True


def get_affected_tests(path_to_wpt, revish=None):
    tests_by_type = defaultdict(set)
    wpt = WPT(path_to_wpt)
    wpt.manifest()
    args = ["--show-type", "--new"]
    if revish:
        args.append(revish)
    s = wpt.tests_affected(*args)
    if s is not None:
        for item in s.strip().split("\n"):
            pair = item.strip().split("\t")
            assert len(pair) == 2
            tests_by_type[pair[1]].add(pair[0])
    return tests_by_type


def construct_try_message(tests_by_type):
    # Example: try: -b do -p win32,win64,linux64,linux,macosx64 -u web-platform-tests[linux64-stylo,Ubuntu,10.10,Windows 7,Windows 8,Windows 10] -t none --artifact
    try_message = ("try: -b do -p win32,win64,linux64,linux -u {test_jobs} "
                   "-t none --artifact --try-test-paths {prefixed_paths}")
    test_type_suite = {
        "testharness": "web-platform-tests",
        "reftest": "web-platform-tests-reftests",
        "wdspec": "web-platform-tests-wdspec",
    }
    platform_suffix = "[linux64-stylo,Ubuntu,10.10,Windows 7,Windows 8,Windows 10]"
    test_data = {
        "test_jobs": [],
        "prefixed_paths": [],
    }
    # TODO? support files, harness changes -- don't want to update metadata
    for test_type, paths in tests_by_type.iteritems():
        suite = test_type_suite[test_type]
        if len(paths):
            machines = platform_suffix if suite == "web-platform-tests" else ""
            test_data["test_jobs"].append(suite + machines)
            test_data["test_jobs"].append(suite + "-e10s" + machines)
        for p in paths:
            test_data["prefixed_paths"].append(suite + ":" + p)
    test_data["test_jobs"] = ",".join(test_data["test_jobs"])
    test_data["prefixed_paths"] = ",".join(test_data["prefixed_paths"])
    return try_message.format(**test_data)


def push_to_try(git_work, branch):
    # TODO determine affected tests (use new wpt command in upstream repo)
    affected_tests = [
        "testing/web-platform/tests/webdriver",
        "testing/web-platform/tests/2dcontext",
        "testing/web-platform/tests/cookies",
    ]
    results_url = None
    # TODO only push to try on mac if necessary
    try_message = ("try: -b do -p linux,linux64 -u web-platform-tests-1,web-platform-tests-e10s-1 "
                   "-t none --artifact --try-test-paths ")
    try_message += "".join(["web-platform-tests:" + t for t in affected_tests])
    git_work.git(b'checkout', branch)
    git_work.git(b'commit', b'--allow-empty', b'-m', try_message)
    try:
        status, stdout, stderr = git_work.git(b'push', b'try', with_extended_output=True)
        # Not sure if this should be stdout or stderr
        rev_match = rev_re.search(stdout)
        results_url = ("https://treeherder.mozilla.org/#/"
                       "jobs?repo=try&revision=") + rev_match.group('rev')
    finally:
        git_work.git.cmd(b'reset', b'HEAD~')
    # TODO also return task_group_id
    return results_url


@settings.configure
def main(config):
    import handlers
    session, git_gecko, git_wpt, gh_wpt, bz = handlers.setup(config)
    try:
        model.create()
        wpt_repository, _ = model.get_or_create(session, model.Repository,
            name="web-platform-tests")
        gecko_repository, _ = model.get_or_create(session, model.Repository,
            name="gecko")
        body = {
            "payload": {
                "pull_request": {
                    "number": 9,
                    "title": "Test PR",
                    "body": "blah blah body"
                },
            },
        }
        pr_id = body["payload"]["pull_request"]["number"]
        # new pr opened
        new_wpt_pr(config, session, git_gecko, git_wpt, bz, body)
        sync = session.query(model.Sync).filter(Sync.pr == pr_id).first()
        status_event = {
            "sha": "409018c0a562e1b47d97b53428bb7650f763720d",
            "state": "pending",
            "context": "continuous-integration/travis-ci/pr",
        }
        status_changed(config, session, bz, git_gecko, git_wpt, sync, status_event)
        # should do nothing second time
        status_changed(config, session, bz, git_gecko, git_wpt, sync, status_event)
    except Exception:
        traceback.print_exc()
        import pdb
        pdb.post_mortem()

if __name__ == '__main__':
    main()
