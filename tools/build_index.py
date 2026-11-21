"""Build the selection index from per-test labels and the pinned source tree.

Inputs
  labels.pkl.gz : {"cov": {relpath: {line: [test numbers]}},
                   "meta": {test: {"modules": [...], "opened": [...], ...}},
                   "tests": tests.json["tests"]}
  repo dir      : the pinned checkout (sqlglot/, tests/, ...)
Output
  index.pkl.gz  : everything the runtime selector needs; test sets are int bitmasks.

Usage: python build_index.py <labels.pkl.gz> <repo_dir> <index.pkl.gz>
"""
import ast
import gzip
import os
import pickle
import sys

# per-line kind codes for Python files (old tree)
K_NONE = 0        # blank / comment / unknown
K_MODULE = 1      # module-level statement line
K_CLASS = 2       # class-body statement line (not inside a def)
K_FUNC = 3        # inside a function body
K_DEF = 4         # def/decorator header line
K_DOC = 5         # docstring line
K_TC = 6          # inside an `if TYPE_CHECKING:` body (never executed)


class Sets:
    def __init__(self):
        self.table = []
        self.ids = {}

    def add(self, mask):
        i = self.ids.get(mask)
        if i is None:
            i = len(self.table)
            self.table.append(mask)
            self.ids[mask] = i
        return i


def mask_of(tests):
    m = 0
    for t in tests:
        m |= 1 << t
    return m


def module_name(rel):
    if not rel.endswith(".py"):
        return None
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def analyze_python(src):
    """Return (line_kind list[1..n], stmt_spans {line:(s,e)} for multi-line simple stmts,
    funcs [(start,end,body_start,qualname)], ok)."""
    n = src.count("\n") + 1
    kind = [K_NONE] * (n + 2)
    spans = {}
    funcs = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return kind, spans, funcs, {}, False

    def mark(lo, hi, k):
        for ln in range(lo, hi + 1):
            if kind[ln] == K_NONE:
                kind[ln] = k

    def visit_body(body, ctx, qual):
        for st in body:
            s, e = st.lineno, st.end_lineno
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{qual}.{st.name}" if qual else st.name
                dstart = min([d.lineno for d in st.decorator_list] + [s])
                # header lines: decorators through the def line (up to first body stmt)
                body_start = st.body[0].lineno if st.body else st.end_lineno
                hdr_end = body_start - 1
                mark(dstart, max(hdr_end, s), K_DEF)
                funcs.append((dstart, e, body_start, name))
                visit_body(st.body, K_FUNC, name)
                mark(dstart, e, K_FUNC)
            elif isinstance(st, ast.ClassDef):
                name = f"{qual}.{st.name}" if qual else st.name
                dstart = min([d.lineno for d in st.decorator_list] + [s])
                body_start = st.body[0].lineno if st.body else st.end_lineno
                mark(dstart, body_start - 1, ctx if ctx == K_FUNC else K_CLASS)
                visit_body(st.body, K_FUNC if ctx == K_FUNC else K_CLASS, name)
                mark(dstart, e, K_FUNC if ctx == K_FUNC else K_CLASS)
            elif isinstance(st, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar, ast.Match)):
                # compound statement: header line(s) take the context kind; recurse into bodies
                first_body = min([b[0].lineno for b in (st.body, getattr(st, "orelse", None), getattr(st, "finalbody", None)) if b]
                                 + [h.lineno for h in (getattr(st, "handlers", None) or [])]
                                 + [c.pattern.lineno for c in (getattr(st, "cases", None) or [])] + [e + 1])
                hdr_end = max(s, first_body - 1)
                mark(s, hdr_end, ctx)
                if hdr_end > s:
                    for ln in range(s, hdr_end + 1):
                        spans[ln] = (s, hdr_end)
                tc = isinstance(st, ast.If) and (
                    (isinstance(st.test, ast.Name) and st.test.id == "TYPE_CHECKING")
                    or (isinstance(st.test, ast.Attribute) and st.test.attr == "TYPE_CHECKING"))
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(st, field, None)
                    if sub:
                        if tc and field == "body":
                            mark(sub[0].lineno, sub[-1].end_lineno, K_TC)
                            continue
                        visit_body(sub, ctx, qual)
                for h in getattr(st, "handlers", []) or []:
                    mark(h.lineno, h.lineno, ctx)
                    visit_body(h.body, ctx, qual)
                for c in getattr(st, "cases", []) or []:
                    mark(c.pattern.lineno, c.pattern.lineno, ctx)
                    visit_body(c.body, ctx, qual)
                mark(s, e, ctx)  # else:/elif lines etc.
            else:
                if (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
                        and isinstance(st.value.value, str)):
                    mark(s, e, K_DOC)
                else:
                    mark(s, e, ctx)
                    if e > s:
                        for ln in range(s, e + 1):
                            spans[ln] = (s, e)
                        # multi-line lambdas: their body runs when called, not when the statement runs
                        for node in ast.walk(st):
                            if isinstance(node, ast.Lambda) and node.end_lineno > node.lineno:
                                for ln in range(node.lineno, node.end_lineno + 1):
                                    cur = lambdas.get(ln)
                                    if cur is None or (node.end_lineno - node.lineno) < (cur[1] - cur[0]):
                                        lambdas[ln] = (node.lineno, node.end_lineno)

    lambdas = {}
    visit_body(tree.body, K_MODULE, "")
    return kind, spans, funcs, lambdas, True


def main(labels_path, repo, out_path):
    with gzip.open(labels_path, "rb") as f:
        L = pickle.load(f)
    cov, meta, tests = L["cov"], L["meta"], L["tests"]
    ntests = len(tests)
    sets = Sets()
    sets.add(0)

    # module -> file path, for importer sets
    py_files = {}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), repo).replace("\\", "/")
            if fn.endswith(".py"):
                py_files[rel] = module_name(rel)
    mod_to_file = {m: f for f, m in py_files.items()}

    importers = {}   # file -> mask
    readers = {}     # path (file or dir) -> mask
    for t, m in meta.items():
        for mod in m.get("modules", []):
            f = mod_to_file.get(mod)
            if f:
                importers[f] = importers.get(f, 0) | (1 << t)
        for p in m.get("opened", []):
            if "__pycache__" in p:
                continue
            readers[p] = readers.get(p, 0) | (1 << t)

    files = {}
    all_mask = mask_of(t["number"] for t in tests)
    for rel in sorted(set(cov) | set(importers) | set(p for p in readers if "." in os.path.basename(p))):
        entry = {}
        lines = cov.get(rel)
        anym = 0
        if lines:
            lm = {}
            for ln, ts in lines.items():
                mk = mask_of(ts)
                anym |= mk
                lm[ln] = sets.add(mk)
            entry["lines"] = lm
        entry["any"] = sets.add(anym)
        # importing a module executes its module-level lines, and child processes only show up in
        # coverage, so "touched any line" is the complete importer set; sys.modules adds the parent view
        entry["importers"] = sets.add(importers.get(rel, 0) | anym)
        entry["readers"] = sets.add(readers.get(rel, 0))
        path = os.path.join(repo, rel)
        if rel.endswith(".py") and os.path.isfile(path):
            src = open(path, encoding="utf-8", newline="").read()
            kind, spans, funcs, lambdas, ok = analyze_python(src)
            entry["kind"] = bytes(kind)
            entry["spans"] = spans
            entry["lambdas"] = lambdas
            fl = []
            for (s, e, bs, name) in funcs:
                mk = 0
                if lines:
                    for ln in range(bs, e + 1):
                        ts = lines.get(ln)
                        if ts:
                            mk |= mask_of(ts)
                fl.append((s, e, bs, sets.add(mk), name))
            entry["funcs"] = fl
            entry["ok"] = ok
        files[rel] = entry

    dir_readers = {p: sets.add(m) for p, m in readers.items() if os.path.isdir(os.path.join(repo, p))}

    # ---- static def-use for class attributes: NAME -> tests that executed a line reading NAME
    class_attr_names = set()
    class_assign_lines = {}   # rel -> {line: [names]}
    uses = {}                 # name -> set((rel, line))
    trees = {}
    for rel in py_files:
        path = os.path.join(repo, rel)
        try:
            trees[rel] = ast.parse(open(path, encoding="utf-8", newline="").read())
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                cal = class_assign_lines.setdefault(rel, {})
                for st in node.body:
                    names = []
                    if isinstance(st, ast.Assign):
                        for t in st.targets:
                            names += [n.id for n in ast.walk(t) if isinstance(n, ast.Name)]
                    elif isinstance(st, (ast.AnnAssign, ast.AugAssign)) and isinstance(st.target, ast.Name):
                        names = [st.target.id]
                    if names:
                        class_attr_names.update(names)
                        for ln in range(st.lineno, st.end_lineno + 1):
                            cal[ln] = names
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in class_attr_names:
                uses.setdefault(node.attr, set()).add((rel, node.lineno))
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in class_attr_names:
                uses.setdefault(node.id, set()).add((rel, node.lineno))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in class_attr_names:
                uses.setdefault(node.value, set()).add((rel, node.lineno))  # getattr(x, "NAME")-style access
    attr_uses = {}
    for name, locs in uses.items():
        mk = 0
        for rel, ln in locs:
            fe = files.get(rel)
            if not fe or "lines" not in fe:
                continue
            span = fe.get("spans", {}).get(ln, (ln, ln))
            for x in range(span[0], span[1] + 1):
                sid = fe["lines"].get(x)
                if sid is not None:
                    mk |= sets.table[sid]
        attr_uses[name] = sets.add(mk)
    for rel, cal in class_assign_lines.items():
        if rel in files:
            files[rel]["class_assign"] = cal
    index = {
        "attr_uses": attr_uses,
        "ntests": ntests,
        "tests": {t["number"]: (t["id"], round(t["seconds"] * 1000)) for t in tests},
        "all": sets.add(all_mask),
        "files": files,
        "dir_readers": dir_readers,
        "sets": sets.table,
    }
    with gzip.open(out_path, "wb", compresslevel=6) as f:
        pickle.dump(index, f, protocol=5)
    print(f"index: files={len(files)} sets={len(sets.table)} size={os.path.getsize(out_path)/1e6:.1f} MB")
    # a few sanity prints
    for rel in ("sqlglot/parsers/dremio.py", "sqlglot/optimizer/scope.py", "tests/fixtures/optimizer/simplify.sql"):
        e = files.get(rel)
        if e:
            print(rel, "importers", bin(sets.table[e["importers"]]).count("1"), "readers",
                  bin(sets.table[e["readers"]]).count("1"), "any", bin(sets.table[e["any"]]).count("1"),
                  "lines", len(e.get("lines", {})))


if __name__ == "__main__":
    main(*sys.argv[1:4])
