"""Report the SHAPE of .env files -- never a value.

For each file: BOM present, and for each named variable: present, value
length, quoted, contains a backslash. For a *_PATH variable: whether the
path it names exists and its size. Nothing else is printed.
"""
import os
import sys

WATCH = {
    "backend": [
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_SLUG",
        "GITHUB_WEBHOOK_SECRET",
        "GITHUB_APP_CLIENT_ID",
        "GITHUB_APP_CLIENT_SECRET",
        "SUPABASE_WEBHOOK_SECRET",
        "SUPABASE_URL",
        "DATABASE_URL",
        "REDIS_URL",
        "PORT",
    ],
    "workers": ["OPENAI_API_KEY", "DATABASE_URL"],
}

for label, path in (("backend", sys.argv[1]), ("workers", sys.argv[2])):
    raw = open(path, "rb").read()
    print(f"[{label}] bom={raw.startswith(b'\xef\xbb\xbf')} crlf={b'\r\n' in raw}")
    values = {}
    for line in raw.decode("utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip().removeprefix("export ").strip()] = value.strip()
    for name in WATCH[label]:
        if name not in values:
            print(f"  {name}: absent")
            continue
        value = values[name]
        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "'\""
        inner = value[1:-1] if quoted else value
        line = f"  {name}: present length={len(inner)} quoted={quoted} backslash={'\\' in inner}"
        if name.endswith("_PATH"):
            candidate = inner.replace("\\\\", "\\")
            line += f" path_exists={os.path.isfile(candidate)}"
            if os.path.isfile(candidate):
                line += f" file_bytes={os.path.getsize(candidate)}"
                inside = os.path.abspath(candidate).lower().startswith(
                    r"c:\users\alec\desktop\code\testtgsd".lower()
                )
                line += f" inside_repository={inside}"
        print(line)
