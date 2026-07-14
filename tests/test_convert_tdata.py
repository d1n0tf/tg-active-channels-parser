from __future__ import annotations

from pathlib import Path

from ChannelsParser.convert_tdata import _looks_like_tdata, _slugify, discover_tdata_folders


def test_slugify() -> None:
    from ChannelsParser.accounts import SLUG_RE

    assert _slugify("acc 1") == "acc_1"
    assert SLUG_RE.match(_slugify("my-phone"))


def test_looks_like_tdata(tmp_path: Path) -> None:
    d = tmp_path / "tdata"
    d.mkdir()
    assert _looks_like_tdata(d) is False
    (d / "key_datas").write_bytes(b"x")
    assert _looks_like_tdata(d) is True


def test_discover_tdata_layouts(tmp_path: Path) -> None:
    base = tmp_path / "tdata_root"
    base.mkdir()

    # layout: acc1/ is tdata itself
    acc1 = base / "acc1"
    acc1.mkdir()
    (acc1 / "key_datas").write_bytes(b"x")

    # layout: acc2/tdata/
    acc2 = base / "acc2"
    (acc2 / "tdata").mkdir(parents=True)
    (acc2 / "tdata" / "key_datas").write_bytes(b"x")

    # noise
    (base / "readme.txt").write_text("nope")

    found = discover_tdata_folders(base)
    ids = {aid for aid, _ in found}
    assert "acc1" in ids
    assert "acc2" in ids
