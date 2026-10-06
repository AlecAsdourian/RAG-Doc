"""Tests for TreeSitterParser."""

import pytest
from tree_sitter import Query, QueryError

from workers.chunker.semantic_chunker import SemanticChunker
from workers.parser import TreeSitterParser, tree_sitter_parser
from workers.parser.tree_sitter_parser import GrammarMismatch


class TestPythonParsing:
    """Test Python code parsing."""

    def test_extract_python_function(self):
        """Test extracting a simple Python function."""
        parser = TreeSitterParser()
        code = '''def hello_world():
    """Say hello."""
    print("Hello, World!")
    return True
'''
        tree = parser.parse_file(code, "python")
        functions = parser.extract_functions(tree, code, "python")

        assert len(functions) == 1
        assert functions[0]["name"] == "hello_world"
        assert functions[0]["start_line"] == 1
        assert functions[0]["end_line"] == 4
        assert "Say hello" in functions[0].get("docstring", "")

    def test_extract_python_class(self):
        """Test extracting a Python class."""
        parser = TreeSitterParser()
        code = '''class Calculator:
    """A simple calculator."""

    def add(self, a, b):
        return a + b
'''
        tree = parser.parse_file(code, "python")
        classes = parser.extract_classes(tree, code, "python")

        assert len(classes) == 1
        assert classes[0]["name"] == "Calculator"
        assert classes[0]["start_line"] == 1
        assert classes[0]["end_line"] == 5
        assert "simple calculator" in classes[0].get("docstring", "")

    def test_extract_multiple_python_functions(self):
        """Test extracting multiple Python functions."""
        parser = TreeSitterParser()
        code = '''def func_one():
    pass

def func_two():
    pass

def func_three():
    pass
'''
        tree = parser.parse_file(code, "python")
        functions = parser.extract_functions(tree, code, "python")

        assert len(functions) == 3
        assert functions[0]["name"] == "func_one"
        assert functions[1]["name"] == "func_two"
        assert functions[2]["name"] == "func_three"

    def test_python_line_numbers_accurate(self):
        """Test that line numbers match source code."""
        parser = TreeSitterParser()
        code = '''# Line 1
# Line 2
def my_function():  # Line 3
    x = 1  # Line 4
    y = 2  # Line 5
    return x + y  # Line 6
# Line 7
'''
        tree = parser.parse_file(code, "python")
        functions = parser.extract_functions(tree, code, "python")

        assert len(functions) == 1
        assert functions[0]["name"] == "my_function"
        assert functions[0]["start_line"] == 3
        assert functions[0]["end_line"] == 6


class TestGoParsing:
    """Test Go code parsing."""

    def test_extract_go_function(self):
        """Test extracting a simple Go function."""
        parser = TreeSitterParser()
        code = '''package main

func HelloWorld() string {
    return "Hello, World!"
}
'''
        tree = parser.parse_file(code, "go")
        functions = parser.extract_functions(tree, code, "go")

        assert len(functions) == 1
        assert functions[0]["name"] == "HelloWorld"
        assert functions[0]["start_line"] == 3
        assert functions[0]["end_line"] == 5

    def test_extract_go_struct(self):
        """Test extracting a Go struct (class equivalent)."""
        parser = TreeSitterParser()
        code = '''package main

type Calculator struct {
    result int
}
'''
        tree = parser.parse_file(code, "go")
        classes = parser.extract_classes(tree, code, "go")

        assert len(classes) == 1
        assert classes[0]["name"] == "Calculator"
        assert classes[0]["start_line"] == 3
        assert classes[0]["end_line"] == 5

    def test_extract_multiple_go_functions(self):
        """Test extracting multiple Go functions."""
        parser = TreeSitterParser()
        code = '''package main

func Add(a, b int) int {
    return a + b
}

func Subtract(a, b int) int {
    return a - b
}
'''
        tree = parser.parse_file(code, "go")
        functions = parser.extract_functions(tree, code, "go")

        assert len(functions) == 2
        assert functions[0]["name"] == "Add"
        assert functions[1]["name"] == "Subtract"


    def test_extract_go_methods(self):
        """Methods are extracted alongside plain functions, in file order.

        Regression guard: the Go query matched only `function_declaration`, so
        every method -- every HTTP handler, every GitHub client call -- was
        absent from the index. Measured on this repository's backend: 85 of
        186 Go callables.
        """
        parser = TreeSitterParser()
        code = '''package main

type Client struct{}

func (c *Client) AppJWT() (string, error) {
    return "", nil
}

func (c Client) Name() string {
    return "c"
}

func NewClient() *Client {
    return &Client{}
}
'''
        tree = parser.parse_file(code, "go")
        functions = parser.extract_functions(tree, code, "go")

        assert [f["name"] for f in functions] == ["AppJWT", "Name", "NewClient"]
        assert functions[0]["start_line"] == 5
        assert functions[0]["end_line"] == 7

class TestTypeScriptParsing:
    """TypeScript, TSX and JavaScript, each in its own grammar (22.2-02, QD5).

    ⚠ ASSERT THE PARSE, NOT ONLY THE NAMES. Until 22.2-02 TypeScript was parsed
    with the JavaScript grammar, which "parses" type annotations into `ERROR`
    nodes, and these tests passed because they asserted names alone. Every
    fixture here asserts `has_error` is false in the grammar the parser chose.
    """

    def test_extract_typescript_function(self):
        """Test extracting a TypeScript function."""
        parser = TreeSitterParser()
        code = '''function greet(name: string): string {
    return `Hello, ${name}!`;
}
'''
        tree = parser.parse_file(code, "typescript", path="src/greet.ts")
        assert not tree.root_node.has_error
        functions = parser.extract_functions(tree, code, "typescript", path="src/greet.ts")

        assert len(functions) == 1
        assert functions[0]["name"] == "greet"
        assert functions[0]["start_line"] == 1
        assert functions[0]["end_line"] == 3

    def test_extract_typescript_class(self):
        """Test extracting a TypeScript class."""
        parser = TreeSitterParser()
        code = '''class Calculator {
    add(a: number, b: number): number {
        return a + b;
    }
}
'''
        tree = parser.parse_file(code, "typescript", path="src/calc.ts")
        assert not tree.root_node.has_error
        classes = parser.extract_classes(tree, code, "typescript", path="src/calc.ts")

        assert len(classes) == 1
        assert classes[0]["name"] == "Calculator"
        assert classes[0]["start_line"] == 1
        assert classes[0]["end_line"] == 5

    def test_extract_typescript_class_methods(self):
        """Test extracting methods from TypeScript class."""
        parser = TreeSitterParser()
        code = '''class Math {
    add(a: number, b: number): number {
        return a + b;
    }

    subtract(a: number, b: number): number {
        return a - b;
    }
}
'''
        tree = parser.parse_file(code, "typescript", path="src/math.ts")
        assert not tree.root_node.has_error
        functions = parser.extract_functions(tree, code, "typescript", path="src/math.ts")

        # Should extract both methods
        assert len(functions) == 2
        assert functions[0]["name"] == "add"
        assert functions[1]["name"] == "subtract"

    def test_extract_javascript_function(self):
        """Test extracting a JavaScript function."""
        parser = TreeSitterParser()
        code = '''function calculate(x, y) {
    return x * y;
}
'''
        tree = parser.parse_file(code, "javascript", path="src/calc.js")
        assert not tree.root_node.has_error
        functions = parser.extract_functions(tree, code, "javascript", path="src/calc.js")

        assert len(functions) == 1
        assert functions[0]["name"] == "calculate"
        assert functions[0]["start_line"] == 1
        assert functions[0]["end_line"] == 3


# ---------------------------------------------------------------------------
# One grammar per file kind, one compiled query set per grammar (22.2-02)
# ---------------------------------------------------------------------------

TS_FIXTURE = '''import { Link } from "./link";

export interface Options { depth: number }

export function crawl(start: Link, options: Options): Link[] {
    return [start];
}

export const normalise = (url: string): string => url.trim();

export abstract class Store<T> {
    abstract get(id: string): T | undefined;

    has(id: string): boolean {
        return this.get(id) !== undefined;
    }
}
'''

TSX_FIXTURE = '''import React from "react";

type Props = { title: string };

export function Header({ title }: Props) {
    return <h1 className="title">{title}</h1>;
}

export const Card = ({ title }: Props) => {
    return (
        <div>
            <Header title={title} />
        </div>
    );
};

export class Panel extends React.Component<Props> {
    render() {
        return <section>{this.props.title}</section>;
    }
}
'''

JS_FIXTURE = '''import { parse } from "./parse.js";

export function load(path) {
    return parse(path);
}

export const save = function (path, data) {
    return [path, data];
};

class Cache {
    get(key) {
        return this.store[key];
    }
}
'''


def _names(parser, code, language, path):
    tree = parser.parse_file(code, language, path=path)
    functions = parser.extract_functions(tree, code, language, path=path)
    classes = parser.extract_classes(tree, code, language, path=path)
    return tree, sorted(f["name"] for f in functions), sorted(c["name"] for c in classes)


class TestGrammarChoice:
    """`grammar_for` is the one place a grammar is chosen; the parse, the
    queries and the census all go through it."""

    def test_the_constructor_compiles_every_query_for_every_grammar(self):
        parser = TreeSitterParser()
        assert set(parser.languages) == {"python", "go", "typescript", "tsx", "javascript"}
        for grammar in parser.languages:
            assert set(parser.queries[grammar]) == {"functions", "classes"}, grammar
            for kind, query in parser.queries[grammar].items():
                assert isinstance(query, Query), (grammar, kind)
        # One compiled set per grammar: the TypeScript and TSX objects are
        # different objects, and JavaScript does not share TypeScript's.
        assert parser.queries["tsx"]["functions"] is not parser.queries["typescript"]["functions"]
        assert parser.queries["javascript"] is not parser.queries["typescript"]

    def test_a_query_that_fails_to_compile_raises_from_the_constructor(self, monkeypatch):
        """Today's class query (`name: (identifier)`) cannot compile under either
        TypeScript grammar. Compiled in the constructor, it raises there --
        outside any fallback -- which stops every ingest loudly.

        ⚠ The source is built as a NEW string object: tree-sitter's failed
        compile writes NULs into the query string's own buffer, so a literal
        shared with the parser would change what later compiles report
        (`22.2-02-PLAN.md`). Only the exception type is asserted.
        """
        bad = "".join(list("(class_declaration\n    name: (identifier) @name) @class\n"))
        monkeypatch.setattr(tree_sitter_parser, "_TYPESCRIPT_CLASSES", bad)
        with pytest.raises(QueryError):
            TreeSitterParser()

    @pytest.mark.parametrize(
        "path,language,grammar",
        [
            ("web/a.ts", "typescript", "typescript"),
            ("web/A.TSX", "typescript", "tsx"),
            ("web/b.tsx", "typescript", "tsx"),
            ("web/c.js", "javascript", "javascript"),
            ("web/d.jsx", "javascript", "javascript"),
            ("svc/e.py", "python", "python"),
            ("cmd/f.go", "go", "go"),
        ],
    )
    def test_the_grammar_is_chosen_by_language_and_extension(self, path, language, grammar):
        assert TreeSitterParser().grammar_for(language, path) == grammar

    def test_typescript_without_a_path_is_refused(self):
        with pytest.raises(ValueError, match="extension"):
            TreeSitterParser().parse_file("const x = 1;", "typescript")

    @pytest.mark.parametrize(
        "code,language,path,grammar",
        [
            (TS_FIXTURE, "typescript", "web/store.ts", "typescript"),
            (TSX_FIXTURE, "typescript", "web/card.tsx", "tsx"),
            (JS_FIXTURE, "javascript", "web/cache.js", "javascript"),
        ],
    )
    def test_each_fixture_parses_clean_in_the_grammar_the_parser_chooses(self, code, language, path, grammar):
        parser = TreeSitterParser()
        tree = parser.parse_file(code, language, path=path)
        assert tree.language == parser.languages[grammar], "parsed with the grammar grammar_for names"
        assert not tree.root_node.has_error, f"{path} has parse errors in the {grammar} grammar"

    def test_jsx_does_not_parse_in_the_typescript_grammar(self):
        """Why the choice matters: the same TSX source has errors as `.ts`."""
        parser = TreeSitterParser()
        assert parser.parse_file(TSX_FIXTURE, "typescript", path="web/card.ts").root_node.has_error
        assert not parser.parse_file(TSX_FIXTURE, "typescript", path="web/card.tsx").root_node.has_error

    def test_the_typescript_queries_name_functions_consts_methods_and_classes(self):
        tree, functions, classes = _names(TreeSitterParser(), TS_FIXTURE, "typescript", "web/store.ts")
        assert not tree.root_node.has_error
        assert functions == ["crawl", "has", "normalise"], "an abstract signature is not a method"
        assert classes == ["Store"], "an abstract class is a class"

    def test_the_tsx_queries_name_functions_consts_methods_and_classes(self):
        """A query compiled for one TypeScript grammar matches NOTHING on the
        other's tree (measured). With one query set per language, this file
        would yield no name at all."""
        tree, functions, classes = _names(TreeSitterParser(), TSX_FIXTURE, "typescript", "web/card.tsx")
        assert not tree.root_node.has_error
        assert functions == ["Card", "Header", "render"]
        assert classes == ["Panel"]

    def test_the_javascript_queries_match_javascript(self):
        tree, functions, classes = _names(TreeSitterParser(), JS_FIXTURE, "javascript", "web/cache.js")
        assert not tree.root_node.has_error
        assert functions == ["get", "load", "save"]
        assert classes == ["Cache"], "JavaScript names a class with an identifier, not a type_identifier"

    def test_a_tree_is_never_queried_with_another_grammars_set(self):
        """A guard, not only a convention: a TSX tree handed to the extractors
        with a `.ts` path would be queried with the TypeScript set, match
        nothing and say nothing. It raises instead."""
        parser = TreeSitterParser()
        tsx_tree = parser.parse_file(TSX_FIXTURE, "typescript", path="web/card.tsx")
        with pytest.raises(GrammarMismatch):
            parser.extract_functions(tsx_tree, TSX_FIXTURE, "typescript", path="web/card.ts")
        with pytest.raises(GrammarMismatch):
            parser.extract_classes(tsx_tree, TSX_FIXTURE, "typescript", path="web/card.ts")


class TestChunkFileByGrammar:
    """Through `SemanticChunker.chunk_file`, the path the ingest takes."""

    @staticmethod
    def _named(path, code, language):
        chunks = SemanticChunker().chunk_file(path, code, language)
        return sorted(
            (c.chunk_type, c.metadata["breadcrumb"]) for c in chunks if c.chunk_type in ("function", "class")
        )

    def test_a_tsx_file_yields_its_named_chunks(self):
        assert self._named("web/card.tsx", TSX_FIXTURE, "typescript") == [
            ("class", "Panel"),
            ("function", "Card"),
            ("function", "Header"),
            ("function", "Panel.render"),
        ]

    def test_a_ts_file_yields_its_named_chunks(self):
        assert self._named("web/store.ts", TS_FIXTURE, "typescript") == [
            ("class", "Store"),
            ("function", "Store.has"),
            ("function", "crawl"),
            ("function", "normalise"),
        ]

    def test_a_js_file_yields_its_named_chunks(self):
        assert self._named("web/cache.js", JS_FIXTURE, "javascript") == [
            ("class", "Cache"),
            ("function", "Cache.get"),
            ("function", "load"),
            ("function", "save"),
        ]


class TestParserEdgeCases:
    """Test edge cases and error handling."""

    def test_unsupported_language(self):
        """Test that unsupported language raises error."""
        parser = TreeSitterParser()
        code = "some code"

        with pytest.raises(ValueError, match="Unsupported language"):
            parser.parse_file(code, "ruby")

    def test_empty_file(self):
        """Test parsing empty file."""
        parser = TreeSitterParser()
        code = ""

        tree = parser.parse_file(code, "python")
        functions = parser.extract_functions(tree, code, "python")
        classes = parser.extract_classes(tree, code, "python")

        assert len(functions) == 0
        assert len(classes) == 0

    def test_code_with_no_functions(self):
        """Test parsing code with no functions."""
        parser = TreeSitterParser()
        code = '''# Just a comment
x = 42
print(x)
'''
        tree = parser.parse_file(code, "python")
        functions = parser.extract_functions(tree, code, "python")

        assert len(functions) == 0
