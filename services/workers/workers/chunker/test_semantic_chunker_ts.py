"""Chunk-level behaviour for TypeScript (22.2-02, QD5).

`test_parser.py` proves each grammar parses clean and each query set matches.
These prove what the chunker makes of it: which declarations become chunks,
and the breadcrumb each carries -- the name the harness scores symbol-level
questions by (`scoring.py`), and what the embedding text leads with.

QD5, LOCKED: functions and generators, methods (inside a class only), classes
(abstract ones included), and module-level `const`/`let` bound to an arrow
function or a function expression, named by the variable. NOT chunks: an
anonymous default export, an object literal's method, a function passed to a
call, a class-field arrow function, and interfaces, type aliases and enums.
"""

from workers.chunker.semantic_chunker import SemanticChunker

TS_SOURCE = '''import { db } from "./db";

export interface User { id: string }
export type Id = string;
export enum Role { Admin, Member }

function plain(a: number): number {
    return a + 1;
}

function* ids(): Generator<number> {
    yield 1;
}

export function exported(user: User): string {
    return user.id;
}

export default function main(): void {
    plain(1);
}

class Repo {
    find(id: Id): User | undefined {
        return db.get(id);
    }

    onSave = () => db.flush();
}

export abstract class Base<T> {
    abstract load(id: string): T;

    describe(): string {
        return "base";
    }
}

const arrow = (x: number) => x * 2;

export const handler = async function (req: Request): Promise<Response> {
    return new Response(req.url);
};

function outer(): number {
    function inner(): number {
        return 2;
    }
    return inner();
}

export const Page = () => {
    function render(): string {
        return "page";
    }
    return render();
};

export const wrapped = withAuth(async () => {
    return 1;
});

export const config = {
    build() {
        return 3;
    },
};
'''

ANONYMOUS_DEFAULT = '''export default function () {
    return 1;
}
'''


def _chunks(source=TS_SOURCE, path="web/src/repo.ts"):
    return SemanticChunker().chunk_file(path, source, "typescript")


def _named(source=TS_SOURCE, path="web/src/repo.ts"):
    return {
        (c.chunk_type, c.metadata["breadcrumb"]): c
        for c in _chunks(source, path)
        if c.chunk_type in ("function", "class")
    }


def test_every_chunkable_declaration_is_a_chunk_named_as_scored():
    assert sorted(_named()) == [
        ("class", "Base"),
        ("class", "Repo"),
        ("function", "Base.describe"),
        ("function", "Page"),
        ("function", "Page.render"),
        ("function", "Repo.find"),
        ("function", "arrow"),
        ("function", "exported"),
        ("function", "handler"),
        ("function", "ids"),
        ("function", "main"),
        ("function", "outer"),
        ("function", "outer.inner"),
        ("function", "plain"),
    ]


def test_a_function_and_a_generator():
    named = _named()
    assert named[("function", "plain")].content.startswith("function plain(a: number)")
    assert named[("function", "ids")].content.startswith("function* ids()")


def test_an_exported_and_a_default_exported_function_keep_their_export_line():
    named = _named()
    exported = named[("function", "exported")]
    assert exported.content.startswith("export function exported(")
    assert exported.metadata["function_name"] == "exported"
    assert named[("function", "main")].content.startswith("export default function main()")


def test_a_class_an_abstract_class_and_their_methods():
    named = _named()
    assert named[("class", "Repo")].content.startswith("class Repo {")
    assert named[("class", "Base")].content.startswith("export abstract class Base<T> {")
    assert named[("function", "Repo.find")].metadata["ancestor_chain"] == ["Repo"]
    # An abstract class is a scope: its method reads Class.method.
    assert named[("function", "Base.describe")].metadata["ancestor_chain"] == ["Base"]
    assert named[("function", "Base.describe")].metadata["parent_scope"] == "abstract class Base<T> {"


def test_a_const_bound_function_is_named_by_its_variable_and_spans_the_declaration():
    named = _named()
    arrow = named[("function", "arrow")]
    assert arrow.metadata["function_name"] == "arrow"
    assert arrow.content == "const arrow = (x: number) => x * 2;"
    handler = named[("function", "handler")]
    assert handler.content.startswith("export const handler = async function (req: Request)")
    assert handler.content.endswith("};"), "the chunk spans the whole declaration"


def test_a_nested_function_is_chunked_as_in_python():
    named = _named()
    inner = named[("function", "outer.inner")]
    assert inner.content.lstrip().startswith("function inner()")
    assert inner.metadata["ancestor_chain"] == ["outer"]
    # Inside a const-bound function, the variable names the scope.
    assert named[("function", "Page.render")].metadata["ancestor_chain"] == ["Page"]


def test_what_is_not_a_chunk():
    named = _named()
    names = {name for _, name in named}
    assert "wrapped" not in names, "a function passed to a call is not chunked"
    assert not any("build" in n for n in names), "an object literal's method is not chunked"
    assert not any("onSave" in n for n in names), "a class-field arrow function is not chunked"
    for symbol in ("User", "Id", "Role"):
        assert symbol not in names, f"{symbol}: interfaces, type aliases and enums are 22.1-01's symbols"


def test_an_anonymous_default_export_is_not_a_chunk_and_its_file_falls_back():
    chunks = _chunks(ANONYMOUS_DEFAULT, "web/src/anon.ts")
    assert chunks and all(c.chunk_type == "fixed_size" for c in chunks), [c.chunk_type for c in chunks]


def test_the_same_declarations_in_a_tsx_file():
    """The TSX grammar has its own compiled queries; the same source, as
    `.tsx`, yields the same chunks."""
    source = TS_SOURCE.replace("function* ids(): Generator<number>", "function* ids()")
    assert sorted(_named(source, "web/src/repo.tsx")) == sorted(_named(source, "web/src/repo.ts"))
