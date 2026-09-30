"""Hostile archives against the extractor and the walk (22-04).

EVERY ARCHIVE IS ASSERTED TO CONTAIN ITS HOSTILE MEMBER before it is fed to
the extractor, by listing it with `tarfile`. An archive that silently lost
its symlink when it was built tests nothing (Phase 21's lesson: ask what
the fixture cannot distinguish). And after every run, NOTHING may exist
outside `dest/`: the test lists the whole temporary directory.

The guards under test, and the mutation each case kills (recorded in
22-04-SUMMARY.md):

- the header check (type, path, top-level directory, size) and
  `filter="data"` -- each holds alone, both removed lets traversal through,
  and `test_the_data_filter_alone_refuses_traversal` makes the filter's
  contribution observable on its own;
- the two expansion counters -- the stream counter (a header bomb) and the
  declared-size budget (a sparse bomb, PR #52's review H1);
- the name filters before writing -- a secret never lands on disk;
- a write failing for any reason but the name fails the extraction loudly
  and leaves no partial file (PR #52's review H2);
- `os.walk(followlinks=False)` and `lstat` -- the walk never follows a
  link that somehow exists on disk.

Symlink-on-disk cases need `os.symlink`, which Windows grants only with a
privilege this machine does not have; they skip there and run in CI. The
real-full-disk case needs a tiny filesystem, named by `RAG_DOC_TINY_FS`
(a `--tmpfs /small:size=300k` in the container runs); a monkeypatched
twin of it runs everywhere.
"""

from __future__ import annotations

import errno
import gzip
import io
import os
import tarfile
import uuid
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pytest

from workers.fetch import (
    DEFAULT_LIMITS,
    FetchFailed,
    FetchRejected,
    Limits,
    collect_tree,
    extract_archive,
    job_directory,
    sweep_stale_workdirs,
)
from workers.fetch import archive as archive_module

MB = 1024 * 1024
TOP = "acme-widgets-0123456789ab"
SECRET = "AKIA-SENTINEL-MUST-NOT-BE-INDEXED"


def _symlinks_available() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "t")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("x")
        try:
            os.symlink(target, os.path.join(d, "l"))
        except (OSError, NotImplementedError):
            return False
    return True


SYMLINKS = _symlinks_available()
needs_symlinks = pytest.mark.skipif(not SYMLINKS, reason="os.symlink needs a privilege this host lacks")


# ---------------------------------------------------------------------
# Building archives
# ---------------------------------------------------------------------


class Member:
    """One tar member to write. `kind` is tarfile's type byte."""

    def __init__(
        self,
        name: str,
        data: bytes = b"",
        kind: bytes = tarfile.REGTYPE,
        linkname: str = "",
        size: Optional[int] = None,
    ) -> None:
        self.name = name
        self.data = data
        self.kind = kind
        self.linkname = linkname
        self.size = size


def build(members: Iterable[Member]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for m in members:
            info = tarfile.TarInfo(m.name)
            info.type = m.kind
            info.linkname = m.linkname
            if m.kind == tarfile.REGTYPE:
                info.size = len(m.data) if m.size is None else m.size
                tar.addfile(info, io.BytesIO(m.data))
            else:
                tar.addfile(info)
    return buf.getvalue()


def regular(rel: str, data: bytes = b"print('ok')\n") -> Member:
    return Member(f"{TOP}/{rel}", data)


# --- raw tar bytes, for shapes tarfile will read but not write ---


def raw_regular(name: str, data: bytes) -> bytes:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info.tobuf(format=tarfile.GNU_FORMAT) + data + b"\0" * ((512 - len(data) % 512) % 512)


def raw_sparse(name: str, stored: bytes, apparent_size: int) -> bytes:
    """An old-GNU sparse member (typeflag 'S'): `stored` at offset 0, then a
    hole up to `apparent_size`. tarfile reads it as a regular file of
    `apparent_size` bytes with `.sparse` set, and extracts it by seek and
    truncate -- `apparent_size` on disk from `len(stored)` in the stream.
    """
    buf = bytearray(512)

    def put(off: int, val: bytes) -> None:
        buf[off:off + len(val)] = val

    put(0, name.encode())
    put(100, b"0000644\0")
    put(108, b"0000000\0")
    put(116, b"0000000\0")
    put(124, f"{len(stored):011o}\0".encode())
    put(136, f"{0:011o}\0".encode())
    buf[156] = ord("S")
    put(257, b"ustar  \0")
    put(265, b"root\0")
    put(297, b"root\0")
    put(386, f"{0:011o}\0".encode())
    put(398, f"{len(stored):011o}\0".encode())
    buf[482] = 0
    put(483, f"{apparent_size:011o}\0".encode())
    chksum = 256 + sum(buf[:148]) + sum(buf[156:])
    put(148, f"{chksum:06o}\0 ".encode())
    return bytes(buf) + stored + b"\0" * ((512 - len(stored) % 512) % 512)


def raw_gz(*members: bytes, end_of_archive: bool = True) -> bytes:
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb") as g:
        g.write(b"".join(members) + (b"\0" * 1024 if end_of_archive else b""))
    return out.getvalue()


def listing(archive: str) -> Dict[str, tarfile.TarInfo]:
    with tarfile.open(archive, mode="r:gz") as tar:
        return {m.name: m for m in tar.getmembers()}


def write(tmp_path, data: bytes) -> str:
    path = os.path.join(str(tmp_path), "archive.tar.gz")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def run(tmp_path, archive: str, limits: Limits = DEFAULT_LIMITS):
    dest = os.path.join(str(tmp_path), "dest")
    stats = extract_archive(archive, dest, limits)
    files, walk_skipped = collect_tree(dest, limits)
    return stats, files, walk_skipped, dest


def everything_under(root: str) -> List[str]:
    """Every file, link or special entry under root, relative, POSIX style."""
    found: List[str] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        for d in list(dirs):
            if os.path.islink(os.path.join(current, d)):
                found.append(os.path.relpath(os.path.join(current, d), root).replace(os.sep, "/") + "@")
                dirs.remove(d)
        for n in names:
            found.append(os.path.relpath(os.path.join(current, n), root).replace(os.sep, "/"))
    return sorted(found)


def assert_nothing_outside_dest(tmp_path, extra_allowed: Sequence[str] = ()) -> None:
    """Only the archive and dest/ may exist under tmp_path."""
    for entry in everything_under(str(tmp_path)):
        assert entry == "archive.tar.gz" or entry.startswith("dest/") or entry in extra_allowed, (
            f"{entry} landed outside dest/"
        )
    parent = os.path.dirname(str(tmp_path))
    assert not os.path.lexists(os.path.join(parent, "escape.py"))
    assert not os.path.lexists(os.path.join(str(tmp_path), "escape.py"))


# ---------------------------------------------------------------------
# A benign archive, first: the fixture can distinguish good from bad
# ---------------------------------------------------------------------


def test_a_benign_archive_is_extracted_under_its_stripped_top_level(tmp_path) -> None:
    archive = write(
        tmp_path,
        build(
            [
                Member(TOP, kind=tarfile.DIRTYPE),
                Member(f"{TOP}/src", kind=tarfile.DIRTYPE),
                regular("src/a.py", b"print(1)\n"),
                regular("README.md", b"# hi\n"),
                regular("pkg/b.go", b"package b\n"),
            ]
        ),
    )
    stats, files, walk_skipped, dest = run(tmp_path, archive)
    assert stats.top_level == TOP
    assert stats.files_written == 3
    assert stats.members_seen == 5
    assert [(f.path, f.language) for f in files] == [
        ("README.md", "markdown"),
        ("pkg/b.go", "go"),
        ("src/a.py", "python"),
    ]
    assert files[2].content == "print(1)\n"
    assert walk_skipped == {}
    assert everything_under(dest) == ["README.md", "pkg/b.go", "src/a.py"]
    assert_nothing_outside_dest(tmp_path)


# ---------------------------------------------------------------------
# Traversal and absolute paths
# ---------------------------------------------------------------------


def test_dot_dot_traversal_is_refused(tmp_path) -> None:
    archive = write(tmp_path, build([regular("src/a.py"), Member(f"{TOP}/../escape.py", b"escaped\n")]))
    members = listing(archive)
    assert f"{TOP}/../escape.py" in members, "premise: the hostile member is in the archive"
    assert members[f"{TOP}/../escape.py"].isreg()

    stats, files, _, dest = run(tmp_path, archive)
    assert stats.skipped.get("unsafe_path") == 1
    assert [f.path for f in files] == ["src/a.py"]
    assert everything_under(dest) == ["src/a.py"]
    assert_nothing_outside_dest(tmp_path)


def test_dot_dot_traversal_without_a_top_level_directory_is_refused(tmp_path) -> None:
    archive = write(tmp_path, build([regular("src/a.py"), Member("../escape.py", b"escaped\n")]))
    assert "../escape.py" in listing(archive)

    stats, files, _, dest = run(tmp_path, archive)
    assert stats.skipped.get("unsafe_path") == 1
    assert everything_under(dest) == ["src/a.py"]
    assert_nothing_outside_dest(tmp_path)


def test_an_absolute_path_is_refused(tmp_path) -> None:
    archive = write(tmp_path, build([regular("src/a.py"), Member("/etc/abs.py", b"abs\n")]))
    assert "/etc/abs.py" in listing(archive), "premise"

    stats, files, _, dest = run(tmp_path, archive)
    assert stats.skipped.get("unsafe_path") == 1
    assert everything_under(dest) == ["src/a.py"]
    assert not os.path.exists(os.path.join(dest, "etc", "abs.py"))
    assert_nothing_outside_dest(tmp_path)


@pytest.mark.parametrize(
    "name",
    [
        f"{TOP}\\..\\escape.py",  # a backslash separator
        "C:/escape.py",  # a drive letter
        f"{TOP}/./escape.py",  # a dot component
        f"{TOP}//escape.py",  # an empty component
        f"{TOP}/sub/../../escape.py",  # traversal from a subdirectory
    ],
)
def test_other_unsafe_spellings_are_refused(tmp_path, name: str) -> None:
    archive = write(tmp_path, build([regular("src/a.py"), Member(name, b"x\n")]))
    assert name in listing(archive), "premise"

    stats, files, _, dest = run(tmp_path, archive)
    assert stats.skipped.get("unsafe_path") == 1, stats.skipped
    assert everything_under(dest) == ["src/a.py"]
    assert_nothing_outside_dest(tmp_path)


def test_a_second_top_level_directory_is_refused(tmp_path) -> None:
    # GitHub's archives have exactly one. A member under another top-level
    # name is not from the archive we asked for.
    archive = write(tmp_path, build([regular("src/a.py"), Member("other-top/src/b.py", b"x\n")]))
    stats, files, _, dest = run(tmp_path, archive)
    assert stats.skipped.get("unexpected_top_level") == 1
    assert everything_under(dest) == ["src/a.py"]


def test_the_top_level_directory_is_checked_against_the_expected_name(tmp_path) -> None:
    # `{owner}-{repo}-{sha7}` is what GitHub builds; an archive under any
    # other name is not the one asked for, and it is refused on its FIRST
    # member, before anything is written.
    archive = write(tmp_path, build([Member(TOP, kind=tarfile.DIRTYPE), regular("src/a.py")]))
    dest = os.path.join(str(tmp_path), "dest")
    stats = extract_archive(archive, dest, DEFAULT_LIMITS, expected_top_level=TOP)
    assert stats.files_written == 1, "the expected name is accepted"

    dest2 = os.path.join(str(tmp_path), "dest2")
    with pytest.raises(FetchFailed) as raised:
        extract_archive(archive, dest2, DEFAULT_LIMITS, expected_top_level="acme-widgets-0000000")
    assert "top-level directory" in str(raised.value)
    assert everything_under(dest2) == []


PRIVATE_SHA = "f798806452c0743312780e0cc3e97301286696bd"


def test_both_measured_directory_names_are_accepted_and_nothing_else(tmp_path) -> None:
    # 22-05's live proof: GitHub archives a PRIVATE repository under
    # `{owner}-{repo}-{sha}`, the FULL SHA (measured on
    # AlecAsdourian/ES-SC-API-Navigator, by SHA and by branch), and a PUBLIC
    # one under `{sha7}` (mealie and octocat/Hello-World, with and without
    # authentication). 22-04 knew only the public form and refused every
    # private repository's archive. Both forms name the commit asked for;
    # every other name -- an unobserved abbreviation length included -- is
    # still refused before anything is written.
    short, full = archive_module.expected_top_levels_for("acme/widgets", PRIVATE_SHA)
    assert short == f"acme-widgets-{PRIVATE_SHA[:7]}"
    assert full == f"acme-widgets-{PRIVATE_SHA}"

    for label, top in (("public", short), ("private", full)):
        archive = write(tmp_path, build([Member(top, kind=tarfile.DIRTYPE), Member(f"{top}/src/a.py", b"#\n")]))
        dest = os.path.join(str(tmp_path), f"dest-{label}")
        stats = extract_archive(archive, dest, DEFAULT_LIMITS, expected_top_level=(short, full))
        assert stats.top_level == top
        assert everything_under(dest) == ["src/a.py"], f"the {label} form must be accepted"

    for label, top in (
        ("a twelve-character abbreviation, never observed", f"acme-widgets-{PRIVATE_SHA[:12]}"),
        ("another commit's full SHA", "acme-widgets-" + "0" * 40),
        ("another repository", f"acme-gadgets-{PRIVATE_SHA}"),
    ):
        archive = write(tmp_path, build([Member(f"{top}/src/a.py", b"#\n")]))
        dest = os.path.join(str(tmp_path), "dest-refused")
        with pytest.raises(FetchFailed) as raised:
            extract_archive(archive, dest, DEFAULT_LIMITS, expected_top_level=(short, full))
        assert everything_under(dest) == [], label
        # The message says what the archive HELD, which is what made the
        # live failure diagnosable only by downloading the archive again.
        assert repr(top) in str(raised.value), label
        assert repr(full) in str(raised.value) and repr(short) in str(raised.value)


def test_a_top_level_name_that_is_not_plain_is_described_not_printed(tmp_path) -> None:
    # The directory name is archive-controlled text: it reaches `last_error`
    # only when it is the kind of name GitHub builds.
    hostile = "acme-widgets-‮evil\x1b[31m"
    archive = write(tmp_path, build([Member(f"{hostile}/src/a.py", b"#\n")]))
    with pytest.raises(FetchFailed) as raised:
        extract_archive(
            archive, os.path.join(str(tmp_path), "dest"), DEFAULT_LIMITS,
            expected_top_level=archive_module.expected_top_levels_for("acme/widgets", PRIVATE_SHA),
        )
    message = str(raised.value)
    assert "evil" not in message and "\x1b" not in message
    assert f"(a {len(hostile)}-character name, not printed)" in message


def test_the_data_filter_alone_refuses_traversal(tmp_path, monkeypatch) -> None:
    # The header check is strictly broader than `filter="data"`, so with
    # both in place the filter never fires and its contribution is
    # invisible (PR #52's review, L2). Neutering the header check here
    # makes it visible: the traversal member reaches `tar.extract`, and the
    # filter refuses it.
    monkeypatch.setattr(archive_module, "_unsafe", lambda name: False)
    archive = write(tmp_path, build([regular("src/a.py"), Member(f"{TOP}/../escape.py", b"escaped\n")]))
    assert f"{TOP}/../escape.py" in listing(archive), "premise"

    stats, files, _, dest = run(tmp_path, archive)
    assert stats.skipped.get("refused_by_filter") == 1, stats.skipped
    assert "unsafe_path" not in stats.skipped, "premise: the header check was out of the way"
    assert everything_under(dest) == ["src/a.py"]
    assert_nothing_outside_dest(tmp_path)


# ---------------------------------------------------------------------
# Links and special files
# ---------------------------------------------------------------------


def test_a_symlink_to_an_absolute_path_is_skipped(tmp_path) -> None:
    archive = write(
        tmp_path,
        build(
            [
                regular("src/a.py"),
                Member(f"{TOP}/link.py", kind=tarfile.SYMTYPE, linkname="/etc/passwd"),
            ]
        ),
    )
    member = listing(archive)[f"{TOP}/link.py"]
    assert member.issym() and member.linkname == "/etc/passwd", "premise"

    stats, files, _, dest = run(tmp_path, archive)
    assert stats.skipped.get("symlink") == 1
    assert [f.path for f in files] == ["src/a.py"]
    assert everything_under(dest) == ["src/a.py"]
    assert not os.path.lexists(os.path.join(dest, "link.py"))


def test_a_symlink_to_dot_dot_is_skipped(tmp_path) -> None:
    archive = write(
        tmp_path,
        build([regular("src/a.py"), Member(f"{TOP}/up", kind=tarfile.SYMTYPE, linkname="..")]),
    )
    member = listing(archive)[f"{TOP}/up"]
    assert member.issym() and member.linkname == "..", "premise"

    stats, _, _, dest = run(tmp_path, archive)
    assert stats.skipped.get("symlink") == 1
    assert everything_under(dest) == ["src/a.py"]


def test_a_symlink_inside_the_tree_is_skipped_too(tmp_path) -> None:
    # The `data` filter ALLOWS a link that stays inside the destination;
    # the type check is what keeps it out. Sibling links are legitimate in
    # repositories, and they are still never followed.
    archive = write(
        tmp_path,
        build([regular("src/a.py"), Member(f"{TOP}/alias.py", kind=tarfile.SYMTYPE, linkname="src/a.py")]),
    )
    assert listing(archive)[f"{TOP}/alias.py"].issym(), "premise"

    stats, files, _, dest = run(tmp_path, archive)
    assert stats.skipped.get("symlink") == 1
    assert [f.path for f in files] == ["src/a.py"]
    assert everything_under(dest) == ["src/a.py"]


def test_a_hardlink_to_a_path_outside_is_skipped(tmp_path) -> None:
    archive = write(
        tmp_path,
        build(
            [
                regular("src/a.py"),
                Member(f"{TOP}/hard.py", kind=tarfile.LNKTYPE, linkname="../../outside.py"),
            ]
        ),
    )
    member = listing(archive)[f"{TOP}/hard.py"]
    assert member.islnk() and member.linkname == "../../outside.py", "premise"

    stats, files, _, dest = run(tmp_path, archive)
    assert stats.skipped.get("hardlink") == 1
    assert everything_under(dest) == ["src/a.py"]
    assert_nothing_outside_dest(tmp_path)


@pytest.mark.parametrize(
    "kind, label",
    [(tarfile.FIFOTYPE, "fifo"), (tarfile.CHRTYPE, "chr"), (tarfile.BLKTYPE, "blk")],
)
def test_special_entries_are_skipped(tmp_path, kind: bytes, label: str) -> None:
    archive = write(tmp_path, build([regular("src/a.py"), Member(f"{TOP}/dev-{label}", kind=kind)]))
    member = listing(archive)[f"{TOP}/dev-{label}"]
    assert member.type == kind and not member.isreg(), "premise"

    stats, _, _, dest = run(tmp_path, archive)
    assert stats.skipped.get("special") == 1
    assert everything_under(dest) == ["src/a.py"]


# ---------------------------------------------------------------------
# Sizes and bombs
# ---------------------------------------------------------------------


def test_a_two_megabyte_file_is_skipped_and_counted(tmp_path) -> None:
    big = b"# " + b"x" * (2 * MB)
    archive = write(tmp_path, build([regular("src/a.py"), regular("src/big.py", big)]))
    assert listing(archive)[f"{TOP}/src/big.py"].size == len(big), "premise"

    stats, files, _, dest = run(tmp_path, archive)
    assert stats.skipped.get("oversize_file") == 1
    assert [f.path for f in files] == ["src/a.py"]
    assert everything_under(dest) == ["src/a.py"], "the oversize file must not be written at all"


def test_a_file_exactly_at_the_cap_is_kept(tmp_path) -> None:
    exact = b"x" * MB
    archive = write(tmp_path, build([regular("src/exact.py", exact)]))
    stats, files, _, _ = run(tmp_path, archive)
    assert stats.files_written == 1
    assert len(files[0].content) == MB


def test_a_decompression_bomb_is_rejected_within_one_member_of_the_cap(tmp_path) -> None:
    # Ten members of 400 KB of zeros: a few KB compressed, 4 MB expanded.
    # With the expansion cap at 1 MB the extractor must stop while reading
    # the third member, not after reading all ten.
    member_size = 400 * 1024
    archive = write(
        tmp_path,
        build([regular(f"src/z{i}.py", b"\x00" * member_size) for i in range(10)]),
    )
    assert os.path.getsize(archive) < 64 * 1024, "premise: the archive is small on disk"
    assert len(listing(archive)) == 10, "premise: ten members"

    limits = Limits(max_expanded_bytes=1 * MB)
    dest = os.path.join(str(tmp_path), "dest")
    with pytest.raises(FetchRejected) as raised:
        extract_archive(archive, dest, limits)
    assert "expands past" in raised.value.reason
    assert 0 < raised.value.members_seen <= 4, (
        f"stopped after {raised.value.members_seen} members; must be within one member of "
        f"the cap ({MB // member_size} members), not after reading all ten"
    )
    written = everything_under(dest)
    assert len(written) <= 3, written


def test_the_expansion_cap_counts_the_whole_tar_stream_not_only_payloads(tmp_path) -> None:
    # A header bomb: many zero-byte members. Their payload is 0 bytes but
    # the tar stream is 512 bytes of header each, and that is what fills a
    # disk with inodes and a CPU with parsing. 4,000 empty files = 2 MB of
    # headers against a 1 MB cap.
    archive = write(tmp_path, build([Member(f"{TOP}/assets/{i}.bin", b"") for i in range(4000)]))
    limits = Limits(max_expanded_bytes=1 * MB)
    with pytest.raises(FetchRejected) as raised:
        extract_archive(archive, os.path.join(str(tmp_path), "dest"), limits)
    assert raised.value.members_seen < 4000


def test_a_sparse_member_is_skipped_and_never_lands(tmp_path) -> None:
    # PR #52's review, H1. tarfile materialises a sparse member by seek and
    # truncate to its declared size: measured before the fix, a 366-byte
    # archive of ten sparse members put 10,000,000 apparent bytes on disk
    # while the stream counter saw 98,304, and the walk returned all ten as
    # text. A real prefix longer than any NUL probe is the shape.
    stored = b"# real prefix\n" + b"x" * 9000
    archive = write(tmp_path, raw_gz(*(raw_sparse(f"{TOP}/src/s{i}.py", stored, 1_000_000) for i in range(10))))
    assert os.path.getsize(archive) < 2048, "premise: a few hundred bytes on disk"
    members = listing(archive)
    assert len(members) == 10, "premise"
    for member in members.values():
        assert member.sparse is not None and member.isreg() and member.size == 1_000_000, "premise: sparse"

    stats, files, walk_skipped, dest = run(tmp_path, archive)
    assert stats.skipped == {"sparse": 10}
    assert stats.files_written == 0
    assert stats.declared_bytes == 10_000_000, "the declared size was charged even though nothing landed"
    assert everything_under(dest) == []
    assert files == []


def test_a_sparse_bomb_is_rejected_on_its_declared_size(tmp_path) -> None:
    # The same archive under a 1 MB expansion cap: the SECOND member's
    # declared size takes the budget past the cap, whatever the stream
    # counter says, so the extractor stops there.
    stored = b"# real prefix\n" + b"x" * 9000
    archive = write(tmp_path, raw_gz(*(raw_sparse(f"{TOP}/src/s{i}.py", stored, 1_000_000) for i in range(10))))
    dest = os.path.join(str(tmp_path), "dest")
    with pytest.raises(FetchRejected) as raised:
        extract_archive(archive, dest, Limits(max_expanded_bytes=1 * MB))
    assert "expands past" in raised.value.reason
    assert raised.value.members_seen == 2, "stopped at the member that crossed the cap"
    assert everything_under(dest) == []


def test_a_full_disk_raises_and_leaves_no_partial_file(tmp_path, monkeypatch) -> None:
    # PR #52's review, H2. Measured before the fix on a 300 KB tmpfs: the
    # extractor returned normally with 5 written and 15 "unwritable", 20
    # files were on disk of which 15 were partial, and the walk returned
    # all 20 as complete files. Here ENOSPC is raised by a wrapped file
    # object once 300,000 bytes have been written, which leaves the same
    # half-written file behind that the real disk did.
    real_open = archive_module.tarfile.bltn_open
    written = {"bytes": 0}

    def quota_open(path, mode="r", *args, **kwargs):
        fh = real_open(path, mode, *args, **kwargs)
        if "w" not in mode:
            return fh

        class Quota:
            def write(self, data):
                if written["bytes"] + len(data) > 300_000:
                    raise OSError(errno.ENOSPC, "No space left on device")
                written["bytes"] += len(data)
                return fh.write(data)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                fh.close()

            def __getattr__(self, name):
                return getattr(fh, name)

        return Quota()

    monkeypatch.setattr(archive_module.tarfile, "bltn_open", quota_open)
    archive = write(tmp_path, build([regular(f"src/f{i}.py", b"#" + b"a" * 51204) for i in range(20)]))
    dest = os.path.join(str(tmp_path), "dest")
    with pytest.raises(FetchFailed) as raised:
        extract_archive(archive, dest, DEFAULT_LIMITS)
    assert "No space left" in str(raised.value) and str(errno.ENOSPC) in str(raised.value)
    sizes = {p: os.path.getsize(os.path.join(dest, p)) for p in everything_under(dest)}
    assert sizes, "premise: some files were written before the disk filled"
    assert all(size == 51205 for size in sizes.values()), f"a partial file remained: {sizes}"


def test_a_full_disk_raises_and_leaves_no_partial_file_on_a_real_small_filesystem(tmp_path) -> None:
    tiny = os.environ.get("RAG_DOC_TINY_FS")
    if not tiny or not os.path.isdir(tiny):
        pytest.skip("RAG_DOC_TINY_FS names no tiny filesystem; run in the container with --tmpfs")
    archive = write(tmp_path, build([regular(f"src/f{i}.py", b"#" + b"a" * 51204) for i in range(20)]))
    dest = os.path.join(tiny, f"dest-{uuid.uuid4().hex}")
    try:
        with pytest.raises(FetchFailed) as raised:
            extract_archive(archive, dest, DEFAULT_LIMITS)
        assert str(errno.ENOSPC) in str(raised.value)
        sizes = {p: os.path.getsize(os.path.join(dest, p)) for p in everything_under(dest)}
        assert all(size == 51205 for size in sizes.values()), f"a partial file remained: {sizes}"
    finally:
        archive_module._rmtree_quiet(dest)


def test_a_name_shaped_write_error_is_skipped_not_fatal(tmp_path, monkeypatch) -> None:
    # ENAMETOOLONG is about the member, not the disk: skipped and counted,
    # and the extraction goes on.
    real_open = archive_module.tarfile.bltn_open

    def picky_open(path, mode="r", *args, **kwargs):
        if "w" in mode and path.endswith("long.py"):
            raise OSError(errno.ENAMETOOLONG, "File name too long")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(archive_module.tarfile, "bltn_open", picky_open)
    archive = write(tmp_path, build([regular("src/long.py"), regular("src/a.py")]))
    stats, files, _, dest = run(tmp_path, archive)
    assert stats.skipped == {"unwritable": 1}
    assert [f.path for f in files] == ["src/a.py"]


@pytest.mark.parametrize(
    "label, payload",
    [
        ("not gzip at all", b"this is a text file, not a gzip stream\n" * 4),
        ("a truncated gzip", None),
        ("a gzip of garbage", gzip.compress(os.urandom(4096))),
        ("an empty gzip", gzip.compress(b"")),
    ],
)
def test_an_unreadable_archive_is_a_plain_failure(tmp_path, label: str, payload: Optional[bytes]) -> None:
    # PR #52's review, L3: BadGzipFile, EOFError and ReadError all become
    # FetchFailed, so 22-05 retries rather than crashing the worker loop.
    if payload is None:
        whole = build([regular("src/a.py", b"#" * 4000)])
        payload = whole[: len(whole) // 2]
    archive = write(tmp_path, payload)
    dest = os.path.join(str(tmp_path), "dest")
    with pytest.raises(FetchFailed) as raised:
        extract_archive(archive, dest, DEFAULT_LIMITS)
    assert "not a readable gzip tar" in str(raised.value), label


def test_a_member_truncated_mid_data_is_a_plain_failure(tmp_path) -> None:
    # The header promises 4,000 bytes; the stream ends after 100.
    info = tarfile.TarInfo(f"{TOP}/src/cut.py")
    info.size = 4000
    archive = write(tmp_path, raw_gz(info.tobuf(format=tarfile.GNU_FORMAT) + b"#" * 100, end_of_archive=False))
    dest = os.path.join(str(tmp_path), "dest")
    with pytest.raises(FetchFailed) as raised:
        extract_archive(archive, dest, DEFAULT_LIMITS)
    assert "not a readable gzip tar" in str(raised.value)
    assert everything_under(dest) == [], "the half-written member was unlinked"


def test_more_than_twenty_thousand_indexable_files_is_rejected_at_the_cap(tmp_path) -> None:
    # 20,001 indexable files, then a hundred more the extractor must never
    # reach: `members_seen` proves it stopped at the cap, not at the end.
    cap = DEFAULT_LIMITS.max_indexable_files
    archive = write(
        tmp_path,
        build([Member(f"{TOP}/src/f{i}.py", b"#\n") for i in range(cap + 1 + 100)]),
    )
    with pytest.raises(FetchRejected) as raised:
        extract_archive(archive, os.path.join(str(tmp_path), "dest"), DEFAULT_LIMITS)
    assert raised.value.reason == f"more than {cap} indexable files"
    assert raised.value.members_seen == cap + 1


def test_exactly_the_file_cap_is_allowed(tmp_path) -> None:
    # The cap is a parameter; this runs it at 50. The 20,001 case above
    # runs the real number.
    limits = Limits(max_indexable_files=50)
    archive = write(tmp_path, build([Member(f"{TOP}/src/f{i}.py", b"#\n") for i in range(50)]))
    stats, files, _, _ = run(tmp_path, archive, limits)
    assert stats.files_written == 50
    assert len(files) == 50


def test_the_walk_applies_the_file_cap_too(tmp_path) -> None:
    # Files planted on disk beside what the extractor wrote are judged by
    # the walk, which caps independently.
    dest = os.path.join(str(tmp_path), "dest")
    os.makedirs(os.path.join(dest, "src"))
    for i in range(6):
        with open(os.path.join(dest, "src", f"p{i}.py"), "w", encoding="utf-8") as fh:
            fh.write("#\n")
    with pytest.raises(FetchRejected):
        collect_tree(dest, Limits(max_indexable_files=5))


# ---------------------------------------------------------------------
# Secrets, vendored, binary, encodings
# ---------------------------------------------------------------------


def test_secret_looking_files_beside_real_code_are_never_returned_or_written(tmp_path) -> None:
    archive = write(
        tmp_path,
        build(
            [
                regular("src/a.py"),
                regular(".env", f"AWS_SECRET={SECRET}\n".encode()),
                regular("deploy/id_rsa", f"-----BEGIN OPENSSH PRIVATE KEY-----\n{SECRET}\n".encode()),
                regular("certs/server.pem", f"-----BEGIN PRIVATE KEY-----\n{SECRET}\n".encode()),
            ]
        ),
    )
    members = listing(archive)
    for name in (f"{TOP}/.env", f"{TOP}/deploy/id_rsa", f"{TOP}/certs/server.pem"):
        assert name in members and members[name].isreg(), f"premise: {name}"

    stats, files, walk_skipped, dest = run(tmp_path, archive)
    assert stats.skipped.get("secret") == 3
    assert [f.path for f in files] == ["src/a.py"]
    assert all(SECRET not in f.content for f in files)
    assert everything_under(dest) == ["src/a.py"], "a secret-looking file must never touch the disk"
    # And the counts carry no paths.
    assert all(isinstance(v, int) for v in stats.skipped.values())
    assert "id_rsa" not in repr(stats.skipped) and ".env" not in repr(stats.skipped)


def test_the_review_named_secret_files_are_never_written(tmp_path) -> None:
    # PR #52's review, L6: the entries it asked to see on the list, each
    # asserted at the extraction level rather than only by name.
    names = [".env.production", ".npmrc", "certs/client.p12", "android/release.jks", ".git-credentials", "home/.aws/credentials"]
    archive = write(tmp_path, build([regular("src/a.py")] + [regular(n, f"{SECRET}\n".encode()) for n in names]))
    members = listing(archive)
    for n in names:
        assert f"{TOP}/{n}" in members, f"premise: {n}"

    stats, files, _, dest = run(tmp_path, archive)
    assert stats.skipped == {"secret": len(names)}
    assert [f.path for f in files] == ["src/a.py"]
    assert everything_under(dest) == ["src/a.py"]


def test_a_secret_planted_on_disk_is_still_refused_by_the_walk(tmp_path) -> None:
    dest = os.path.join(str(tmp_path), "dest")
    os.makedirs(dest)
    with open(os.path.join(dest, ".env"), "w", encoding="utf-8") as fh:
        fh.write(SECRET)
    with open(os.path.join(dest, "a.py"), "w", encoding="utf-8") as fh:
        fh.write("#\n")
    files, skipped = collect_tree(dest)
    assert [f.path for f in files] == ["a.py"]
    assert skipped == {"secret": 1}


def test_vendored_generated_and_lockfiles_are_skipped_and_counted(tmp_path) -> None:
    archive = write(
        tmp_path,
        build(
            [
                regular("src/a.py"),
                regular("vendor/lib/x.go", b"package x\n"),
                regular("node_modules/left-pad/index.js", b"x\n"),
                regular("web/app.min.js", b"x\n"),
                regular("api/schema_pb2.py", b"x\n"),
                regular("package-lock.json", b"{}\n"),
                regular("go.sum", b"\n"),
                regular("assets/logo.png", b"\x89PNG"),
            ]
        ),
    )
    stats, files, _, dest = run(tmp_path, archive)
    assert [f.path for f in files] == ["src/a.py"]
    assert stats.skipped == {"vendored": 2, "generated": 2, "lockfile": 2, "unsupported": 1}
    assert everything_under(dest) == ["src/a.py"]


def test_a_generated_go_file_is_skipped_by_its_header(tmp_path) -> None:
    generated = b"// Code generated by protoc-gen-go. DO NOT EDIT.\npackage pb\n"
    archive = write(tmp_path, build([regular("pkg/gen.go", generated), regular("pkg/real.go", b"package pkg\n")]))
    _, files, walk_skipped, _ = run(tmp_path, archive)
    assert [f.path for f in files] == ["pkg/real.go"]
    assert walk_skipped == {"generated": 1}


def test_a_binary_py_file_is_skipped(tmp_path) -> None:
    binary = b"#!/usr/bin/python\n" + b"\x00\x01\x02\xff" * 100
    archive = write(tmp_path, build([regular("src/a.py"), regular("src/blob.py", binary)]))
    assert listing(archive)[f"{TOP}/src/blob.py"].size == len(binary), "premise"

    _, files, walk_skipped, _ = run(tmp_path, archive)
    assert [f.path for f in files] == ["src/a.py"]
    assert walk_skipped == {"binary": 1}


def test_a_nul_anywhere_in_the_file_is_binary(tmp_path) -> None:
    # PR #52's review, H3. The first cut probed 8 KB and let a late NUL
    # through into `content`, which 22-05 writes with psycopg2 -- and a NUL
    # is the one character psycopg2 refuses. The whole file is in memory;
    # the whole file is checked.
    text_then_nul = b"# " + b"a" * 9000 + b"\x00" * 100
    archive = write(tmp_path, build([regular("src/a.py"), regular("src/late.py", text_then_nul)]))
    _, files, walk_skipped, _ = run(tmp_path, archive)
    assert [f.path for f in files] == ["src/a.py"]
    assert walk_skipped == {"binary": 1}
    assert all("\x00" not in f.content for f in files)


def test_non_utf8_content_is_skipped(tmp_path) -> None:
    latin1 = "# caf\xe9 na\xefve\n".encode("latin-1")
    archive = write(tmp_path, build([regular("src/a.py"), regular("src/latin.py", latin1)]))
    _, files, walk_skipped, _ = run(tmp_path, archive)
    assert [f.path for f in files] == ["src/a.py"]
    assert walk_skipped == {"non_utf8": 1}


def test_a_utf8_bom_is_stripped(tmp_path) -> None:
    archive = write(tmp_path, build([regular("src/bom.py", "\ufeffprint(1)\n".encode("utf-8"))]))
    _, files, _, _ = run(tmp_path, archive)
    assert files[0].content == "print(1)\n"


# ---------------------------------------------------------------------
# The walk never follows a link that exists on disk
# ---------------------------------------------------------------------


@needs_symlinks
def test_the_walk_skips_a_file_symlink_planted_on_disk(tmp_path) -> None:
    outside = os.path.join(str(tmp_path), "outside.py")
    with open(outside, "w", encoding="utf-8") as fh:
        fh.write(SECRET)
    dest = os.path.join(str(tmp_path), "dest")
    os.makedirs(dest)
    os.symlink(outside, os.path.join(dest, "link.py"))
    with open(os.path.join(dest, "a.py"), "w", encoding="utf-8") as fh:
        fh.write("#\n")
    assert os.path.islink(os.path.join(dest, "link.py")), "premise"

    files, skipped = collect_tree(dest)
    assert [f.path for f in files] == ["a.py"]
    assert skipped == {"link": 1}
    assert all(SECRET not in f.content for f in files)


@needs_symlinks
def test_the_walk_does_not_descend_a_directory_symlink(tmp_path) -> None:
    outside_dir = os.path.join(str(tmp_path), "outside")
    os.makedirs(outside_dir)
    with open(os.path.join(outside_dir, "secret.py"), "w", encoding="utf-8") as fh:
        fh.write(SECRET)
    dest = os.path.join(str(tmp_path), "dest")
    os.makedirs(dest)
    os.symlink(outside_dir, os.path.join(dest, "linked"), target_is_directory=True)
    with open(os.path.join(dest, "a.py"), "w", encoding="utf-8") as fh:
        fh.write("#\n")
    assert os.path.islink(os.path.join(dest, "linked")), "premise"

    files, _ = collect_tree(dest)
    assert [f.path for f in files] == ["a.py"]
    assert all(SECRET not in f.content for f in files)


# ---------------------------------------------------------------------
# The property alone, for the defence-in-depth matrix
# ---------------------------------------------------------------------
#
# The tests above assert the property AND the accounting (which skip
# reason was counted). These assert the PROPERTY ONLY -- nothing hostile
# lands, nothing hostile is returned -- so that the mutation table in
# 22-04-SUMMARY.md can say cleanly: with the header check alone removed, or
# `filter="data"` alone removed, these still pass; with both removed, the
# traversal one fails. The accounting tests fail earlier, on the reason.


class TestNothingHostileLands:
    def _run(self, tmp_path, members: List[Member]):
        archive = write(tmp_path, build([regular("src/a.py")] + members))
        assert len(listing(archive)) == 1 + len(members), "premise"
        _, files, _, dest = run(tmp_path, archive)
        assert [f.path for f in files] == ["src/a.py"]
        assert everything_under(dest) == ["src/a.py"]
        assert_nothing_outside_dest(tmp_path)

    def test_dot_dot_traversal(self, tmp_path) -> None:
        self._run(tmp_path, [Member(f"{TOP}/../escape.py", b"escaped\n")])

    def test_absolute_path(self, tmp_path) -> None:
        self._run(tmp_path, [Member("/etc/abs.py", b"abs\n")])

    def test_symlink_to_absolute_path(self, tmp_path) -> None:
        self._run(tmp_path, [Member(f"{TOP}/link.py", kind=tarfile.SYMTYPE, linkname="/etc/passwd")])

    def test_symlink_to_dot_dot(self, tmp_path) -> None:
        self._run(tmp_path, [Member(f"{TOP}/up.py", kind=tarfile.SYMTYPE, linkname="../../outside.py")])

    def test_hardlink_outside(self, tmp_path) -> None:
        self._run(tmp_path, [Member(f"{TOP}/hard.py", kind=tarfile.LNKTYPE, linkname="../../outside.py")])


# ---------------------------------------------------------------------
# The job directory
# ---------------------------------------------------------------------


def test_job_directory_requires_a_uuid(tmp_path) -> None:
    with pytest.raises(ValueError):
        job_directory(str(tmp_path), "../escape")
    with pytest.raises(ValueError):
        job_directory(str(tmp_path), "not-a-uuid")
    job = str(uuid.uuid4())
    path = job_directory(str(tmp_path), job.upper())
    assert os.path.isdir(path)
    assert os.path.basename(path).startswith(job + "-"), "canonical spelling, then the random suffix"
    assert os.path.dirname(path) == str(tmp_path)


def test_job_directory_is_unique_per_call(tmp_path) -> None:
    # PR #52's review, M1: two processes on one host holding the same job
    # (a reclaim racing a stale attempt) must not share a tree by path.
    job = str(uuid.uuid4())
    first = job_directory(str(tmp_path), job)
    with open(os.path.join(first, "stale"), "w", encoding="utf-8") as fh:
        fh.write("x")
    second = job_directory(str(tmp_path), job)
    assert first != second
    assert os.path.isdir(first) and os.path.isdir(second)
    assert os.listdir(second) == [], "the new attempt starts empty"
    assert os.listdir(first) == ["stale"], "and the stale attempt's tree is untouched"


def test_sweep_removes_only_the_stale_directory_of_a_job(tmp_path) -> None:
    job = str(uuid.uuid4())
    stale = job_directory(str(tmp_path), job)
    fresh = job_directory(str(tmp_path), job)
    from datetime import datetime, timedelta

    old = (datetime.now() - timedelta(days=2)).timestamp()
    os.utime(stale, (old, old))
    assert sweep_stale_workdirs(str(tmp_path), timedelta(hours=1)) == 1
    assert not os.path.exists(stale)
    assert os.path.isdir(fresh)


def test_gzip_stream_is_read_through_the_counter(tmp_path) -> None:
    # The counted expansion is the tar stream: for a tiny archive that is
    # tarfile's 10 KB record, more than the 1-byte payload and less than
    # the 1 MB cap. The declared budget is the payload alone.
    archive = write(tmp_path, build([regular("src/a.py", b"#")]))
    stats, _, _, _ = run(tmp_path, archive)
    assert stats.payload_bytes == 1
    assert stats.declared_bytes == 1
    assert 1 < stats.expanded_bytes <= 64 * 1024
    with gzip.open(archive) as g:
        assert g.read()  # the file is a real gzip stream
