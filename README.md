# scripted_to_declarative.py

Converts a Scripted Jenkinsfile into Declarative Pipeline syntax, grounded in
Groovy's own parser and Jenkins's documented Declarative Pipeline grammar.

## Requirements

- Python 3.9+
- **Groovy on PATH** (e.g. `apt-get install groovy`). This is a hard
  requirement, not optional - see "Why a real parser" below.

## Usage

```
python3 scripted_to_declarative.py path/to/Jenkinsfile path/to/Jenkinsfile.declarative
```

Prints a report: what was natively mapped, what fell back to `script {}`,
warnings, and a confidence level.

## Why a real parser, not regex

Jenkinsfiles are arbitrary Groovy: nested braces inside strings, GString
interpolation (`"${...}"`), comments, multi-line method chains, and
paren-less "command" calls (`sh 'x'`) all break naive brace-counting or
regex. `ast_dump.groovy` parses the input with Groovy's own compiler
(`CONVERSION` phase - real AST, not executed) so structural recognition
(where one statement ends, what a method call's name and arguments are)
comes from Groovy itself, which cannot be wrong about Groovy syntax.
`scripted_to_declarative.py` only decides what recognized structure maps to;
every piece of code it emits is sliced out of the **original source** by
exact line/column span (never regenerated from the AST), so the tool cannot
subtly rewrite - and therefore cannot subtly break - code it copies, whether
or not it fully "understands" that code.

## What it maps natively

- A single top-level `node` / `node('label')` → top-level `agent`
- Multiple sequential top-level `node('x') { ... }` blocks → `agent none` +
  per-stage `agent { label 'x' }`
- `stage('Name') { ... }` → `stage('Name') { steps { ... } }`
- A stage body that is *entirely* step calls (see classification rule below)
  → copied verbatim into `steps {}`
- A stage body whose only content is `parallel(branch: { ... }, ...)` →
  `parallel { stage('branch') { steps {...} } ... }`, recursing the same
  classification into each branch
- A top-level `try { <stages> } catch (e) { ... } finally { ... }` wrapping
  the whole pipeline → stages unwrapped normally, `catch` → best-effort
  `post.failure`, `finally` → best-effort `post.always` (flagged as
  best-effort in the report: try/catch/finally and `post{}` conditions are
  not semantically identical)
- Top-level `import` statements, `@Library` annotations, and top-level `def`
  helper functions → preserved verbatim in their original relative position
  (before/after the `pipeline {}` block), since Declarative Pipeline
  supports both. This covers both spellings of the shared-library directive:
  `@Library('x') import com.foo.Bar` (an annotated import, which lives on
  Groovy's ModuleNode and is simply copied through with the leading lines)
  and the far more common `@Library('x') _`, which Groovy parses as a real
  top-level *statement* - an annotated declaration of the throwaway variable
  `_` - and which is therefore recognised explicitly as file preamble and
  hoisted above `pipeline {}`. Everything from line 1 down to the first
  statement that is *not* preamble is copied verbatim, so a shebang, an
  `import` sitting between the `@Library` line and the pipeline body, and
  comments all survive
- A single `node {}` with other statements around it → the `node` still
  supplies the `agent`, and the statements outside it become synthetic
  `Setup`/`Cleanup` stages (flagged: in Scripted they ran on the flyweight
  executor with no workspace, whereas the generated stages run on the agent)

## The classification rule (what needs `script {}`)

Per Jenkins's own Declarative parser (confirmed via the "Expected a step"
validation error - see JENKINS-45829 and the Pipeline syntax docs), a
`steps {}` block may contain **only step invocations** - not assignments,
not `if`/`for`/`while`/`try`/`switch`, not bare expressions. So each
top-level statement of a stage body is classified as:

- **plain** (copy verbatim into `steps {}`): the statement is a method call,
  with or without a trailing closure, regardless of which method it calls -
  built-in step, plugin step, or a custom function you defined. Declarative
  does not validate step *names*, only statement *shape*.
- **needs `script {}`**: an assignment (`x = ...`, `def x = ...`), a
  control-flow statement (`if`/`for`/`while`/`switch`/`try`), a bare
  non-call expression, or anything else.

Critically, this classification is **not recursive into a step's own
closure argument**. `dir('x') { ... }`, `withEnv([...]) { ... }`,
`timeout(time: 5) { ... }`, `retry(3) { ... }`, `catchError { ... }` - these
are steps that take a closure, and whatever Groovy is inside that closure
(loops, conditionals, assignments) is ordinary CPS Groovy in *both*
Scripted and Declarative Pipeline. Declarative's "must be a step" rule only
applies to what `steps {}` directly enumerates, not to what's nested inside
one of those steps. So a `timeout(time: 5) { if (x) { sh 'y' } }` statement
is copied through as a single plain statement, `if` and all, correctly and
without needing internal `script{}` wrapping.

When a stage body mixes plain and non-plain statements, the **whole stage
body** is wrapped in one `script {}` block (all-or-nothing) rather than
trying to interleave fine-grained `script{}` blocks around individual
statements - simpler, and exactly as correct, since a stage's steps run
sequentially either way.

## The Declarative grammar dictionary

`declarative_grammar.json` is a machine-readable dictionary of the
identifiers that are legal in a Declarative Pipeline. It exists so that the
claims this tool makes about Declarative syntax are auditable **data**
rather than knowledge baked into prose and code comments.

It follows the same rule as the converter: nothing in it is written from
memory. Every identifier was read off Jenkins's own documentation (the
source URLs and the verification date are recorded in the file's `_meta`
block), and anything that could not be confirmed there is listed under
`unverified` instead of being quietly included - currently the `libraries`
directive and the `isRestartedRun` `when` condition, both widely used in the
wild but absent from the pages checked.

The dictionary is explicit about which sets are **closed** and which are
**open**:

- **Closed** (defined by the pipeline-model-definition grammar, so a name
  outside the set is an error): `sections`, `stage_directives`, `agent`
  types and options, `post_conditions`, `options` (pipeline and stage),
  `parameters`, `triggers`, `when` conditions, `matrix`, `input`, `tools`.
- **Open**: `steps`. Any plugin can contribute a step, and a call to a
  function you wrote yourself is equally legal - which is exactly why this
  converter classifies statement *shape* and never step *names*. The
  `steps.core` entries are the steps shipped by the standard Pipeline
  plugins, provided for reference and tooling hints; **do not** use them as
  a whitelist to reject an unrecognised step.

Each step entry carries a `block` flag marking whether it takes a closure
body. That is the distinction that matters most when converting Scripted to
Declarative: a block step (`dir`, `withEnv`, `timeout`, `retry`,
`catchError`, ...) is copied through as a single statement, and the Groovy
inside its closure is *not* subject to the "must be a step" rule - see "The
classification rule" above.

The file also records the structural rules the converter relies on (a
`stage` must have exactly one of `steps`/`stages`/`parallel`/`matrix`;
`@Library` is a compile-time annotation that belongs above the `pipeline`
block), and the documented `@Library` annotation and `library` step forms.

It is currently reference data - the converter does not read it at runtime.

## Known limitations (things this deliberately does NOT attempt)

- **Dynamic/conditional stage generation** (`if`/`for`/`while` used at a
  point where a sequence of `stage()` calls was expected, e.g. looping over
  a list to generate one stage per item) has no direct Declarative
  equivalent - `matrix{}` covers parameterized stages over a fixed axis set,
  `when{}` covers per-stage conditions, neither is a drop-in replacement for
  arbitrary Groovy control flow generating stages. This tool does not guess
  at a `matrix{}`/`when{}` restructuring; it bundles the affected code into
  a `script{}`-wrapped synthetic stage and flags it loudly for manual
  review, exactly the same way the Job DSL converter refuses to guess an
  unknown method rather than inventing one.
- **`properties([parameters([...]), pipelineTriggers([...])])`** (the common
  scripted idiom for declaring parameters/triggers/build-discarder) is
  **not** mapped to native `parameters{}`/`triggers{}`/`options{}`.
  `ast_dump.groovy` does not currently describe the internal structure of
  list-literal arguments, and approximating that from raw text risked
  silently mis-mapping a parameter type - so this call is left as an
  ordinary (non-step) statement, which the classifier correctly routes into
  a `script{}`-wrapped stage, with a note in the report to migrate it by
  hand. Extending `ast_dump.groovy` to describe `ListExpression` entries the
  same way it already describes named-argument maps would let this be done
  properly; that's the natural next increment, not a shortcut taken now.
- **`tool '...'`** usage and the declarative `tools {}` block: not mapped.
  A `tool` call assigning a path into a variable for later use in `sh`
  doesn't have a clean 1:1 `tools{}` equivalent without knowing how the
  variable is used downstream. `tool` calls pass through fine as plain
  steps either way, so nothing is lost - just not "upgraded" to `tools{}`.
- **`input(...)`**: left as a plain step call rather than promoted to the
  stage-level `input {}` directive (a different construct with different
  semantics: it pauses before the stage's steps run, not at an arbitrary
  point inside them). Leaving it as a step is always correct; promoting it
  automatically risked changing behavior.
- **Double-checkout risk**: Declarative's `agent` directive automatically
  checks out the configured SCM before the first stage runs; a Scripted
  `node {}` does not. If the original file has an explicit `checkout scm` (or
  equivalent) near the top, converting it as-is can mean it runs twice. This
  tool does not try to detect or remove that automatically (removing it
  could be wrong depending on `skipDefaultCheckout` and other options) -
  review the first stage's `checkout`/`git` usage by hand after converting.
- Comments inside a stage body that ends up `script{}`-wrapped, and any
  code copied verbatim in general, keep their **original indentation** from
  the source file rather than being re-indented to match their new nesting
  depth. This is deliberate: re-indenting risks corrupting the content of a
  multi-line string (e.g. an embedded shell script) if done carelessly.
  Cosmetic only - run a Groovy formatter afterward if you want it tidied.

## Validation

`scripted_to_declarative.py` re-parses its own output automatically, with
the same Groovy parser (CONVERSION phase, via `ast_dump.groovy`) it used to
read the input - this proves the output is syntactically valid Groovy, i.e.
that the conversion did not corrupt anything while slicing and re-nesting
source text. It does **not** prove the output is a *valid Declarative
Pipeline* (a syntactically fine Groovy file can still be missing a required
section, or misuse a directive) or that it's behaviorally equivalent to the
original Scripted pipeline.

The check is deliberately a parse and not a `groovyc` compile: compiling
resolves class references, and a Jenkinsfile's class references only exist
inside a running Jenkins. `@Library('x') _` fails to compile locally with
"unable to resolve class Library for annotation", as does every `import
com.acme.SomethingFromTheSharedLibrary` that usually follows it - so a
compile-based check reported errors that were properties of the local
environment rather than defects in the output, on exactly the files that
use shared libraries.

For real validation, use Jenkins's own Declarative Linter, against a Jenkins
instance with the versions of Jenkins/plugins you actually run:

```
# via the Jenkins CLI
java -jar jenkins-cli.jar -s https://your-jenkins declarative-linter < Jenkinsfile.declarative

# via HTTP, if anonymous read access is enabled
curl -X POST -F "jenkinsfile=<Jenkinsfile.declarative" https://your-jenkins/pipeline-model-converter/validate
```

This actually validates the Declarative structure (required sections,
correct directive usage) without running a real build. It's the closest
equivalent for this tool to what the Job DSL Test Harness is for
`convert.py` - run it before trusting a converted Jenkinsfile, and treat a
clean parse check alone as "parses," not "correct."
# jenkins-scripted-to-declarative
