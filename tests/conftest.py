import pytest

from pdf2epub import web


@pytest.fixture(autouse=True)
def isolated_job_store(tmp_path, monkeypatch):
    """Point every test at an empty job store and registry.

    The client fixture's lifespan runs startup recovery at fixture-setup
    time, before a test body can patch JOB_ROOT itself — so without this,
    the suite scans (and can rewrite) the real store under
    ~/.local/share/pdf2epub. Individual tests may still re-patch JOB_ROOT
    to their own tmp directory; this fixture only guarantees the default
    is never the real one.
    """
    monkeypatch.setattr(web, "JOB_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(web, "jobs", {})
    yield
