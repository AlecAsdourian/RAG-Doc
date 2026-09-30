#!/usr/bin/env python3
"""The measurement environment, and proof that the tokenizer loads with no network.

A MEASUREMENT RECORD, not product code. Prints the Python and package
versions the census ran with, then points HTTP(S)_PROXY at a closed port
BEFORE importing tiktoken, so any download attempt would fail, and loads
`cl100k_base` and the encodings tiktoken assigns to both embedding models.
No OpenAI API call is made: tiktoken's encodings are local files once cached.
"""
import os
import platform
from importlib.metadata import version

os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"

import tiktoken  # noqa: E402  (after the proxy is set, deliberately)

print(f"python {platform.python_version()}")
for package in ("tree-sitter", "tree-sitter-python", "tree-sitter-go", "tree-sitter-javascript",
                "tree-sitter-typescript", "tiktoken"):
    print(f"{package} {version(package)}")
encoding = tiktoken.get_encoding("cl100k_base")
print(f"cl100k_base loaded with the network blocked: {len(encoding.encode('def hello(world): return 42'))} tokens "
      "for 'def hello(world): return 42'")
for model in ("text-embedding-ada-002", "text-embedding-3-small"):
    print(f"{model} -> {tiktoken.encoding_for_model(model).name}")
