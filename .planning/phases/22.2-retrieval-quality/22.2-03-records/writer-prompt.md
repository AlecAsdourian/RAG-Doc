# 22.2-03 — The writers' run prompt

This is the prompt each `blind-question-writer` run is launched with, committed
verbatim before any writer runs (22.2-03-PLAN.md, Task 1, commit A). The
standing rules are the agent's own definition,
`.claude/agents/blind-question-writer.md`; this prompt sets the run's details
and repeats the rules that matter most.

How it is used:
- **Writer 1** is launched with the text under "The prompt", with the
  avoid-list section left exactly as written for writer 1.
- **Writer 2** is launched with the same text, with the avoid-list section
  replaced by writer 1's `(path, symbol)` pairs, one per line, in writer 1's
  order. Writer 2 sees those pairs and nothing else of writer 1's output.
- **A replacement writer** (after a voided batch) gets the same text as the
  writer it replaces.
- **A top-up or a rephrasing** continues the same agent with the matching
  message under "Continuing a writer", and nothing else.

Nothing in this file is changed once a writer has been launched with it.

---

## The prompt

Write **15** questions about the codebase checked out at this root:

    C:/Users/Alec/Desktop/code/rag-bench-corpora/linkwarden

This is the root. Everything you read must be inside it.

**How to read it.**
- Every Grep and Glob call must pass the root, or a directory inside it, as its
  `path` argument, written as an absolute path, for example
  `C:/Users/Alec/Desktop/code/rag-bench-corpora/linkwarden/apps/web`. A Grep
  or Glob call without a `path` searches a different project, and one such
  call discards all of your questions.
- Every Read call names an absolute path inside the root.
- Keep Glob patterns and Grep's `glob` filter relative (`**/*.ts`), never
  absolute, and never use a `..` segment or a relative path anywhere.

**Where to look.** Only these three directories under the root:
- `apps/web`
- `apps/worker`
- `packages`

**Files to skip.** Never target, and do not bother reading:
- type declaration files, whose names end in `.d.ts`;
- tests, whose names end in `.test.ts`, `.test.tsx`, `.spec.ts` or
  `.spec.tsx`;
- anything under a directory named `e2e`;
- anything under `node_modules` or `vendor`.

Only `.ts` and `.tsx` files count.

**What to write.** Questions a developer new to this codebase would ask, each
answered by one named declaration that you have read. For each one: the
question, the file's exact path relative to the root, the answering symbol,
and `path:line — why` as evidence.

**The phrasing.**
- Ask the way a newcomer asks, about behaviour, purpose or a situation.
- Never use the answering symbol's name (or any part of a dotted name), the
  file's name, or any other distinctive identifier from the code.
- About a third of the questions deliberately use words the code does not.

**The kinds.** A mix of "where", "how", "what happens when" and "what
decides".

**The symbol's form.** One of: a named function; a module-level `const` or
`let` bound directly to an arrow function or a function expression (named by
the variable); a class; or a class method written `ClassName.method`. Never
an anonymous default export, a function passed to another call, an
object-literal method, a function nested inside another function, an arrow
function assigned to a class field, or an interface, type alias or enum.
Spell it exactly as declared.

**The spread, across the whole set.** These questions are part of one set of
30, written by two writers in turn. **At most five questions per package,
counted across the whole set of 30, not just yours.**
- A package is the file's directory cut to its first four path segments, or
  the whole directory when it is shallower:
  - `apps/web/lib/api/controllers/x.ts` belongs to
    `apps/web/lib/api`;
  - `apps/web/components/ModalContent/X.tsx` belongs to
    `apps/web/components/ModalContent`;
  - `apps/web/pages/index.tsx` belongs to `apps/web/pages`;
  - `packages/router/x.ts` belongs to `packages/router`.
- Every pair on the avoid-list below counts toward its package's five, as
  well as each of your own questions. If the list already has three in a
  package, you may add at most two there; if it has five, add none.
- Spread across the codebase rather than clustering in one area.

**The avoid-list.** Do not target any `(path, symbol)` pair listed here. A
different symbol in the same file is allowed. The list is all you are given
about earlier questions; do not look for them.

    (none: you are the first writer)

**Your answer.** You cannot write files. Your final message is a JSON array of
15 objects and nothing else: no preamble, no commentary, no code fence. Each
object has exactly the fields `question`, `path`, `symbol` and `evidence`, in
that order. If you cannot meet every rule for the last few, return fewer
rather than break a rule.

---

## Writer 2's avoid-list

For writer 2 (and for any writer replacing writer 2), the avoid-list block in
the prompt above, `(none: you are the first writer)`, is replaced by writer
1's pairs, one per line, indented the same way, in this form:

    apps/web/lib/api/example/file.ts — someFunction
    packages/router/other.ts — SomeClass.someMethod

Nothing else of writer 1's output is passed on.

---

## Continuing a writer

**The top-up.** If a writer's final message holds fewer than 15 questions,
the same agent is continued with this message, `N` being the number missing,
and nothing else:

> Please write N more questions under the same instructions, about
> `(path, symbol)` pairs different from the ones you have already returned,
> and return only the N new ones as a JSON array.

- The spread still counts across the whole set: the avoid-list's pairs, the
  writer's earlier questions and the new ones.
- The top-up's tool calls are audited like the first run's.
- The new questions follow the writer's earlier ones in id order, so the ids
  keep the order the writer produced them in.
- If the top-up still falls short, it is repeated with the new shortfall.

**A rephrasing.** If `--check` warns that a question names its answering
symbol, its writer is continued with this message, and nothing else:

> Your question "QUESTION" uses the word "WORD", which is part of its
> answering symbol's name. Please rephrase that question without it, keeping
> the same path, symbol and evidence, and return it as a JSON array of one
> object.

The exchange is recorded in `writer-audit.txt`, and its tool calls are
audited like the rest.
