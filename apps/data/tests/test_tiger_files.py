"""`src/geo/tiger_files.download_and_extract` — the TIGER download step every
loader and the county lookup share. No network: `urllib.request.urlopen` is
replaced with an in-memory response.

Behaviors locked in:

- A download streams into ``<zip>.part`` and is renamed onto the cached name
  only once complete, so an interrupted transfer leaves nothing behind and
  the next run downloads again instead of opening a truncated zip.
- A cached file that is not a zip is deleted and the error names its path.
- A cached zip is reused without fetching; a completed download is
  extracted, requested with a browser User-Agent and a cache-busting query.
"""

import io
import re
import urllib.request
import zipfile

import pytest

from src.geo import tiger_files

URL = "https://www2.census.gov/geo/tiger/TIGER2024/COUNTY/tl_2024_us_county.zip"


def _zip_bytes(name: str = "tl_2024_us_county.shp", content: bytes = b"shape") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    return buf.getvalue()


class _Response:
    """Stand-in for the `urlopen` response: a context manager whose `read`
    hands out `chunks` in turn, then raises `error` if given, else EOF."""

    def __init__(self, chunks: list[bytes], error: BaseException | None = None) -> None:
        self.chunks = list(chunks)
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int = -1) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        if self.error is not None:
            raise self.error
        return b""


def _serve(monkeypatch, response):
    seen = []

    def fake_urlopen(req):
        seen.append(req)
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def _boom(*_args, **_kwargs):
    raise AssertionError("network download attempted")


@pytest.fixture()
def paths(tmp_path):
    zip_path = tmp_path / "county" / "tl_2024_us_county.zip"
    extract_dir = tmp_path / "county" / "us"
    return zip_path, extract_dir


def test_interrupted_download_leaves_no_cached_file(paths, monkeypatch):
    zip_path, extract_dir = paths
    _serve(monkeypatch, _Response([b"PK\x03\x04 first chunk"], ConnectionResetError("connection reset")))
    with pytest.raises(ConnectionResetError):
        tiger_files.download_and_extract(URL, zip_path, extract_dir)
    assert not zip_path.exists()
    assert not zip_path.with_name(zip_path.name + ".part").exists()
    assert [p.name for p in zip_path.parent.iterdir()] == ["us"]  # only the (empty) extract dir
    assert list(extract_dir.iterdir()) == []


def test_completed_download_is_renamed_into_place_and_extracted(paths, monkeypatch):
    zip_path, extract_dir = paths
    payload = _zip_bytes()
    seen = _serve(monkeypatch, _Response([payload[:10], payload[10:]]))
    tiger_files.download_and_extract(URL, zip_path, extract_dir)
    assert zip_path.read_bytes() == payload
    assert not zip_path.with_name(zip_path.name + ".part").exists()
    assert [p.name for p in tiger_files.shp_files(extract_dir)] == ["tl_2024_us_county.shp"]
    (request,) = seen
    assert re.fullmatch(re.escape(URL) + r"\?_=\d+", request.full_url)
    assert request.get_header("User-agent") == "Mozilla/5.0"


def test_cached_zip_is_reused_without_downloading(paths, monkeypatch):
    zip_path, extract_dir = paths
    zip_path.parent.mkdir(parents=True)
    zip_path.write_bytes(_zip_bytes(content=b"cached"))
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    tiger_files.download_and_extract(URL, zip_path, extract_dir)
    assert (extract_dir / "tl_2024_us_county.shp").read_bytes() == b"cached"


def test_cached_non_zip_is_deleted_and_the_error_names_it(paths, monkeypatch):
    """An HTML error page served with a 200 lands under the cached name; the
    next run must not keep failing on it silently."""
    zip_path, extract_dir = paths
    zip_path.parent.mkdir(parents=True)
    zip_path.write_bytes(b"<html>Access denied</html>")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(RuntimeError, match=re.escape(str(zip_path))) as excinfo:
        tiger_files.download_and_extract(URL, zip_path, extract_dir)
    assert "re-run to re-download" in str(excinfo.value)
    assert not zip_path.exists()
