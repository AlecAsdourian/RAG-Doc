"""Chunk-level behaviour for decorated Python definitions (22.2-02, QD6).

QD6, LOCKED: a chunk starts at its definition's first decorator. Before
22.2-02 a decorated function's chunk started at `def`, so `@router.get(...)`
sat in no chunk, or only inside an enclosing class chunk (mealie: 105 and 466
decorators). Only the span moves: the breadcrumb is built from the definition
node as before, which is what symbol scoring reads (`scoring.py`).

Python docstrings are left as they are: inside the body, so in the chunk text
and in `metadata["docstring"]` (QA5's reading, LOCKED 2026-10-06; 22.2-04's
to consider).
"""

from workers.chunker.semantic_chunker import SemanticChunker

PY_SOURCE = '''from fastapi import APIRouter

router = APIRouter()


@router.get("/recipes")
async def list_recipes():
    return []


@cache
@router.get("/recipes/{recipe_id}")
def create_recipe(data):
    return data


class RecipeService:
    """Reads and writes recipes."""

    @property
    def name(self):
        """The service's name."""
        return "recipes"

    def plain(self):
        return 1


@dataclass(frozen=True)
class Recipe:
    title: str


def undecorated():
    return 2
'''


def _named(source=PY_SOURCE, path="mealie/routes/recipes.py"):
    chunks = SemanticChunker().chunk_file(path, source, "python")
    return {c.metadata["breadcrumb"]: c for c in chunks if c.chunk_type in ("function", "class")}


def _line(source, text):
    """The 1-indexed line of the first line that starts with `text` (stripped)."""
    return next(i for i, ln in enumerate(source.splitlines(), start=1) if ln.strip().startswith(text))


def test_a_decorated_top_level_function_starts_at_its_decorator():
    chunk = _named()["list_recipes"]
    assert chunk.start_line == _line(PY_SOURCE, '@router.get("/recipes")')
    assert chunk.content.startswith('@router.get("/recipes")\nasync def list_recipes():')
    assert chunk.metadata["function_name"] == "list_recipes"
    assert chunk.metadata["ancestor_chain"] == []


def test_a_decorated_method_starts_at_its_decorator_and_keeps_its_breadcrumb():
    chunk = _named()["RecipeService.name"]
    assert chunk.start_line == _line(PY_SOURCE, "@property")
    assert chunk.content.startswith("    @property\n    def name(self):")
    assert chunk.metadata["ancestor_chain"] == ["RecipeService"]
    assert chunk.metadata["parent_scope"] == "class RecipeService:"


def test_stacked_decorators_start_at_the_first():
    chunk = _named()["create_recipe"]
    assert chunk.start_line == _line(PY_SOURCE, "@cache")
    assert chunk.content.startswith('@cache\n@router.get("/recipes/{recipe_id}")\ndef create_recipe(data):')


def test_a_decorated_class_starts_at_its_decorator():
    chunk = _named()["Recipe"]
    assert chunk.chunk_type == "class"
    assert chunk.start_line == _line(PY_SOURCE, "@dataclass(frozen=True)")
    assert chunk.content.startswith("@dataclass(frozen=True)\nclass Recipe:")


def test_an_undecorated_definition_is_unchanged():
    named = _named()
    assert named["undecorated"].content.startswith("def undecorated():")
    assert named["RecipeService.plain"].content.startswith("    def plain(self):")
    assert named["RecipeService"].content.startswith("class RecipeService:")


def test_a_decorated_methods_docstring_is_still_extracted():
    assert _named()["RecipeService.name"].metadata["docstring"] == "The service's name."


def test_the_names_are_those_of_the_undecorated_definitions():
    """Every breadcrumb, function name and class name is the definition's own."""
    assert sorted(_named()) == [
        "Recipe",
        "RecipeService",
        "RecipeService.name",
        "RecipeService.plain",
        "create_recipe",
        "list_recipes",
        "undecorated",
    ]
