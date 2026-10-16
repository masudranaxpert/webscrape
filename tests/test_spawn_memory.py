"""Browser launches must survive a parent process holding one huge merged writable mapping.

Production incident (2026-10-16): the API process carried a ~6 GB rw-p mapping (RSS ~10 MB). With the kernel's
heuristic overcommit, fork() refuses a mapping larger than RAM+swap, and uvloop spawns subprocesses with fork(), so
every Playwright driver start failed with `OSError: [Errno 12] Cannot allocate memory` until the service restarted.
The default asyncio loop uses vfork and is immune.
"""

import asyncio
import mmap
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

CHUNK_MB = 256


def _limit_mb() -> int:
    mem = dict(re.findall(r"^(MemTotal|SwapTotal):\s+(\d+)", Path("/proc/meminfo").read_text(), re.M))
    return (int(mem["MemTotal"]) + int(mem["SwapTotal"])) // 1024


def _overcommit_mode() -> int:
    return int(Path("/proc/sys/vm/overcommit_memory").read_text())


@pytest.fixture()
def huge_mapping():
    """Many small private writable mappings that merge into one VMA bigger than RAM+swap (each mmap alone passes)."""
    if _overcommit_mode() != 0:
        pytest.skip("needs heuristic overcommit (vm.overcommit_memory=0)")
    limit = _limit_mb()
    if limit > 48 * 1024:
        pytest.skip(f"{limit} MB of RAM+swap: too large to emulate cheaply")
    maps, total = [], 0
    try:
        while total < limit + 300:
            maps.append(mmap.mmap(-1, CHUNK_MB << 20, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                                  prot=mmap.PROT_READ | mmap.PROT_WRITE))
            total += CHUNK_MB
    except (OSError, ValueError) as err:
        for m in maps:
            m.close()
        pytest.skip(f"could not build the mapping: {err}")
    yield total
    for m in maps:
        m.close()


async def _spawn_true() -> int:
    proc = await asyncio.create_subprocess_exec("/bin/true")
    return await proc.wait()


def test_default_asyncio_loop_spawns_despite_huge_mapping(huge_mapping):
    assert asyncio.run(_spawn_true()) == 0


def test_uvloop_fork_is_what_fails(huge_mapping):
    uvloop = pytest.importorskip("uvloop")
    with pytest.raises(OSError) as err:
        uvloop.run(_spawn_true())
    assert err.value.errno == 12, "documents why the entrypoint must not use uvloop"


def test_entrypoint_runs_uvicorn_on_the_default_loop():
    script = (Path(__file__).resolve().parent.parent / "docker-entrypoint.sh").read_text()
    assert re.search(r"uvicorn main:app\b.*--loop asyncio", script), "docker-entrypoint.sh must pass --loop asyncio"
