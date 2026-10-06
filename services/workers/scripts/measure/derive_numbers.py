"""Apply 22.1-05's committed rules N1-N7 to the run records. Arithmetic only.

`22.1-05-operating-numbers.md`, "Rules", is the authority; this script
implements it so the arithmetic in the record can be re-derived from the
committed JSON records. It sets nothing: the numbers go into code, compose,
tests and docs by hand, and a test pins each one to its constant.

    python scripts/measure/derive_numbers.py <records dir>

Record names it reads: `real-*.json` (the four real repositories),
`cap-before-*.json` and `cap-after-*.json` (the at-cap runs on the code
before and after A-L3's fix), `confirm-*.json` (N4's confirmation runs) and
`disk-*.json` (the 500 MB disk run).
"""

from __future__ import annotations

import json
import math
import pathlib
import sys
from typing import Any, Dict, List

MIB = 1024 * 1024
CAP_CHUNKS = 100_000
CAP_BYTES_MB = 500  # U6: 500 MB, the fetcher's MB being 1024 * 1024 bytes


def load(directory: pathlib.Path, pattern: str) -> List[Dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob(pattern))]


def ceil_to(value: float, step: float) -> float:
    return math.ceil(value / step) * step


def embed_rate(record: Dict[str, Any]) -> Dict[str, float]:
    job = record["jobs"][0]
    seconds = job["stages"]["seconds"]["embed"]
    calls = record["openai"]["calls"]
    tokens = record["openai"]["tokens"]
    return {"embed_seconds": seconds, "tokens": tokens, "http_calls": calls,
            "tokens_per_second": tokens / seconds, "tokens_per_minute": tokens / seconds * 60,
            "calls_per_minute": calls / seconds * 60}


def beats_of(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    beats = [b for r in records for b in r["heartbeat"]["beats"]]
    return {
        "runs": len(records),
        "beats": len(beats),
        "beats_57014": sum(r["heartbeat"]["beats_57014"] for r in records),
        "log_heartbeat_failed_querycanceled": sum(r["heartbeat"]["log_heartbeat_failed_querycanceled"] for r in records),
        "log_assuming_it_is_lost": sum(r["heartbeat"]["log_assuming_it_is_lost"] for r in records),
        "longest_beat_seconds": max((b["seconds"] for b in beats), default=0.0),
    }


def main() -> int:
    d = pathlib.Path(sys.argv[1])
    real = load(d, "real-*.json")
    before = load(d, "cap-before-*.json")
    after = load(d, "cap-after-*.json")
    confirm = load(d, "confirm-*.json")
    disk = load(d, "disk-*.json")
    out: Dict[str, Any] = {}

    # ---- N1 -------------------------------------------------------------
    fetch_per_mb = []
    for r in real:
        j = r["jobs"][0]
        fetch_per_mb.append({"run": r["record"], "fetch_seconds": j["stages"]["seconds"]["fetch"],
                             "archive_mb": j["archive_bytes"] / MIB,
                             "seconds_per_mb": j["stages"]["seconds"]["fetch"] / (j["archive_bytes"] / MIB)})
    worst_fetch = max(fetch_per_mb, key=lambda x: x["seconds_per_mb"])
    f_cap = CAP_BYTES_MB * worst_fetch["seconds_per_mb"]

    at_cap = [r for r in before + after if r["repository"]["full_name"].endswith("-99000")]
    p_cap = max(r["jobs"][0]["stages"]["seconds"]["parse"] for r in at_cap)

    per_chunk = []
    rates = []
    for r in real:
        distinct = r["jobs"][0]["distinct_stored"]
        per_chunk.append({"run": r["record"], "tokens": r["openai"]["tokens"], "distinct_chunks": distinct,
                          "tokens_per_chunk": r["openai"]["tokens"] / distinct})
        rates.append({"run": r["record"], **embed_rate(r)})
    max_tpc = max(per_chunk, key=lambda x: x["tokens_per_chunk"])
    min_rate = min(rates, key=lambda x: x["tokens_per_second"])
    e_cap = CAP_CHUNKS * max_tpc["tokens_per_chunk"] / min_rate["tokens_per_second"]

    after_cap = [r for r in after if r["repository"]["full_name"].endswith("-99000")]
    s_cap = max(r["jobs"][0]["stages"]["seconds"]["store"] for r in after_cap)
    t_cap = f_cap + p_cap + e_cap + s_cap
    longest_real = max(r["jobs"][0]["stages"]["seconds"]["claim_to_completion"] for r in real)
    n1_raw = 3 * t_cap
    n1 = max(ceil_to(n1_raw, 900), ceil_to(3 * longest_real, 900))
    out["N1"] = {"F_cap": f_cap, "fetch_worst": worst_fetch, "fetch_all": fetch_per_mb,
                 "P_cap": p_cap, "E_cap": e_cap, "tokens_per_chunk_max": max_tpc,
                 "tokens_per_chunk_all": per_chunk, "rate_min": min_rate, "rates_all": rates,
                 "S_cap": s_cap, "T_cap": t_cap, "three_T_cap": n1_raw,
                 "longest_real_job": longest_real, "max_job_duration_seconds": n1,
                 "over_4h": n1 > 4 * 3600}

    # ---- N2 / N3 --------------------------------------------------------
    post_fix = after + confirm
    b_after = beats_of(post_fix)
    b_before = beats_of(before)
    keep = (b_after["beats_57014"] == 0 and b_after["log_heartbeat_failed_querycanceled"] == 0
            and b_after["log_assuming_it_is_lost"] == 0 and b_after["longest_beat_seconds"] < 1.0)
    lease = 300 if keep else max(300, ceil_to(2 * s_cap, 60))
    out["N2"] = {"before_fix": b_before, "after_fix": b_after, "keep_5_minutes": keep,
                 "lease_seconds": lease, "beat_seconds": 60}
    if keep:
        h = b_after["longest_beat_seconds"]
        timeout = max(5, math.ceil(10 * h))
        out["N3"] = {"applies": True, "H": h, "ten_H": 10 * h, "timeout_seconds": timeout,
                     "constraint": f"4 x 60 + {timeout} = {240 + timeout} < {lease}",
                     "constraint_holds": 240 + timeout < lease, "ten_H_over_15": 10 * h > 15}
    else:
        out["N3"] = {"applies": False, "timeout_seconds": 15}

    # ---- N4 / N5 --------------------------------------------------------
    large = [r for r in real if r["repository"]["full_name"] == "django/django"]
    if large:
        rate = embed_rate(large[0])
        limits = large[0]["openai"]["limits"]
        tpm = int(limits["x-ratelimit-limit-tokens"])
        rpm = int(limits["x-ratelimit-limit-requests"])
        ratio = min(tpm / rate["tokens_per_minute"], rpm / rate["calls_per_minute"])
        max_connections = large[0]["host"]["max_connections"]
        n_openai = math.floor(0.7 * ratio)
        n_db = math.floor(0.25 * max_connections / 2)
        n = max(1, min(n_openai, n_db))
        out["N4"] = {"W_tpm": rate["tokens_per_minute"], "W_rpm": rate["calls_per_minute"],
                     "TPM": tpm, "RPM": rpm, "TPM_over_W_tpm": tpm / rate["tokens_per_minute"],
                     "RPM_over_W_rpm": rpm / rate["calls_per_minute"], "N_openai": n_openai,
                     "max_connections": max_connections, "N_db": n_db, "N": n,
                     "confirmations": [{"record": c["record"], "workers": c["workers"],
                                        "http_429": len(c["openai"]["http_429"]),
                                        "jobs": [(j["label"], j["final"]) for j in c["jobs"]]}
                                       for c in confirm]}
        out["N5"] = {"TPM": tpm, "RPM": rpm, "model": "text-embedding-ada-002",
                     "W_tpm": rate["tokens_per_minute"], "W_rpm": rate["calls_per_minute"],
                     "C": math.floor(ratio),
                     "http_429_by_run": {r["record"]: len(r["openai"]["http_429"]) for r in real + confirm}}

    # ---- N6 / N7 --------------------------------------------------------
    out["N6"] = {"S_cap": s_cap, "stop_grace_period_seconds": max(120, ceil_to(s_cap + 30, 60))}
    rss = max((r["peak_rss_bytes"] for r in at_cap), default=0)
    out["N7"] = {"peak_rss_at_cap_bytes": rss, "peak_rss_at_cap_gib": rss / 2**30,
                 "over_4_gb": rss > 4 * 10**9,
                 "disk_peak_workdir_bytes": max((r["peak_workdir_bytes"] for r in disk), default=None)}
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
