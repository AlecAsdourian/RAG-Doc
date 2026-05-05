"""Tests for TreeSitterParser."""

import pytest
from workers.parser import TreeSitterParser


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


class TestTypeScriptParsing:
    """Test TypeScript/JavaScript code parsing."""

    def test_extract_typescript_function(self):
        """Test extracting a TypeScript function."""
        parser = TreeSitterParser()
        code = '''function greet(name: string): string {
    return `Hello, ${name}!`;
}
'''
        tree = parser.parse_file(code, "typescript")
        functions = parser.extract_functions(tree, code, "typescript")

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
        tree = parser.parse_file(code, "typescript")
        classes = parser.extract_classes(tree, code, "typescript")

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
        tree = parser.parse_file(code, "typescript")
        functions = parser.extract_functions(tree, code, "typescript")

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
        tree = parser.parse_file(code, "javascript")
        functions = parser.extract_functions(tree, code, "javascript")

        assert len(functions) == 1
        assert functions[0]["name"] == "calculate"
        assert functions[0]["start_line"] == 1
        assert functions[0]["end_line"] == 3


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
