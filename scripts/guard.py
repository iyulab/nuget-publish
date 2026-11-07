#!/usr/bin/env python3
"""Guards for the central NuGet publish workflow. Standard library only.

The trusted-publishing policy on nuget.org covers every package of an owner, so the real
authorization lives here: a source repository may publish only what the allowlist says, from
the workflow the allowlist names, and only from a finished run of its own.

The allowlist is the repository variable PUBLISHERS (JSON, see publishers.example.json).

  guard.py plan                    validate SOURCE_REPO, write job outputs
  guard.py run                     validate the source run (needs GH_TOKEN, SOURCE_REPO, RUN_ID)
  guard.py packages <dir>          validate the downloaded packages against the allowlist
"""
import fnmatch
import json
import os
import pathlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

NUGET_OWNERS = ("iyulab", "iyulab-rnd")
SOURCE_RE = re.compile(r"^iyulab/[A-Za-z0-9_.-]+$")
WORKFLOW_RE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")
BLOCKED_EVENTS = ("pull_request", "pull_request_target")
MAX_NUSPEC_BYTES = 1_000_000


def fail(message):
    print(f"::error::{message}")
    sys.exit(1)


def load_entry(source_repo):
    if not SOURCE_RE.match(source_repo):
        fail(f"source-repo must look like iyulab/<name>, got: {source_repo!r}")
    raw = os.environ.get("PUBLISHERS", "").strip()
    if not raw:
        fail("the repository variable PUBLISHERS is not set")
    try:
        publishers = json.loads(raw)["publishers"]
    except (ValueError, KeyError, TypeError):
        fail("PUBLISHERS must be JSON of the form {\"publishers\": {...}}")
    entry = next((v for k, v in publishers.items() if k.lower() == source_repo.lower()), None)
    if entry is None:
        fail(f"{source_repo} is not in the publishers allowlist")
    if entry.get("owner") not in NUGET_OWNERS:
        fail(f"{source_repo}: owner must be one of {NUGET_OWNERS}")
    if not WORKFLOW_RE.match(entry.get("workflow", "")):
        fail(f"{source_repo}: workflow must be a file name such as release.yml")
    packages = entry.get("packages")
    if not packages or not all(isinstance(p, str) and p for p in packages):
        fail(f"{source_repo}: packages must be a non-empty list of package ids or globs")
    return entry


def write_output(name, value):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        out.write(f"{name}={value}\n")


def plan():
    source = os.environ["SOURCE_REPO"]
    entry = load_entry(source)
    write_output("owner", entry["owner"])
    write_output("name", source.split("/", 1)[1])
    print(f"{source} may publish {entry['packages']} as {entry['owner']} from {entry['workflow']}")


def run():
    source = os.environ["SOURCE_REPO"]
    run_id = os.environ["RUN_ID"]
    if not run_id.isdigit():
        fail(f"run-id must be numeric, got: {run_id!r}")
    entry = load_entry(source)
    result = subprocess.run(
        ["gh", "api", f"repos/{source}/actions/runs/{run_id}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        fail(f"cannot read run {run_id} of {source}: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    # request.yml asks while the source run is still going (it waits for this workflow), so the run is
    # normally in_progress; a manual request for a finished run needs completed/success. The caller's own
    # `needs` already guarantees that the pack job succeeded.
    finished_ok = data["status"] == "completed" and data["conclusion"] == "success"
    if data["status"] != "in_progress" and not finished_ok:
        fail(f"run {run_id} is {data['status']}/{data['conclusion']}, not in_progress or completed/success")
    if data["event"] in BLOCKED_EVENTS:
        fail(f"run {run_id} was triggered by {data['event']}; pull request runs never publish")
    head_repo = (data.get("head_repository") or {}).get("full_name", "")
    if head_repo.lower() != source.lower():
        fail(f"run {run_id} built code from {head_repo or 'unknown'}, not from {source}")
    path = data["path"].split("@", 1)[0]
    expected = f".github/workflows/{entry['workflow']}"
    if path != expected:
        fail(f"run {run_id} came from {path}, the allowlist names {expected}")
    print(f"run {run_id} of {source}: {data['event']} on {data['head_branch']} via {path}")


def package_identity(path):
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.endswith(".nuspec") and "/" not in n]
        if len(names) != 1:
            fail(f"{path.name}: expected exactly one root .nuspec, found {len(names)}")
        info = archive.getinfo(names[0])
        if info.file_size > MAX_NUSPEC_BYTES:
            fail(f"{path.name}: .nuspec is unreasonably large")
        content = archive.read(info)
    # A .nuspec never needs a DTD or entities; refuse them before parsing (XXE, entity expansion).
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        fail(f"{path.name}: .nuspec declares a DTD or entities, which is not allowed")
    root = ET.fromstring(content)
    meta = next((e for e in root.iter() if e.tag.split("}")[-1] == "metadata"), None)
    if meta is None:
        fail(f"{path.name}: .nuspec has no metadata")
    fields = {e.tag.split("}")[-1]: (e.text or "").strip() for e in meta}
    if not fields.get("id") or not fields.get("version"):
        fail(f"{path.name}: .nuspec lacks id or version")
    return fields["id"], fields["version"]


def packages(directory):
    source = os.environ["SOURCE_REPO"]
    entry = load_entry(source)
    patterns = [p.lower() for p in entry["packages"]]
    files = sorted(p for p in pathlib.Path(directory).rglob("*") if p.is_file())
    if not files:
        fail("the artifact is empty")
    nupkgs = 0
    for path in files:
        if path.suffix not in (".nupkg", ".snupkg"):
            fail(f"unexpected file in the artifact: {path.name} (only .nupkg and .snupkg are allowed)")
        package_id, version = package_identity(path)
        if not any(fnmatch.fnmatchcase(package_id.lower(), pattern) for pattern in patterns):
            fail(f"{source} may not publish {package_id}; allowed: {entry['packages']}")
        print(f"ok  {package_id} {version}  ({path.name})")
        nupkgs += path.suffix == ".nupkg"
    if nupkgs == 0:
        fail("the artifact holds no .nupkg")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "plan":
        plan()
    elif command == "run":
        run()
    elif command == "packages" and len(sys.argv) == 3:
        packages(sys.argv[2])
    else:
        sys.exit(__doc__)
