#!/usr/bin/env python3
"""
scripted_to_declarative.py - Convert a Scripted Jenkinsfile into Declarative
Pipeline syntax, grounded in Groovy's own parser and the documented
Declarative Pipeline grammar - never inventing a directive or step.

Why a real parser (ast_dump.groovy) instead of regex/string-scanning:
Jenkinsfiles are arbitrary Groovy. Nested braces inside strings, GString
interpolation, comments, multi-line method chains, and slashy strings all
break naive brace-counting. Groovy's own compiler cannot be wrong about
Groovy syntax, so structural recognition (statement boundaries, method call
names, closure bodies) is delegated to it entirely (ast_dump.groovy, run as a
subprocess). This script only makes DECISIONS about what recognized
structure maps to; it never re-derives structure itself.

Why source is always copied verbatim, never regenerated: every extracted
piece of code (a step call, a stage body, a post block) is sliced out of the
ORIGINAL file by exact line/column span, never re-serialized from the AST.
This guarantees the tool cannot subtly rewrite - and so cannot subtly break -
code it copies, whether or not it "understands" that code.

The declarative-syntax rules this tool encodes are grounded in Jenkins's own
documentation (jenkins.io/doc/book/pipeline/syntax/) and, concretely:

  * A `steps {}` block may only contain step invocations - Jenkins's own
    Declarative parser rejects assignments, if/for/while/try, and other bare
    Groovy statements there with "Expected a step" (see e.g.
    JENKINS-45829). It does NOT, however, recurse into the body of a
    closure passed to a step (dir {}, withEnv {}, timeout {}, parallel {},
    catchError {}, script {} itself, ...) - whatever is inside such a
    closure is ordinary CPS Groovy regardless of which Pipeline syntax
    you're in. That is why this tool only classifies the TOP-LEVEL
    statements of a stage body, not their nested closures, and why a
    step-with-closure statement is always copied through unexamined.
  * `script {}` is the documented escape hatch for "a block of Scripted
    Pipeline" inside Declarative - used here as the guaranteed-correct
    fallback for any stage body this tool cannot confidently classify as
    plain steps, exactly parallel to configure{} in the Job DSL converter.
  * agent/parameters/triggers/options/environment/post section syntax is
    reproduced only for the small, stable subset verified against Jenkins's
    own docs (see NATIVE MAPPINGS below); anything else is left as original
    source inside script{} rather than guessed at.

This tool cannot, and does not try to, handle every Scripted Jenkinsfile
losslessly - some patterns (dynamic/conditional stage generation via
if/for/while at the point where stages are expected) have no direct
Declarative equivalent and are flagged for manual review rather than
silently misconverted. See README.md, "Known limitations".
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AST_DUMP_GROOVY = os.path.join(SCRIPT_DIR, "ast_dump.groovy")

INDENT = "    "


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

@dataclass
class Report:
    native_mappings: list[str] = field(default_factory=list)
    script_fallbacks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def native(self, what: str):
        self.native_mappings.append(what)

    def fallback(self, what: str):
        self.script_fallbacks.append(what)

    def warn(self, what: str):
        self.warnings.append(what)

    def confidence(self) -> str:
        if self.warnings:
            return "low - see warnings, manual review required"
        if self.script_fallbacks:
            return "medium - structurally converted with script{} fallbacks; run the declarative linter (see README) before trusting this"
        return "medium - fully native mapping; run the declarative linter (see README) before trusting this - this tool cannot execute Jenkins"

    def render(self) -> str:
        lines = [f"Native mappings ({len(self.native_mappings)}):"]
        lines += [f"  - {m}" for m in self.native_mappings] or ["  (none)"]
        lines.append(f"script{{}} fallbacks ({len(self.script_fallbacks)}):")
        lines += [f"  - {m}" for m in self.script_fallbacks] or ["  (none)"]
        if self.warnings:
            lines.append(f"Warnings ({len(self.warnings)}):")
            lines += [f"  - {w}" for w in self.warnings]
        lines.append(f"Confidence: {self.confidence()}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# AST dump invocation + source extraction
# --------------------------------------------------------------------------

def run_ast_dump(path: str) -> dict:
    groovy = shutil.which("groovy")
    if not groovy:
        raise RuntimeError(
            "The 'groovy' command is not on PATH. This tool requires a real Groovy "
            "installation to parse the input (see module docstring for why) - install "
            "Groovy (e.g. `apt-get install groovy`) and retry. This is a hard "
            "requirement, not an optional nicety: without it there is no reliable way "
            "to find statement boundaries in arbitrary Groovy without risking silent "
            "misparsing."
        )
    proc = subprocess.run(
        [groovy, AST_DUMP_GROOVY, path],
        capture_output=True,
        text=True,
        timeout=120,
    )
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else None
    except (json.JSONDecodeError, IndexError):
        data = None
    if data is None or not data.get("ok"):
        err = (data or {}).get("error") if data else (proc.stderr or proc.stdout)
        raise ValueError(f"{path} could not be parsed as Groovy: {err}")
    return data


class Extractor:
    def __init__(self, source: str):
        self.lines = source.splitlines()

    def extract(self, node: dict) -> str:
        sl, sc, el, ec = node["line"], node["col"], node["lastLine"], node["lastCol"]
        if sl == el:
            return self.lines[sl - 1][sc - 1:ec - 1]
        parts = [self.lines[sl - 1][sc - 1:]]
        for i in range(sl, el - 1):
            parts.append(self.lines[i])
        parts.append(self.lines[el - 1][:ec - 1])
        return "\n".join(parts)

    def line(self, n: int) -> str:
        return self.lines[n - 1] if 0 < n <= len(self.lines) else ""

    def lines_between(self, start_line: int, end_line_exclusive: int) -> str:
        # start_line and end_line_exclusive are 1-based; returns full lines
        # [start_line, end_line_exclusive)
        if start_line >= end_line_exclusive:
            return ""
        return "\n".join(self.lines[start_line - 1:end_line_exclusive - 1])

    def span_str(self, node: dict) -> str:
        return f"line {node['line']}-{node['lastLine']}"


def indent_block(text: str, indent: str) -> str:
    if not text:
        return text
    return "\n".join(indent + ln if ln.strip() else ln for ln in text.split("\n"))


# --------------------------------------------------------------------------
# Statement classification
# --------------------------------------------------------------------------

PLAIN_KIND = "call"


def is_plain_statement(stmt: dict) -> bool:
    """A statement is safe to copy verbatim as a Declarative step ONLY at the
    top level of a steps{} body - i.e. it must itself be a call. We do NOT
    recurse into its trailing closure or named-arg closures: whatever is
    inside a step's own closure argument (dir{}, withEnv{}, timeout{},
    parallel{}, catchError{}, a custom step, ...) is ordinary CPS Groovy in
    BOTH Scripted and Declarative Pipeline - Declarative's "steps must be
    steps" restriction applies only at the point where steps{} enumerates
    its direct children, not inside any closure one of those steps takes."""
    return stmt.get("kind") == PLAIN_KIND


def classify_body(stmts: list[dict]) -> bool:
    """Return True if every top-level statement in `stmts` is plain-copyable."""
    return all(is_plain_statement(s) for s in stmts)


def is_dynamic_control(stmt: dict) -> bool:
    return stmt.get("kind") in ("if", "for", "while", "switch")


def statement_annotations(stmt: dict) -> list[str]:
    return stmt.get("annotations") or []


def is_preamble_statement(stmt: dict) -> bool:
    """True for a top-level statement that is really FILE PREAMBLE and must be
    emitted ABOVE `pipeline {}`, not inside it.

    The case that matters in practice is the shared-library directive:

        @Library('my-shared-lib@main') _

    Groovy parses that as an ExpressionStatement wrapping an *annotated*
    DeclarationExpression (the `_` is a throwaway variable that exists purely
    to give the annotation something to attach to), so it shows up as an
    ordinary top-level statement - which the stage classifier would otherwise
    treat as a non-step statement and bury inside a `script {}` block. That is
    always wrong: `@Library` is resolved by Jenkins when the Jenkinsfile is
    COMPILED, so it must sit at the top of the file, before `pipeline {}`, in
    Declarative exactly as in Scripted. (`@Library` written on an `import`
    instead - `@Library('x') import com.foo.Bar` - needs no special handling:
    imports live on the Groovy ModuleNode, not in the statement block, so they
    never appear here and are already copied through with the leading lines.)

    Any other annotated top-level statement (`@Field def x = ...`, `@Grab`,
    ...) is script-level Groovy for the same compile-time reason, so the test
    is on "carries an annotation", not on the annotation's name.
    """
    return bool(statement_annotations(stmt))


def split_preamble(top_level: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split the top-level statements into (preamble, body).

    Only a LEADING run of preamble statements is peeled off: an annotated
    statement appearing after real pipeline code is not something we can hoist
    without reordering the file, so it is left in the body and warned about by
    the caller.
    """
    i = 0
    while i < len(top_level) and is_preamble_statement(top_level[i]):
        i += 1
    return top_level[:i], top_level[i:]


def brief(stmt: dict) -> str:
    """One-line human description of a statement, for diagnostics."""
    if stmt.get("kind") == "call":
        return f"{stmt.get('name') or '<call>'}() at line {stmt['line']}"
    return f"{stmt.get('kind')} statement at line {stmt['line']}"


def closure_bodies_of(stmt: dict) -> list[list[dict]]:
    """Every closure body directly attached to a statement: a trailing closure
    (`dir('x') { ... }`), closures passed as named args (`parallel a: {...}`),
    and the sub-bodies of a try/catch/finally."""
    bodies = []
    tc = stmt.get("trailingClosure")
    if tc and tc.get("body") is not None:
        bodies.append(tc["body"])
    for entry in stmt.get("namedArgs") or []:
        if entry.get("kind") == "closure" and entry.get("closure", {}).get("body") is not None:
            bodies.append(entry["closure"]["body"])
    if stmt.get("kind") == "try":
        bodies.append(stmt.get("tryBody") or [])
        for c in stmt.get("catches") or []:
            bodies.append(c.get("body") or [])
        bodies.append(stmt.get("finallyBody") or [])
    return [b for b in bodies if b]


def find_buried_node_calls(stmts: list[dict], chain: tuple[str, ...] = ()) -> list[tuple[dict, tuple[str, ...]]]:
    """Find `node {}` calls that are NOT at the level being converted - i.e.
    nested inside some other block. Returns (statement, enclosing call names).

    This exists purely for diagnostics, and only matters when the top-level
    search for a node {} has already failed. A buried node is the single most
    common reason for that: wrapping the whole pipeline in `timestamps {}`,
    `ansiColor {}`, `podTemplate {}`, `withCredentials {}`, `ws {}` etc. and
    putting the node inside is idiomatic Scripted, but it means the agent that
    Declarative needs at the top of the file is somewhere this tool will not
    hoist it from - and a copied-through `node {}` inside a `steps {}` block
    is not legal Declarative, so the user needs to be told rather than handed
    output that only looks converted."""
    found = []
    for st in stmts:
        name = st.get("name") if st.get("kind") == "call" else None
        if name == "node" and chain:
            found.append((st, chain))
        inner = chain + (name,) if name else chain
        for body in closure_bodies_of(st):
            found.extend(find_buried_node_calls(body, inner))
    return found


def looks_like_parallel_map(stmt: dict) -> bool:
    return (
        stmt.get("kind") == "call"
        and stmt.get("name") == "parallel"
        and stmt.get("namedArgs")
        and all(e.get("kind") == "closure" for e in stmt["namedArgs"])
    )



# --------------------------------------------------------------------------
# Rendering: stage bodies
# --------------------------------------------------------------------------

def render_plain_steps(stmts: list[dict], extractor: Extractor, indent: str) -> str:
    lines = []
    for s in stmts:
        lines.append(indent_block(extractor.extract(s), indent))
    return "\n".join(lines)


def render_script_wrapped(stmts: list[dict], extractor: Extractor, indent: str) -> str:
    if not stmts:
        return ""
    body_text = extractor.lines_between(stmts[0]["line"], stmts[-1]["lastLine"] + 1)
    inner_indent = indent + INDENT
    out = [f"{indent}script {{"]
    out.append(indent_block(body_text, inner_indent))
    out.append(f"{indent}}}")
    return "\n".join(out)


def render_parallel_stage_body(stmt: dict, extractor: Extractor, indent: str, report: Report, ctx: str) -> str:
    branches = []
    for entry in stmt["namedArgs"]:
        key = entry["key"] or "branch"
        body = entry["closure"]["body"]
        if classify_body(body):
            steps_text = render_plain_steps(body, extractor, indent + INDENT * 2)
            report.native(f"{ctx}/parallel branch '{key}' -> stage/steps (plain)")
        else:
            steps_text = render_script_wrapped(body, extractor, indent + INDENT * 2)
            report.fallback(f"{ctx}/parallel branch '{key}' -> stage/script{{}} (contains non-step statements)")
        branches.append(
            f"{indent}    stage({json.dumps(key)}) {{\n"
            f"{indent}        steps {{\n"
            f"{steps_text}\n"
            f"{indent}        }}\n"
            f"{indent}    }}"
        )
    return f"{indent}parallel {{\n" + "\n".join(branches) + f"\n{indent}}}"


def render_stage(stmt: dict, extractor: Extractor, indent: str, report: Report, agent_override: str | None) -> str:
    label_span = stmt["positionalArgs"][0] if stmt.get("positionalArgs") else None
    label_src = extractor.extract(label_span) if label_span else '"Stage"'
    if label_span and not (label_src.strip().startswith("'") or label_src.strip().startswith('"')):
        report.warn(
            f"stage name at {extractor.span_str(stmt)} is not a plain string literal "
            f"({label_src.strip()!r}) - Declarative Pipeline stage names have limited "
            "support for dynamic/computed names; verify this works as expected or "
            "make the name a literal string."
        )

    body = stmt["trailingClosure"]["body"] if stmt.get("trailingClosure") else []
    ctx = f"stage {label_src.strip()}"

    lines = [f"{indent}stage({label_src}) {{"]
    if agent_override:
        lines.append(f"{indent}{INDENT}agent {{ label {json.dumps(agent_override)} }}")

    if len(body) == 1 and looks_like_parallel_map(body[0]):
        lines.append(render_parallel_stage_body(body[0], extractor, indent + INDENT, report, ctx))
    elif classify_body(body):
        if body:
            report.native(f"{ctx} -> steps {{}} (plain step sequence)")
        steps_text = render_plain_steps(body, extractor, indent + INDENT * 2)
        lines.append(f"{indent}{INDENT}steps {{")
        lines.append(steps_text)
        lines.append(f"{indent}{INDENT}}}")
    else:
        report.fallback(f"{ctx} -> steps {{ script {{}} }} (contains assignment/control-flow/other non-step statement(s))")
        steps_text = render_script_wrapped(body, extractor, indent + INDENT * 2)
        lines.append(f"{indent}{INDENT}steps {{")
        lines.append(steps_text)
        lines.append(f"{indent}{INDENT}}}")

    lines.append(f"{indent}}}")
    return "\n".join(lines)


def make_synthetic_stage(name: str, stmts: list[dict], extractor: Extractor, indent: str, report: Report) -> str:
    ctx = f"synthetic stage '{name}'"
    lines = [f"{indent}stage({json.dumps(name)}) {{"]
    if classify_body(stmts):
        if stmts:
            report.native(f"{ctx} -> steps {{}} (statement(s) outside any stage() call in the original file)")
        steps_text = render_plain_steps(stmts, extractor, indent + INDENT * 2)
    else:
        report.fallback(f"{ctx} -> steps {{ script {{}} }} (statement(s) outside any stage() call, contains non-step statements)")
        steps_text = render_script_wrapped(stmts, extractor, indent + INDENT * 2)
    lines.append(f"{indent}{INDENT}steps {{")
    lines.append(steps_text)
    lines.append(f"{indent}{INDENT}}}")
    lines.append(f"{indent}}}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Rendering: a body as a sequence of stages
# --------------------------------------------------------------------------

def process_body_as_stages(
    stmts: list[dict], extractor: Extractor, indent: str, report: Report, agent_override: str | None = None
) -> list[str]:
    rendered = []
    prelude: list[dict] = []
    saw_dynamic = False

    def flush_prelude(where: str):
        nonlocal prelude
        if prelude:
            name = "Setup" if where == "before" else "Cleanup"
            rendered.append(make_synthetic_stage(name, prelude, extractor, indent, report))
            prelude = []

    saw_any_stage = any(s.get("kind") == "call" and s.get("name") == "stage" for s in stmts)

    for s in stmts:
        if s.get("kind") == "call" and s.get("name") == "stage":
            flush_prelude("before")
            rendered.append(render_stage(s, extractor, indent, report, agent_override))
        elif is_dynamic_control(s):
            saw_dynamic = True
            prelude.append(s)
        else:
            prelude.append(s)

    flush_prelude("after" if saw_any_stage else "before")

    if saw_dynamic:
        report.warn(
            "if/for/while found at a point where sequential stage() calls were "
            "expected - this usually means the original pipeline generates stages "
            "dynamically/conditionally, which Declarative Pipeline has no direct "
            "equivalent for (see 'matrix' for parameterized stages or 'when' for "
            "per-stage conditions in the Jenkins docs, neither of which is a drop-in "
            "replacement for arbitrary Groovy control flow). The affected statements "
            "were bundled into a script{}-wrapped synthetic stage instead of being "
            "restructured - review this section by hand."
        )

    return rendered


# --------------------------------------------------------------------------
# properties()/parameters()/triggers() top-level idiom
# --------------------------------------------------------------------------

def _string_literal_text(extractor: Extractor, span: dict) -> str | None:
    text = extractor.extract(span).strip()
    if (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
        return text
    return None


# --------------------------------------------------------------------------
# Note on properties()/parameters()/triggers() (NOT implemented)
# --------------------------------------------------------------------------
#
# A common Scripted idiom puts `properties([parameters([...]), pipelineTriggers([...])])`
# at the top of the file, which has clean Declarative equivalents (parameters{},
# triggers{}, options{}). This converter deliberately does NOT attempt that
# mapping: doing it correctly requires the AST dumper to describe the
# structure of list-literal arguments (ListExpression entries, each itself a
# call), which ast_dump.groovy does not currently expose. Rather than
# approximate that from raw text and risk silently mis-mapping a parameter
# type, a top-level properties(...) call is left exactly where the general
# statement-classification logic already puts it: as a non-step statement,
# which means it lands in a script{}-wrapped synthetic stage together with
# whatever it was next to, and is called out in the report so a human can
# migrate it to native parameters{}/triggers{}/options{} directives by hand.
# See README.md, "Known limitations" for how to extend this properly.


# --------------------------------------------------------------------------
# try/catch/finally -> post{} best-effort inference
# --------------------------------------------------------------------------

def try_shape_post_inference(body_stmts: list[dict], extractor: Extractor, outer_indent: str, report: Report):
    """If a node/pipeline body is EXACTLY one top-level try/catch/finally
    statement wrapping the real stage sequence, offer a best-effort mapping
    of catch -> post.failure and finally -> post.always. This is explicitly
    flagged as best-effort: try/catch/finally and post{} conditions are not
    semantically identical (post conditions key off build result, not
    exception flow), so this is provided as a starting point, not a proof of
    equivalence."""
    if len(body_stmts) != 1 or body_stmts[0].get("kind") != "try":
        return None
    try_stmt = body_stmts[0]
    report.warn(
        "Detected a top-level try/catch/finally wrapping the whole pipeline body. "
        "Mapped catch -> post.failure and finally -> post.always as a BEST-EFFORT "
        "starting point - try/catch/finally and Declarative's post{} conditions are "
        "not semantically identical (post keys off build result, not exception "
        "flow). Review this mapping by hand, especially if the catch block does "
        "anything beyond marking/reporting failure."
    )
    stage_indent = outer_indent + INDENT
    stages = process_body_as_stages(try_stmt["tryBody"], extractor, stage_indent, report)

    post_blocks = []
    for c in try_stmt.get("catches", []):
        body = c["body"]
        if classify_body(body):
            steps_text = render_plain_steps(body, extractor, outer_indent + INDENT * 2)
            report.native("catch -> post.failure (plain)")
        else:
            steps_text = render_script_wrapped(body, extractor, outer_indent + INDENT * 2)
            report.fallback("catch -> post.failure/script{} (contains non-step statements)")
        post_blocks.append(f"{outer_indent}{INDENT}failure {{\n{steps_text}\n{outer_indent}{INDENT}}}")

    finally_body = try_stmt.get("finallyBody")
    if finally_body:
        if classify_body(finally_body):
            steps_text = render_plain_steps(finally_body, extractor, outer_indent + INDENT * 2)
            report.native("finally -> post.always (plain)")
        else:
            steps_text = render_script_wrapped(finally_body, extractor, outer_indent + INDENT * 2)
            report.fallback("finally -> post.always/script{} (contains non-step statements)")
        post_blocks.append(f"{outer_indent}{INDENT}always {{\n{steps_text}\n{outer_indent}{INDENT}}}")

    post_text = None
    if post_blocks:
        post_text = f"{outer_indent}post {{\n" + "\n".join(post_blocks) + f"\n{outer_indent}}}"

    return stages, post_text


# --------------------------------------------------------------------------
# Top-level conversion
# --------------------------------------------------------------------------

def render_agent(label_text: str | None) -> str:
    if label_text is None:
        return "agent any"
    text = label_text.strip()
    if (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
        return f"agent {{ label {text} }}"
    return f"agent {{ label {text} }}  // original expression was not a plain string literal - verify"


def convert(dump: dict, extractor: Extractor, report: Report) -> str:
    top_level = dump["topLevel"]
    method_decls = dump.get("methodDecls", [])

    # Peel off file preamble (`@Library('x') _` and friends) BEFORE anything
    # else looks at the statement list: those statements belong above
    # pipeline {}, and leaving them in the body would both bury them in a
    # script {} block (where Jenkins would never resolve the library) and
    # break the "is the whole file one node {}?" test below.
    preamble_stmts, body_stmts = split_preamble(top_level)

    if (
        len(body_stmts) == 1
        and body_stmts[0].get("kind") == "call"
        and body_stmts[0].get("name") == "pipeline"
    ):
        report.warn("Input already appears to be a Declarative Pipeline (top-level `pipeline {}` block found) - nothing converted.")
        return extractor.lines_between(1, body_stmts[0]["lastLine"] + 1).rstrip() + "\n"

    if not top_level:
        report.warn("No top-level statements found - nothing to convert.")
        return ""

    # Everything above the first non-preamble statement is copied through
    # verbatim: shebang, comments, imports, and any `@Library` lines - imports
    # interleaved BETWEEN @Library lines and the pipeline body included, which
    # a preamble ending at the first statement of any kind would have dropped.
    preamble_end_line = body_stmts[0]["line"] if body_stmts else len(extractor.lines) + 1
    for st in preamble_stmts:
        anns = ", ".join("@" + a for a in statement_annotations(st))
        report.native(
            f"{anns} at {extractor.span_str(st)} -> preserved verbatim above pipeline {{}} "
            "(annotations are resolved at compile time and are legal in Declarative unchanged)"
        )
    for st in body_stmts:
        if is_preamble_statement(st):
            anns = ", ".join("@" + a for a in statement_annotations(st))
            report.warn(
                f"{anns} at {extractor.span_str(st)} appears AFTER pipeline code rather than at the "
                "top of the file, so it was left where it is instead of being hoisted. Annotations "
                "like @Library must precede the code that uses them - move it to the top of the "
                "file by hand and re-run."
            )

    if not body_stmts:
        report.warn(
            "The file contains nothing but preamble (imports/@Library/annotated declarations) - "
            "no pipeline body was found to convert."
        )
        return extractor.lines_between(1, preamble_end_line).rstrip() + "\n"

    preamble_methods = [m for m in method_decls if m["line"] < preamble_end_line]
    trailing_methods = [m for m in method_decls if m["line"] >= preamble_end_line]

    preamble_text = extractor.lines_between(1, preamble_end_line).rstrip()
    for m in preamble_methods:
        report.native(f"preserved top-level function '{m['name']}' verbatim before pipeline {{}}")

    node_calls = [s for s in body_stmts if s.get("kind") == "call" and s.get("name") == "node"]

    indent = INDENT
    stage_indent = indent + INDENT
    post_text = None

    if node_calls and len(node_calls) == len(body_stmts):
        if len(node_calls) == 1:
            nc = node_calls[0]
            label = extractor.extract(nc["positionalArgs"][0]) if nc.get("positionalArgs") else None
            agent_text = render_agent(label)
            report.native(f"node({label or ''}) -> {agent_text.split('//')[0].strip()}")
            body = nc["trailingClosure"]["body"] if nc.get("trailingClosure") else []
            inferred = try_shape_post_inference(body, extractor, indent, report)
            if inferred:
                stages, post_text = inferred
            else:
                stages = process_body_as_stages(body, extractor, stage_indent, report)
        else:
            agent_text = "agent none"
            report.native(f"{len(node_calls)} sequential top-level node{{}} blocks -> agent none + per-stage agent")
            stages = []
            for nc in node_calls:
                label = extractor.extract(nc["positionalArgs"][0]) if nc.get("positionalArgs") else None
                label_clean = label.strip().strip("'\"") if label else None
                body = nc["trailingClosure"]["body"] if nc.get("trailingClosure") else []
                stages.extend(process_body_as_stages(body, extractor, stage_indent, report, agent_override=label_clean))
    elif len(node_calls) == 1:
        # One node {} wrapping the stages, plus other top-level statements
        # around it (a very common shape once a shared library is in play:
        # `@Library('x') _`, then a config map or a helper call, then the
        # node). The node still determines the agent; the statements outside
        # it are kept in their original order in synthetic stages rather than
        # being lumped together with the node itself - which would have
        # emitted a `node {}` block INSIDE the generated pipeline.
        node_idx = next(i for i, st in enumerate(body_stmts) if st is node_calls[0])
        nc = body_stmts[node_idx]
        before, after = body_stmts[:node_idx], body_stmts[node_idx + 1:]
        label = extractor.extract(nc["positionalArgs"][0]) if nc.get("positionalArgs") else None
        agent_text = render_agent(label)
        report.native(f"node({label or ''}) -> {agent_text.split('//')[0].strip()}")
        report.warn(
            f"{len(before) + len(after)} top-level statement(s) sit outside the node {{}} block. "
            "In Scripted these run on the flyweight executor, before/after any agent is "
            "allocated and with no workspace; Declarative has no equivalent slot, so they were "
            "put into synthetic stage(s) that DO run on the agent. Review them - anything that "
            "must not occupy an executor (or must run before agent allocation) needs a different "
            "home, e.g. an environment{} entry or a `when` condition."
        )
        stages = []
        if before:
            stages.append(make_synthetic_stage("Setup", before, extractor, stage_indent, report))
        node_body = nc["trailingClosure"]["body"] if nc.get("trailingClosure") else []
        stages.extend(process_body_as_stages(node_body, extractor, stage_indent, report))
        if after:
            stages.append(make_synthetic_stage("Cleanup", after, extractor, stage_indent, report))
    else:
        found_desc = "; ".join(brief(st) for st in body_stmts[:8])
        if len(body_stmts) > 8:
            found_desc += f"; ... ({len(body_stmts) - 8} more)"
        report.warn(
            "No single top-level node {} (or sequence of node {} blocks) wrapping the "
            "whole file was found. Defaulting to `agent any` for the whole pipeline - "
            "verify this matches how the original Scripted pipeline was actually "
            "invoked (e.g. run via a node provided externally by the job configuration). "
            f"What was found at the top level instead: {found_desc}."
        )
        agent_text = "agent any"

        # A node {} buried inside another block is the usual reason we got
        # here, and it is NOT a benign warning: the enclosing block gets
        # copied through as a step, which puts a node {} - and any stage {}
        # inside it - inside a Declarative steps {} block, which Jenkins
        # rejects. Say so explicitly rather than emitting output that merely
        # looks converted.
        for st, chain in find_buried_node_calls(body_stmts):
            wrappers = " > ".join(f"{c}{{}}" for c in chain)
            report.warn(
                f"node{{}} at line {st['line']} is nested inside {wrappers}, so it was NOT "
                "turned into an `agent` - the whole block was copied through as a step, "
                "which means the generated file has a node{} (and any stage{} inside it) "
                "in a steps{} block. That is not valid Declarative and Jenkins will reject "
                "it. Restructure by hand: hoist the node to the pipeline's `agent` (or a "
                "per-stage `agent{}`), and re-express the enclosing block as a Declarative "
                "directive where one exists (e.g. timestamps{} -> `options { timestamps() }`) "
                "or move it inside the stage's steps."
            )

        stages = process_body_as_stages(body_stmts, extractor, stage_indent, report)

    out = []
    if preamble_text:
        out.append(preamble_text)
        out.append("")
    out.append("pipeline {")
    out.append(f"{indent}{agent_text}")
    out.append(f"{indent}stages {{")
    out.append("\n".join(stages))
    out.append(f"{indent}}}")
    if post_text:
        out.append(post_text)
    out.append("}")

    if trailing_methods:
        out.append("")
        for m in trailing_methods:
            out.append(extractor.extract(m))
            report.native(f"preserved top-level function '{m['name']}' verbatim after pipeline {{}}")

    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Output syntax check
# --------------------------------------------------------------------------

def syntax_check(path: str, report: Report):
    """Re-parse the generated file with the same CONVERSION-phase Groovy parser
    used on the input, proving the output is syntactically valid Groovy
    (balanced braces, well-formed statements - i.e. that this tool did not
    corrupt anything while slicing and re-nesting source text).

    Deliberately a PARSE, not a compile (`groovyc`): compiling resolves class
    references, and a Jenkinsfile's class references only exist inside a
    running Jenkins. `@Library('x') _` fails to compile locally with "unable
    to resolve class Library for annotation", and so does every `import
    com.acme.SomethingFromTheSharedLibrary` that typically follows it. Those
    are properties of the environment, not defects in the output, and
    reporting them as errors made the tool declare low confidence on
    perfectly good conversions of exactly the files that use shared
    libraries. Declarative *structure* is validated by Jenkins's own
    declarative-linter instead - see README, "Validation".
    """
    if not open(path).read().strip():
        # Nothing was converted (the caller already warned why); an empty file
        # is not a syntax error, and handing it to the parser only produces a
        # confusing "A source must be specified".
        return
    try:
        run_ast_dump(path)
    except (RuntimeError, ValueError) as exc:
        report.warn(
            "the generated output did not parse as valid Groovy - this is a bug in the "
            f"conversion, do not use the output as-is:\n{exc}"
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="Path to the Scripted Jenkinsfile to convert")
    ap.add_argument("output", help="Path to write the Declarative Jenkinsfile to")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        print(f"ERROR: no such file: {args.input}", file=sys.stderr)
        sys.exit(1)

    report = Report()
    try:
        dump = run_ast_dump(args.input)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    with open(args.input) as f:
        source = f.read()
    extractor = Extractor(source)

    output_text = convert(dump, extractor, report)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as f:
        f.write(output_text)

    syntax_check(args.output, report)

    print(report.render())


if __name__ == "__main__":
    main()
