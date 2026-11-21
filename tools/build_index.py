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
    cls_lines = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return kind, spans, funcs, {}, cls_lines, {}, False

    def mark(lo, hi, k, qual=None):
        for ln in range(lo, hi + 1):
            if kind[ln] == K_NONE:
                kind[ln] = k
            if k == K_CLASS and qual:
                cls_lines.setdefault(ln, qual)

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
                mark(dstart, body_start - 1, ctx if ctx == K_FUNC else K_CLASS, qual)
                visit_body(st.body, K_FUNC if ctx == K_FUNC else K_CLASS, name)
                mark(dstart, e, K_FUNC if ctx == K_FUNC else K_CLASS, name)
            elif isinstance(st, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar, ast.Match)):
                # compound statement: header line(s) take the context kind; recurse into bodies
                first_body = min([b[0].lineno for b in (st.body, getattr(st, "orelse", None), getattr(st, "finalbody", None)) if b]
                                 + [h.lineno for h in (getattr(st, "handlers", None) or [])]
                                 + [c.pattern.lineno for c in (getattr(st, "cases", None) or [])] + [e + 1])
                hdr_end = max(s, first_body - 1)
                mark(s, hdr_end, ctx, qual)
                if hdr_end > s:
                    for ln in range(s, hdr_end + 1):
                        spans[ln] = (s, hdr_end)
                for ln in range(s, e + 1):
                    compound[ln] = (s, e)  # innermost wins: inner statements are visited later
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
                    mark(c.pattern.lineno, c.pattern.lineno, ctx, qual)
                    visit_body(c.body, ctx, qual)
                mark(s, e, ctx, qual)  # else:/elif lines etc.
            else:
                if (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
                        and isinstance(st.value.value, str)):
                    mark(s, e, K_DOC)
                else:
                    mark(s, e, ctx, qual)
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
    compound = {}
    visit_body(tree.body, K_MODULE, "")
    return kind, spans, funcs, lambdas, cls_lines, compound, True


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
            kind, spans, funcs, lambdas, cls_lines, compound, ok = analyze_python(src)
            entry["kind"] = bytes(kind)
            entry["spans"] = spans
            entry["lambdas"] = lambdas
            entry["compound"] = compound
            mod = module_name(rel)
            entry["cls_lines"] = {ln: f"{mod}.{q}" for ln, q in cls_lines.items()}
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
    def assigned_names(st):
        if isinstance(st, ast.Assign):
            return [n.id for t in st.targets for n in ast.walk(t) if isinstance(n, ast.Name)]
        if isinstance(st, (ast.AnnAssign, ast.AugAssign)) and isinstance(st.target, ast.Name):
            return [st.target.id]
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and not st.decorator_list:
            return [st.name]
        return []

    mod_assign_lines = {}     # rel -> {line: [names]} for module-level assignments / plain defs (header lines)
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                cal = class_assign_lines.setdefault(rel, {})
                for st in node.body:
                    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        class_attr_names.add(st.name)  # methods are read by name too (self.m, super().m)
                        continue
                    names = assigned_names(st)
                    if names:
                        class_attr_names.update(names)
                        for ln in range(st.lineno, st.end_lineno + 1):
                            cal[ln] = names
        mal = mod_assign_lines.setdefault(rel, {})

        def module_level(body):
            for st in body:
                if isinstance(st, (ast.If, ast.Try, ast.TryStar, ast.With)):
                    for field in ("body", "orelse", "finalbody"):
                        sub = getattr(st, field, None)
                        if sub:
                            module_level(sub)
                    for h in getattr(st, "handlers", []) or []:
                        module_level(h.body)
                    continue
                names = assigned_names(st)
                if names:
                    class_attr_names.update(names)
                    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        end = st.body[0].lineno - 1 if st.body else st.end_lineno
                    else:
                        end = st.end_lineno
                    for ln in range(st.lineno, end + 1):
                        mal[ln] = names

        module_level(tree.body)
    # Metaclass derivations `klass.A = f(klass.B)` define attributes too; track their targets.
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute) \
                    and isinstance(node.targets[0].value, ast.Name) and node.targets[0].value.id in ("klass", "cls"):
                class_attr_names.add(node.targets[0].attr)

    # Three kinds of use sites:
    #   uses     : unqualified reads (self.NAME, dialect.NAME, klass.NAME, bare NAME in functions)
    #   quses    : reads qualified by a class-like name (MySQLParser.NAME, exp.Column.NAME) and bare NAME
    #              reads inside a class body (which resolve to that class's own attribute)
    #   test_uses: any read located in a test file
    # Derivations: an assignment at class/module level (or `klass.A = ...` in a metaclass) whose right-hand
    # side reads tracked names. Such a line runs once at import; what matters is where its *result* is
    # read, so the line is replaced by the uses of the attributes it defines.
    quses = {}
    test_uses = {}
    deriv = {}  # (rel, line) -> (class simple name or None, [derived attribute names])
    for rel, tree in trees.items():
        def refs_of(expr):
            out = set()
            for n in ast.walk(expr):
                if isinstance(n, ast.Attribute):
                    out.add(n.attr)
                elif isinstance(n, ast.Name):
                    out.add(n.id)
            return out & class_attr_names

        def record_deriv(st, cls):
            if not isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)) or getattr(st, "value", None) is None:
                return
            tnames = assigned_names(st)
            if not tnames or not all(t in class_attr_names for t in tnames):
                return
            if refs_of(st.value):
                for ln in range(st.lineno, st.end_lineno + 1):
                    deriv[(rel, ln)] = (cls, tnames)

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for st in node.body:
                    record_deriv(st, node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for st in ast.walk(node):
                    if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Attribute) \
                            and isinstance(st.targets[0].value, ast.Name) and st.targets[0].value.id in ("klass", "cls") \
                            and refs_of(st.value):
                        for ln in range(st.lineno, st.end_lineno + 1):
                            deriv[(rel, ln)] = (None, [st.targets[0].attr])

        def module_level_deriv(body):
            for st in body:
                if isinstance(st, (ast.If, ast.Try, ast.TryStar, ast.With)):
                    for field in ("body", "orelse", "finalbody"):
                        sub = getattr(st, field, None)
                        if sub:
                            module_level_deriv(sub)
                    for h in getattr(st, "handlers", []) or []:
                        module_level_deriv(h.body)
                else:
                    record_deriv(st, None)

        module_level_deriv(tree.body)

    for rel, tree in trees.items():
        class_level_nodes = {}  # id(node) -> class simple name, for Name loads at class level
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for st in node.body:
                    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        continue
                    for sub in ast.walk(st):
                        if isinstance(sub, ast.Name):
                            class_level_nodes[id(sub)] = node.name
        for node in ast.walk(tree):
            # reads of every identifier are tracked, so "no reader anywhere" is a provable fact
            name = None
            qual_cls = None
            if isinstance(node, ast.Attribute):
                name = node.attr
                base = node.value
                base_name = base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else None)
                if base_name and base_name[:1].isupper() and not base_name.isupper():
                    qual_cls = base_name  # CamelCase: a class
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
                qual_cls = class_level_nodes.get(id(node))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                name = node.value  # getattr(x, "NAME")-style access
            elif isinstance(node, ast.ImportFrom):
                # `from m import NAME` reads NAME when the importing module loads
                for alias in node.names:
                    loc = (rel, node.lineno)
                    uses.setdefault(alias.name, set()).add(loc)
                    if rel.startswith("tests/"):
                        test_uses.setdefault(alias.name, set()).add(loc)
                continue
            if name is None:
                continue
            loc = (rel, node.lineno)
            if rel.startswith("tests/"):
                test_uses.setdefault(name, set()).add(loc)
            if qual_cls:
                quses.setdefault((qual_cls, name), set()).add(loc)
            else:
                uses.setdefault(name, set()).add(loc)

    def line_mask(rel, ln):
        fe = files.get(rel)
        if not fe or "lines" not in fe:
            return 0
        mk = 0
        span = fe.get("spans", {}).get(ln, (ln, ln))
        for x in range(span[0], span[1] + 1):
            sid = fe["lines"].get(x)
            if sid is not None:
                mk |= sets.table[sid]
        return mk

    quses_by_name = {}
    for (cls, name), locs in quses.items():
        quses_by_name.setdefault(name, set()).update(locs)

    def reads_of(name, cls):
        locs = set(uses.get(name, ()))
        locs |= quses.get((cls, name), set()) if cls else quses_by_name.get(name, set())
        locs |= test_uses.get(name, set())
        return locs

    def expand(locs, visited):
        mk = 0
        for loc in locs:
            d = deriv.get(loc)
            if d is None:
                mk |= line_mask(*loc)
                continue
            dcls, targets = d
            for t in targets:
                key = (t, dcls)
                if key in visited:
                    continue
                visited.add(key)
                mk |= expand(reads_of(t, dcls), visited)
        return mk

    attr_uses = {name: sets.add(expand(locs, {(name, None)})) for name, locs in uses.items()}
    quses_idx = {(cls, name): sets.add(expand(locs, {(name, cls)})) for (cls, name), locs in quses.items()}
    attr_uses_all = {}
    for name in set(uses) | set(quses_by_name) | set(test_uses):
        attr_uses_all[name] = sets.add(expand(reads_of(name, None), {(name, None)}))
    for name in class_attr_names:
        attr_uses_all.setdefault(name, sets.add(0))  # tracked but never read: distinguishable from unknown
    test_uses_idx = {name: sets.add(expand(locs, {(name, None)})) for name, locs in test_uses.items()}
    print(f"derivation lines: {len(deriv)}")
    for rel, cal in class_assign_lines.items():
        if rel in files:
            files[rel]["class_assign"] = cal
    for rel, mal in mod_assign_lines.items():
        if rel in files and mal:
            files[rel]["mod_assign"] = mal

    # ---- instantiation traces: qualname -> tests that instantiated it or a subclass; subclass lists
    inst = {}
    subs = {}
    for t, m in meta.items():
        for q, mro in (m.get("inst") or {}).items():
            for anc in mro:
                inst[anc] = inst.get(anc, 0) | (1 << t)
            for anc in mro[1:]:
                subs.setdefault(anc, set()).add(q)
    inst_idx = {q: sets.add(mk) for q, mk in inst.items()}
    subs_idx = {q: sorted(v) for q, v in subs.items()}
    print(f"traced classes: {len(inst_idx)}; quses {len(quses_idx)}; test_uses {len(test_uses_idx)}")

    # ---- Enum classes: comparisons `x is E.M` and `x == E.M` are interchangeable for plain Enums
    # (identity-based __eq__), but not for str/int mixins. Keyed by simple name; ambiguous names are dropped.
    enum_kind = {}
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = []
            for b in node.bases:
                bases.append(b.id if isinstance(b, ast.Name) else (b.attr if isinstance(b, ast.Attribute) else "?"))
            if not any(b in ("Enum", "AutoName", "IntEnum", "StrEnum", "Flag", "IntFlag") for b in bases):
                continue
            plain = not any(b in ("str", "int", "IntEnum", "StrEnum", "IntFlag", "float", "bytes") for b in bases)
            if node.name in enum_kind and enum_kind[node.name] != plain:
                enum_kind[node.name] = None  # ambiguous
            else:
                enum_kind[node.name] = plain
    # subclasses of plain enums defined elsewhere (class Foo(AutoName) already covered; nested e.g. DataType.Type)
    plain_enums = sorted(n for n, p in enum_kind.items() if p)
    print(f"plain enums: {plain_enums}")
    index = {
        "plain_enums": plain_enums,
        "attr_uses": attr_uses,
        "attr_uses_all": attr_uses_all,
        "quses": quses_idx,
        "test_uses": test_uses_idx,
        "inst": inst_idx,
        "subs": subs_idx,
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
