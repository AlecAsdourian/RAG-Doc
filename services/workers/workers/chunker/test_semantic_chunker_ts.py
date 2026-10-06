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


# ---------------------------------------------------------------------------
# Decorators inside their definition's chunk (QD6), at each place the
# TypeScript grammar puts one (`22.2-02-records/ts-decorator-placement.txt`)
# ---------------------------------------------------------------------------

DECORATED = '''@Injectable()
class Service {
    run(): void {}
}

@Component({ selector: "app-widget" })
export class Widget {
    @Input()
    label: string;

    @HostListener("click")
    @Throttle(100)
    onClick(): void {}
}

export @Sealed class Box {
    open(): void {}
}
'''


def test_a_decorated_class_starts_at_its_decorator():
    service = _named(DECORATED, "web/src/service.ts")[("class", "Service")]
    assert service.start_line == 1
    assert service.content.startswith("@Injectable()\nclass Service {")


def test_an_exported_class_starts_at_the_decorator_before_export():
    named = _named(DECORATED, "web/src/widget.ts")
    widget = named[("class", "Widget")]
    assert widget.start_line == 6
    assert widget.content.startswith('@Component({ selector: "app-widget" })\nexport class Widget {')
    box = named[("class", "Box")]
    assert box.content.startswith("export @Sealed class Box {"), "a decorator after export is on the class line"


def test_a_decorated_method_starts_at_its_first_decorator_and_keeps_its_breadcrumb():
    on_click = _named(DECORATED, "web/src/widget.ts")[("function", "Widget.onClick")]
    assert on_click.start_line == 11
    assert on_click.content.startswith('    @HostListener("click")\n    @Throttle(100)\n    onClick(): void {}')
    assert on_click.metadata["ancestor_chain"] == ["Widget"]


def test_a_decorated_property_is_not_a_chunk_and_stays_in_its_class():
    named = _named(DECORATED, "web/src/widget.ts")
    assert not any("label" in name for _, name in named)
    assert "@Input()\n    label: string;" in named[("class", "Widget")].content


# ---------------------------------------------------------------------------
# JSDoc as the docstring, once (QA4, QA5)
# ---------------------------------------------------------------------------

JSDOC = '''/** Adds one. */
function plain(a: number): number {
    return a + 1;
}

/**
 * Loads a user by id.
 * @param id the user's id
 */
export function load(id: string): string {
    return id;
}

/** Doubles x. */
export const double = (x: number) => x * 2;

/** Keeps recipes. */
class Store {
    /** Saves a recipe. */
    save(): void {}

    /** Routed. */
    @Get("/recipes")
    list(): void {}
}

// A line comment is not documentation.
function lined(): void {}

/** Separated by a blank line. */

function separated(): void {}

/** Above the decorator. */
@Injectable()
export class Decorated {}
'''


def _docs():
    return {name: c.metadata.get("docstring") for (_, name), c in _named(JSDOC, "web/src/docs.ts").items()}


def test_jsdoc_above_a_function_an_exported_function_and_a_const_is_the_docstring():
    docs = _docs()
    assert docs["plain"] == "Adds one."
    assert docs["load"] == "Loads a user by id. @param id the user's id"
    assert docs["double"] == "Doubles x."


def test_jsdoc_above_a_class_a_method_and_a_decorated_method():
    docs = _docs()
    assert docs["Store"] == "Keeps recipes."
    assert docs["Store.save"] == "Saves a recipe."
    assert docs["Store.list"] == "Routed.", "the JSDoc sits above the method's first decorator"


def test_jsdoc_above_a_decorator_is_the_docstring_and_the_chunk_starts_at_the_decorator():
    decorated = _named(JSDOC, "web/src/docs.ts")[("class", "Decorated")]
    assert decorated.metadata["docstring"] == "Above the decorator."
    assert decorated.content.startswith("@Injectable()\nexport class Decorated {}")


def test_a_line_comment_and_a_separated_jsdoc_are_not_docstrings():
    docs = _docs()
    assert docs["lined"] is None
    assert docs["separated"] is None


def test_the_jsdoc_is_not_in_the_chunk_text():
    """Embedded once: through the docstring, never also through the content.
    (A class chunk holds its methods, and so their JSDoc; its own JSDoc, the
    one above `class`, is still outside it.)"""
    for (_, name), chunk in _named(JSDOC, "web/src/docs.ts").items():
        doc = chunk.metadata.get("docstring")
        if doc:
            assert f"/** {doc}" not in chunk.content and doc.split(".")[0] not in chunk.content, (
                f"{name}'s chunk text carries its own JSDoc"
            )
