// ast_dump.groovy
//
// Parses a Jenkinsfile using Groovy's OWN compiler (CONVERSION phase - parsed
// into a real AST, not executed, not semantically resolved) and emits a JSON
// description of its top-level structure. This exists so scripted_to_declarative.py
// never has to guess where one statement ends and another begins, or misparse a
// brace inside a string/comment - that recognition is done by the real Groovy
// parser, which cannot be wrong about Groovy syntax by definition.
//
// Design choice that matters: every node in the JSON carries its exact source
// line/column span. The Python side always extracts the ORIGINAL SOURCE TEXT
// for a span rather than regenerating Groovy from the AST - so anything this
// tool copies into the output is guaranteed byte-identical to what a human
// wrote, never a reinterpretation of it.
//
// Usage: groovy ast_dump.groovy <path-to-jenkinsfile>   (prints JSON to stdout)

import org.codehaus.groovy.ast.builder.AstBuilder
import org.codehaus.groovy.control.CompilePhase
import org.codehaus.groovy.control.MultipleCompilationErrorsException
import org.codehaus.groovy.ast.stmt.*
import org.codehaus.groovy.ast.expr.*
import org.codehaus.groovy.ast.ImportNode
import org.codehaus.groovy.ast.MethodNode
import groovy.json.JsonOutput

if (args.length < 1) {
    System.err.println("usage: groovy ast_dump.groovy <jenkinsfile>")
    System.exit(2)
}

def file = new File(args[0])
def src = file.text

def span(node) {
    [line: node.lineNumber, col: node.columnNumber, lastLine: node.lastLineNumber, lastCol: node.lastColumnNumber]
}

// Groovy's AST occasionally wraps a block's real statement list in an extra
// nested BlockStatement (observed for TryCatchStatement.finallyStatement;
// guarded against everywhere for safety). Flatten any such nesting so callers
// always get the real leaf statements, never a single wrapper block.
def flattenStatements(stmtList) {
    def out = []
    stmtList.each { s ->
        if (s instanceof BlockStatement) {
            out.addAll(flattenStatements(s.statements))
        } else {
            out << s
        }
    }
    return out
}

def statementsOf(stmt) {
    if (stmt == null) return []
    if (stmt instanceof BlockStatement) return flattenStatements(stmt.statements)
    return [stmt]
}

def describeExpr(e) {
    if (e instanceof ClosureExpression) {
        def stmts = statementsOf(e.code)
        return [type: 'closure', body: stmts.collect { describeStmt(it) }] + span(e)
    }
    return [type: 'other'] + span(e)
}

def describeNamedArgs(namedArgListExpr) {
    namedArgListExpr.mapEntryExpressions.collect { me ->
        def keyText
        try { keyText = me.keyExpression.text } catch (ignored) { keyText = null }
        def v = me.valueExpression
        if (v instanceof ClosureExpression) {
            [key: keyText, kind: 'closure', closure: describeExpr(v)]
        } else {
            [key: keyText, kind: 'other'] + span(v)
        }
    }
}

def describeCall(call, stmtSpan) {
    def name
    try { name = call.methodAsString } catch (ignored) { name = null }
    def result = [kind: 'call', name: name] + stmtSpan
    def args = call.arguments
    def positional = []   // non-named, non-trailing-closure args, as source spans
    def namedArgs = null
    def trailingClosure = null

    List<Expression> argExprs = []
    if (args instanceof ArgumentListExpression) {
        argExprs = args.expressions
    } else if (args instanceof TupleExpression) {
        argExprs = args.expressions
    }

    argExprs.each { a ->
        if (a instanceof NamedArgumentListExpression) {
            namedArgs = describeNamedArgs(a)
        } else if (a instanceof MapExpression) {
            // Some call shapes wrap named args as a MapExpression instead of
            // NamedArgumentListExpression depending on how they were written.
            namedArgs = a.mapEntryExpressions.collect { me ->
                def keyText
                try { keyText = me.keyExpression.text } catch (ignored) { keyText = null }
                def v = me.valueExpression
                if (v instanceof ClosureExpression) {
                    [key: keyText, kind: 'closure', closure: describeExpr(v)]
                } else {
                    [key: keyText, kind: 'other'] + span(v)
                }
            }
        } else if (a instanceof ClosureExpression) {
            trailingClosure = describeExpr(a)
        } else {
            positional << (span(a) + [text: null])
        }
    }
    result.positionalArgs = positional
    result.namedArgs = namedArgs
    result.trailingClosure = trailingClosure
    return result
}

def describeStmt(stmt) {
    def s = span(stmt)
    if (stmt instanceof ExpressionStatement) {
        def e = stmt.expression
        if (e instanceof MethodCallExpression) {
            return describeCall(e, s)
        } else if (e instanceof DeclarationExpression) {
            return [kind: 'declaration'] + s
        } else if (e instanceof BinaryExpression && e.operation?.text == '=') {
            return [kind: 'assignment'] + s
        } else {
            return [kind: 'other_expr'] + s
        }
    } else if (stmt instanceof IfStatement) {
        return [kind: 'if'] + s
    } else if (stmt instanceof ForStatement) {
        return [kind: 'for'] + s
    } else if (stmt instanceof WhileStatement) {
        return [kind: 'while'] + s
    } else if (stmt instanceof TryCatchStatement) {
        def result = [kind: 'try'] + s
        result.tryBody = statementsOf(stmt.tryStatement).collect { describeStmt(it) }
        result.catches = stmt.catchStatements.collect { c ->
            [exceptionType: c.exceptionType?.name, body: statementsOf(c.code).collect { describeStmt(it) }] + span(c)
        }
        def fStmts = statementsOf(stmt.finallyStatement)
        if (fStmts) {
            result.finallyBody = fStmts.collect { describeStmt(it) }
        }
        return result
    } else if (stmt instanceof SwitchStatement) {
        return [kind: 'switch'] + s
    } else if (stmt instanceof ReturnStatement) {
        return [kind: 'return'] + s
    } else {
        return [kind: 'other'] + s
    }
}

try {
    def nodes = new AstBuilder().buildFromString(CompilePhase.CONVERSION, false, src)
    def block = nodes.find { it instanceof BlockStatement }
    def classNode = nodes.find { it instanceof org.codehaus.groovy.ast.ClassNode }

    def topStatements = block ? statementsOf(block).collect { describeStmt(it) } : []

    // Top-level `def name(...) { ... }` declarations compile as methods on the
    // generated script class rather than appearing in the statement block -
    // report their spans separately so Python can preserve them verbatim
    // outside the pipeline {} block.
    def methodDecls = []
    if (classNode) {
        classNode.methods.each { MethodNode m ->
            if (m.lineNumber > 0 && m.name != 'main' && m.name != 'run') {
                methodDecls << [name: m.name] + span(m)
            }
        }
    }

    println JsonOutput.toJson([
        ok: true,
        topLevel: topStatements,
        methodDecls: methodDecls,
    ])
} catch (MultipleCompilationErrorsException e) {
    println JsonOutput.toJson([ok: false, error: e.message])
    System.exit(1)
} catch (Exception e) {
    println JsonOutput.toJson([ok: false, error: "${e.class.name}: ${e.message}"])
    System.exit(1)
}
