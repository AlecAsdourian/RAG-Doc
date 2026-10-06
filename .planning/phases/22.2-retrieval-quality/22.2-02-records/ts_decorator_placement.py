#!/usr/bin/env python3
"""Where tree-sitter-typescript puts a decorator, and where the chunk now starts (22.2-02, Task 2).

Answers 22.2-CONTEXT's open question with one fixture per position: a class, an
exported class (decorator before `export`, and after it), a method and a
property. For each decorator it prints the node it is a child of and whether
it is a sibling of the declaration; then the chunks SemanticChunker makes of
the fixture, with their start lines. Offline: no corpus, database or network.

    ts_decorator_placement.py > ts-decorator-placement.txt   (run from services/workers)
"""
import sys
from importlib import metadata
from pathlib import Path

WORKERS = Path(__file__).resolve().parents[4] / "services" / "workers"
sys.path.insert(0, str(WORKERS))

from workers.chunker.semantic_chunker import SemanticChunker  # noqa: E402

FIXTURES = {
    "a class": "@Injectable()\nclass Service {\n  run() {}\n}\n",
    "an exported class, decorator before export": "@Component({ selector: 'x' })\nexport class Widget {\n  render() {}\n}\n",
    "an exported class, decorator after export": "export @Sealed class Box {\n  open() {}\n}\n",
    "a method": "class Controller {\n  @Get('/items')\n  @Auth()\n  list() {\n    return [];\n  }\n}\n",
    "a property": "class Form {\n  @Input()\n  name: string;\n}\n",
}

DECLARATIONS = ("class_declaration", "abstract_class_declaration", "method_definition", "public_field_definition")


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def main() -> None:
    chunker = SemanticChunker()
    parser = chunker.parser
    print(f"tree-sitter {metadata.version('tree-sitter')}, "
          f"tree-sitter-typescript {metadata.version('tree-sitter-typescript')}; grammar: "
          f"{parser.grammar_for('typescript', 'x.ts')} (the chunker's own choice for a .ts path)")
    for label, source in FIXTURES.items():
        print(f"\n== {label}")
        for i, line in enumerate(source.splitlines(), start=1):
            print(f"   {i:>2} | {line}")
        tree = parser.parse_file(source, "typescript", path="web/fixture.ts")
        print(f"   parse errors: {tree.root_node.has_error}")
        for node in walk(tree.root_node):
            if node.type != "decorator":
                continue
            parent = node.parent
            text = source.encode()[node.start_byte:node.end_byte].decode()
            siblings = [s.type for s in parent.named_children if s.type in DECLARATIONS and s is not node]
            where = (f"a sibling of {', '.join(siblings)} in the {parent.type}" if siblings and parent.type == "class_body"
                     else f"a child of the {parent.type}")
            print(f"   decorator {text!r} on line {node.start_point[0] + 1}: {where}")
        for chunk in chunker.chunk_file("web/fixture.ts", source, "typescript"):
            if chunk.chunk_type in ("function", "class"):
                print(f"   chunk {chunk.chunk_type} {chunk.metadata['breadcrumb']}: lines "
                      f"{chunk.start_line}-{chunk.end_line}, starts with {chunk.content.splitlines()[0]!r}")


if __name__ == "__main__":
    main()
