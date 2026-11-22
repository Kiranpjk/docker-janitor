import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from docker_janitor.cli import main
from docker_janitor.core import DockerCLI, Item, Policy, parse_size, parse_ts, plan

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def container(cid, image, status="exited", days=30, labels=None, mounts=()):
    return {
        "Id": cid,
        "Name": f"/{cid}",
        "Image": image,
        "Created": ts(days + 1),
        "State": {"Status": status, "FinishedAt": ts(days) if status != "running" else "0001-01-01T00:00:00Z"},
        "Config": {"Labels": labels or {}},
        "Mounts": [{"Type": "volume", "Name": m} for m in mounts],
        "SizeRw": 1000,
    }


def image(iid, tags=(), days=30, size=100_000_000, labels=None):
    return {"Id": iid, "RepoTags": list(tags), "Created": ts(days), "Size": size, "Config": {"Labels": labels}}


def volume(name, days=30, labels=None):
    return {"Name": name, "CreatedAt": ts(days).replace(".123456789Z", "Z"), "Labels": labels}


ANON = "a" * 64


def kinds(items):
    return [(i.kind, i.id) for i in items]


def test_parse_helpers():
    assert parse_ts("2026-11-22T23:42:29.211252429Z") == datetime(2026, 11, 22, 23, 42, 29, 211252, timezone.utc)
    assert parse_ts("2026-11-22T23:42:29+01:00").utcoffset() == timedelta(hours=1)
    assert parse_ts("0001-01-01T00:00:00Z") is None
    assert parse_size("8.19kB") == 8190
    assert parse_size("1.5GB") == 1_500_000_000
    assert parse_size("N/A") is None


def test_stopped_containers_and_their_images_are_removed_in_order():
    items = plan(
        [container("old", "sha256:app")],
        [image("sha256:app", ["app:1"], days=30), image("sha256:app2", ["app:2"]), image("sha256:app3", ["app:3"])],
        [],
        Policy(keep_last=2),
        NOW,
    )
    # app:1 is the oldest of three tags, so it is outside the newest 2
    assert kinds(items) == [("container", "old"), ("image", "sha256:app")]


def test_running_and_recent_containers_are_kept_and_protect_images():
    items = plan(
        [container("web", "sha256:web", status="running"), container("recent", "sha256:job", days=1)],
        [image("sha256:web", ["web:old"], days=400), image("sha256:job", [], days=400)],
        [],
        Policy(keep_last=0),
        NOW,
    )
    assert items == []


def test_keep_last_per_repo():
    imgs = [image(f"sha256:{n}", [f"api:{n}"], days=10 + n) for n in range(5)]
    items = plan([], imgs, [], Policy(keep_last=2), NOW)
    assert sorted(i.id for i in items) == ["sha256:2", "sha256:3", "sha256:4"]


def test_young_images_kept_dangling_removed():
    imgs = [image("sha256:new", ["x:1"], days=1), image("sha256:x2", ["x:2"], days=2), image("sha256:dang", [], days=0)]
    items = plan([], imgs, [], Policy(keep_last=0, older_than_days=7), NOW)
    assert kinds(items) == [("image", "sha256:dang")]
    assert items[0].targets == ["sha256:dang"]


def test_keep_patterns_and_labels():
    imgs = [
        image("sha256:pg", ["postgres:15"]),
        image("sha256:lbl", ["tool:1"], labels={"janitor.keep": "true"}),
        image("sha256:gone", ["tool:0"]),
    ]
    items = plan(
        [container("c", "sha256:none", labels={"janitor.keep": "yes"})], imgs, [], Policy(0, 0, ("postgres:*",))
    )
    assert kinds(items) == [("image", "sha256:gone")]


def test_multi_tag_image_removed_by_tags():
    items = plan([], [image("sha256:m", ["a:1", "registry:5000/a:1"])], [], Policy(keep_last=0), NOW)
    assert items[0].targets == ["a:1", "registry:5000/a:1"]


def test_volumes_are_opt_in_and_anonymous_only_by_default():
    vols = [volume(ANON), volume("pgdata"), volume("b" * 64, days=1), volume("c" * 64)]
    cons = [container("db", "sha256:pg", status="running", mounts=["c" * 64])]
    assert plan(cons, [], vols, Policy(), NOW) == []
    items = plan(cons, [], vols, Policy(volumes=True), NOW, volume_sizes={ANON: 4096})
    assert kinds(items) == [("volume", ANON)] and items[0].size == 4096
    items = plan(cons, [], vols, Policy(volumes=True, named_volumes=True), NOW)
    assert {i.id for i in items} == {ANON, "pgdata"}


def test_volume_freed_when_its_container_is_removed():
    cons = [container("old", "sha256:x", mounts=[ANON])]
    items = plan(cons, [], [volume(ANON)], Policy(volumes=True), NOW)
    assert kinds(items) == [("container", "old"), ("volume", ANON)]


class FakeDocker:
    def __init__(self, fail_on=()):
        self.removed = []
        self.fail_on = fail_on

    def containers(self):
        return [container("old", "sha256:app")]

    def images(self):
        return [image("sha256:app", ["app:1"], days=30)]

    def volumes(self):
        return []

    def volume_sizes(self):
        return {}

    def root_dir(self):
        return "/"

    def remove(self, item: Item):
        if item.kind in self.fail_on:
            raise RuntimeError("conflict")
        self.removed.append(item.id)


def test_cli_dry_run_deletes_nothing(capsys):
    fake = FakeDocker()
    assert main(["--keep-last", "0"], docker=fake) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "app:1" in out
    assert fake.removed == []


def test_cli_apply_and_json(capsys):
    fake = FakeDocker()
    assert main(["--keep-last", "0", "--apply", "--json"], docker=fake) == 0
    data = json.loads(capsys.readouterr().out.split("\nremoved")[0])
    assert [d["kind"] for d in data] == ["container", "image"]
    assert fake.removed == ["old", "sha256:app"]


def test_cli_apply_reports_failures():
    fake = FakeDocker(fail_on=("image",))
    assert main(["--keep-last", "0", "--apply"], docker=fake) == 1
    assert fake.removed == ["old"]


def test_cli_disk_threshold_skips(capsys):
    assert main(["--only-if-above", "100"], docker=FakeDocker()) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_docker_cli_wrapper_builds_commands():
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        out = {"ps": "id2\nid1\n", "container": json.dumps([{"Id": "id1"}])}.get(cmd[1], "")
        return subprocess.CompletedProcess(cmd, 0, out, "")

    cli = DockerCLI(runner=runner)
    assert cli.containers() == [{"Id": "id1"}]
    assert calls[1] == ["docker", "container", "inspect", "--size", "id1", "id2"]
    assert cli.images() == []  # empty list: no inspect call
    cli.remove(Item("image", "sha256:x", "a:1", None, 0, "", ["a:1", "b:1"]))
    assert calls[-1] == ["docker", "image", "rm", "a:1", "b:1"]


def test_docker_cli_errors_raise():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "Cannot connect to the Docker daemon")

    with pytest.raises(RuntimeError, match="Cannot connect"):
        DockerCLI(runner=runner).containers()
    assert DockerCLI(runner=runner).volume_sizes() == {}
