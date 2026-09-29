"""One test case per filter rule (22-04, U7 and the vendored/generated rules).

Parametrised per rule on purpose: the mutation "remove one deny-list entry"
must fail exactly that entry's case, which a single test over a list of
names could not show.
"""

from __future__ import annotations

import pytest

from workers.fetch.filters import (
    GENERATED_GO_LINES,
    LANGUAGES,
    classify_path,
    is_generated_go,
    is_secret_name,
)

# --- U7: the deny-list, one example per entry ---

SECRET_EXAMPLES = [
    (".env", ".env"),
    (".env.*", ".env.production"),
    (".env.* (local)", ".env.local"),
    ("*.pem", "server.pem"),
    ("*.key", "private.key"),
    ("*.p12", "client.p12"),
    ("*.pfx", "cert.pfx"),
    ("*.jks", "keystore.jks"),
    ("*.keystore", "release.keystore"),
    ("id_rsa*", "id_rsa"),
    ("id_rsa* (pub)", "id_rsa.pub"),
    ("id_dsa*", "id_dsa"),
    ("id_ecdsa*", "id_ecdsa"),
    ("id_ed25519*", "id_ed25519"),
    (".npmrc", ".npmrc"),
    (".pypirc", ".pypirc"),
    (".netrc", ".netrc"),
    ("*.tfvars", "prod.tfvars"),
    ("credentials*.json", "credentials.json"),
    ("credentials*.json (suffixed)", "credentials-prod.json"),
    ("service-account*.json", "service-account.json"),
    ("service-account*.json (suffixed)", "service-account-ci.json"),
    (".git-credentials", ".git-credentials"),
]


@pytest.mark.parametrize("rule, name", SECRET_EXAMPLES, ids=[r for r, _ in SECRET_EXAMPLES])
def test_each_deny_list_entry_refuses_its_example(rule: str, name: str) -> None:
    assert is_secret_name(name), f"{rule} must match {name}"
    verdict = classify_path(f"config/{name}")
    assert not verdict.indexable
    assert verdict.reason == "secret"


@pytest.mark.parametrize("name", ["ID_RSA", "Server.PEM", ".ENV", ".Env.Local"])
def test_the_deny_list_is_case_insensitive(name: str) -> None:
    assert classify_path(name).reason == "secret"


@pytest.mark.parametrize("path", [".aws/credentials", "home/deploy/.aws/credentials", ".AWS/Credentials"])
def test_aws_credentials_are_a_secret_wherever_the_directory_sits(path: str) -> None:
    assert classify_path(path).reason == "secret"


def test_the_aws_rule_is_the_credentials_file_only() -> None:
    # `.aws/config` holds region and profile names, not keys, and a file
    # merely named `credentials` elsewhere is judged by its extension.
    assert classify_path(".aws/config").reason == "unsupported"
    assert classify_path("src/credentials").reason == "unsupported"
    assert classify_path("credentials.py").indexable, "the content-level gap, recorded in 22-CONTEXT U7"


def test_a_secret_beats_every_other_rule() -> None:
    # A `.env` inside node_modules is counted as a secret, not as vendored:
    # the secret count is the one worth reading.
    assert classify_path("node_modules/pkg/.env").reason == "secret"


@pytest.mark.parametrize("name", ["env", "environment.py", "pemfile.py", "keys.py", "rsa_notes.md"])
def test_ordinary_names_are_not_secrets(name: str) -> None:
    assert not is_secret_name(name), name


def test_the_id_rsa_prefix_is_deliberately_broad() -> None:
    # `id_rsa*` matches anything that starts with `id_rsa`, notes included.
    # Broad on purpose: a miss here sends key material to OpenAI.
    assert is_secret_name("id_rsa_notes.md")


# --- vendored directories, one case each ---


@pytest.mark.parametrize("directory", ["vendor", "node_modules", "dist", "build", ".git", "third_party"])
def test_each_vendored_directory_is_skipped_at_any_depth(directory: str) -> None:
    assert classify_path(f"{directory}/lib/a.py").reason == "vendored"
    assert classify_path(f"services/api/{directory}/a.py").reason == "vendored"


def test_a_file_merely_named_like_a_vendored_directory_is_fine() -> None:
    assert classify_path("src/vendor.py").indexable
    assert classify_path("src/build.go").indexable


# --- generated files, one case each ---


@pytest.mark.parametrize("name", ["app.min.js", "schema_pb2.py", "api.pb.go"])
def test_each_generated_pattern_is_skipped(name: str) -> None:
    assert classify_path(f"src/{name}").reason == "generated"


# --- lockfiles, one case each ---


@pytest.mark.parametrize(
    "name", ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "go.sum"]
)
def test_each_lockfile_is_skipped(name: str) -> None:
    assert classify_path(name).reason == "lockfile"


# --- the extension map ---


@pytest.mark.parametrize("ext, language", sorted(LANGUAGES.items()))
def test_each_supported_extension_has_its_language(ext: str, language: str) -> None:
    verdict = classify_path(f"src/thing{ext}")
    assert verdict.indexable
    assert verdict.language == language


@pytest.mark.parametrize("name", ["logo.png", "data.csv", "Makefile", "notes.txt", "lib.rs", "a.PY.bak"])
def test_anything_else_is_unsupported(name: str) -> None:
    assert classify_path(f"src/{name}").reason == "unsupported"


def test_an_empty_or_dotted_path_is_unsafe() -> None:
    assert classify_path("").reason == "unsafe_path"
    assert classify_path("/").reason == "unsafe_path"


# --- the generated-Go header ---


def test_the_generated_go_header_is_found_within_twenty_lines() -> None:
    header = "// Code generated by protoc-gen-go. DO NOT EDIT."
    lines = ["// comment"] * (GENERATED_GO_LINES - 1) + [header, "package pb"]
    assert is_generated_go("\n".join(lines))


def test_the_generated_go_header_on_line_twenty_one_does_not_count() -> None:
    header = "// Code generated by protoc-gen-go. DO NOT EDIT."
    lines = ["// comment"] * GENERATED_GO_LINES + [header, "package pb"]
    assert not is_generated_go("\n".join(lines))


@pytest.mark.parametrize(
    "line",
    [
        "// Code generated DO NOT EDIT",  # no trailing period
        "// code generated by x. DO NOT EDIT.",  # lower-case c
        "  // Code generated by x. DO NOT EDIT.",  # not at column 0
        "// Code generated by x. DO NOT EDIT. really",  # trailing text
    ],
)
def test_near_misses_of_the_go_header_do_not_count(line: str) -> None:
    assert not is_generated_go(line + "\npackage x\n")


def test_the_go_header_tolerates_crlf() -> None:
    assert is_generated_go("// Code generated by mockery. DO NOT EDIT.\r\npackage mocks\r\n")
