"""Keeps the version number consistent everywhere, so a release can't ship with mismatched numbers."""
import importlib.util
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


from app.version import __version__


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_dockerfile_label_matches():
    assert f'org.opencontainers.image.version="{__version__}"' in read("Dockerfile")


def test_changelog_has_current_version_first():
    versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", read("CHANGELOG.md"), re.MULTILINE)
    assert versions and versions[0] == __version__, f"top changelog entry {versions[:1]} != {__version__}"


def test_release_notes_script_extracts_section():
    spec = importlib.util.spec_from_file_location("release_notes", os.path.join(ROOT, "scripts", "release-notes.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = mod.section(read("CHANGELOG.md"), __version__)
    assert text and "### Added" in text and "## [" not in text and "github.com/" not in text
    assert mod.section(read("CHANGELOG.md"), "9.9.9") is None
