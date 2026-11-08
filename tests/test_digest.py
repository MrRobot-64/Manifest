import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.digest import (UNKNOWN, UP_TO_DATE, UPDATE_AVAILABLE, compare_digests,
                        map_portainer_status, parse_image_ref, parse_www_authenticate,
                        pick_image_reference)

A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64


def test_parse_dockerhub_official():
    r = parse_image_ref("nginx")
    assert (r.registry, r.repo, r.tag) == ("registry-1.docker.io", "library/nginx", "latest")


def test_parse_user_repo_with_tag():
    r = parse_image_ref("linuxserver/plex:1.40")
    assert (r.repo, r.tag) == ("linuxserver/plex", "1.40")


def test_parse_ghcr_and_port_registry():
    assert parse_image_ref("ghcr.io/o/r:v1").registry == "ghcr.io"
    r = parse_image_ref("myhost:5000/app")
    assert (r.registry, r.repo, r.tag) == ("myhost:5000", "app", "latest")


def test_parse_digest_pinned():
    r = parse_image_ref(f"nginx@{A}")
    assert r.digest == A and r.tag is None


def test_up_to_date():
    ref = parse_image_ref("nginx:latest")
    assert compare_digests(ref, [f"nginx@{A}"], A) == (UP_TO_DATE, "")


def test_update_available():
    ref = parse_image_ref("nginx:latest")
    assert compare_digests(ref, [f"nginx@{A}"], B)[0] == UPDATE_AVAILABLE


def test_any_matching_repo_digest_counts():
    ref = parse_image_ref("ghcr.io/o/r:v1")
    assert compare_digests(ref, [f"ghcr.io/o/r@{A}", f"ghcr.io/o/r@{B}"], B)[0] == UP_TO_DATE


def test_other_repo_digests_ignored():
    ref = parse_image_ref("ghcr.io/o/r:v1")
    status, reason = compare_digests(ref, [f"ghcr.io/other/x@{B}"], B)
    assert status == UNKNOWN and reason


def test_dockerhub_repo_digest_normalisation():
    ref = parse_image_ref("docker.io/library/nginx")
    assert compare_digests(ref, [f"nginx@{A}"], A)[0] == UP_TO_DATE


def test_unknown_cases_have_reasons():
    ref = parse_image_ref("nginx")
    assert compare_digests(ref, None, A)[0] == UNKNOWN
    assert compare_digests(ref, [f"nginx@{A}"], None)[0] == UNKNOWN
    assert compare_digests(parse_image_ref(f"nginx@{A}"), [f"nginx@{A}"], A)[0] == UNKNOWN
    assert all(compare_digests(ref, x, y)[1] for x, y in [(None, A), ([f"nginx@{A}"], None)])


def test_pick_image_reference_prefers_named_list_entry():
    assert pick_image_reference("nginx:1.25", ["nginx:other"], "nginx:cfg") == "nginx:1.25"


def test_pick_image_reference_recovers_name_when_tag_moved():
    # container list shows a bare ID once the tag points at a newer image
    assert pick_image_reference("sha256:" + "a" * 64, [], "lscr.io/linuxserver/bazarr:latest") \
        == "lscr.io/linuxserver/bazarr:latest"
    # config wins over a stray tag on the old image
    assert pick_image_reference("sha256:abc", ["x:1.2.3"], "x:latest") == "x:latest"
    # no config: fall back to a tag on the old image
    assert pick_image_reference("sha256:abc", ["x:1.2.3"], None) == "x:1.2.3"
    assert pick_image_reference("", ["x:1.2.3"], "sha256:def") == "x:1.2.3"


def test_pick_image_reference_gives_up_on_bare_ids():
    assert pick_image_reference("sha256:abc", [], None) is None
    assert pick_image_reference("sha256:abc", ["<none>:<none>"], "sha256:def") is None
    assert pick_image_reference(None, None, None) is None


def test_stale_container_is_flagged_once_the_reference_is_recovered():
    """The scenario from the live site: the old image keeps its RepoDigest but the tag moved on."""
    ref = parse_image_ref(pick_image_reference("sha256:" + "a" * 64, [], "lscr.io/linuxserver/bazarr:latest"))
    old_local = [f"lscr.io/linuxserver/bazarr@{A}"]
    assert compare_digests(ref, old_local, B)[0] == UPDATE_AVAILABLE
    assert compare_digests(ref, old_local, A)[0] == UP_TO_DATE


def test_portainer_status_mapping():
    assert map_portainer_status("updated") == UP_TO_DATE
    assert map_portainer_status("Outdated") == UPDATE_AVAILABLE
    for s in ("skipped", "error", "processing", "preparing", "", None):
        assert map_portainer_status(s) is None


def test_www_authenticate():
    h = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'
    assert parse_www_authenticate(h) == ("bearer", {"realm": "https://auth.docker.io/token", "service": "registry.docker.io"})
    assert parse_www_authenticate("") is None
