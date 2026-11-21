#!/usr/bin/env python3
"""Pick the tests a patch can affect and split them across workers.

Reads /data/commits/*.patch and /data/tests.json, writes one JSON line per commit to stdout.
Everything about the base tree comes from the prebuilt index (index.pkl.gz) and the checkout at REPO_DIR.
Standard library only.
"""
import ast
import gzip
import json
import os
import pickle
import sys
import time

DATA = os.environ.get("DATA_DIR", "/data")
REPO = os.environ.get("REPO_DIR", "/app")
INDEX_PATH = os.environ.get("INDEX_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.pkl.gz"))
WORKER_MS = 2000
MAX_WORKERS = 8
DEFUSE = os.environ.get("SEL_DEFUSE", "1") == "1"

K_NONE, K_MODULE, K_CLASS, K_FUNC, K_DEF, K_DOC, K_TC = 0, 1, 2, 3, 4, 5, 6

def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------- index
class Index:
    def __init__(self, path):
        with gzip.open(path, "rb") as f:
            d = pickle.load(f)
        self.sets = d["sets"]
        self.files = d["files"]
        self.attr_uses = d.get("attr_uses", {})
        self.dir_readers = d["dir_readers"]
        self.all_mask = self.sets[d["all"]]
        self.ntests = d["ntests"]
        self.tests = d["tests"]  # number -> (id, ms)

    def m(self, sid):
        return self.sets[sid]


# ----------------------------------------------------------------------------- unified diff
class Hunk:
    __slots__ = ("old_start", "old_len", "new_start", "new_len", "lines")

    def __init__(self, a, b, c, d):
        self.old_start, self.old_len, self.new_start, self.new_len, self.lines = a, b, c, d, []


def parse_patch(text):
    files = []
    cur = None
    hunk = None
    rem_old = rem_new = 0
    for raw in text.split("\n"):
        if hunk is not None and rem_old <= 0 and rem_new <= 0:
            hunk = None  # hunk complete; whatever follows is not part of it
        if raw.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "new_file": False, "deleted": False, "binary": False}
            files.append(cur)
            hunk = None
            rest = raw[len("diff --git "):]
            if rest.startswith("a/") and " b/" in rest:
                a, b = rest[2:].split(" b/", 1)
                cur["old"], cur["new"] = a, b
            continue
        if cur is None:
            continue
        if hunk is not None:
            tag = raw[:1]
            if tag == "\\":
                continue  # "\ No newline at end of file"
            if raw == "":
                raw, tag = " ", " "  # an empty context line with its leading space stripped
            if tag == " ":
                rem_old -= 1; rem_new -= 1
            elif tag == "-":
                rem_old -= 1
            elif tag == "+":
                rem_new -= 1
            else:
                raise ValueError(f"unexpected line inside hunk: {raw[:40]!r}")
            hunk.lines.append(raw)
            continue
        if raw.startswith("--- "):
            p = raw[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
        elif raw.startswith("+++ "):
            p = raw[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
        elif raw.startswith("new file mode"):
            cur["new_file"] = True
        elif raw.startswith("deleted file mode"):
            cur["deleted"] = True
        elif raw.startswith("rename from "):
            cur["old"] = raw[len("rename from "):]
        elif raw.startswith("rename to "):
            cur["new"] = raw[len("rename to "):]
        elif raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
            cur["binary"] = True
        elif raw.startswith("@@"):
            h = raw[2:].split("@@")[0].strip().split()
            o, n = h[0], h[1]
            os_, ol = (o[1:].split(",") + ["1"])[:2]
            ns_, nl = (n[1:].split(",") + ["1"])[:2]
            hunk = Hunk(int(os_), int(ol), int(ns_), int(nl))
            rem_old, rem_new = hunk.old_len, hunk.new_len
            cur["hunks"].append(hunk)
    return files


class Applied:
    """Result of applying hunks: new text, removed old lines, added new lines with anchors, line maps."""
    __slots__ = ("new_lines", "removed", "added", "new_to_old", "old_to_new")


def apply_hunks(old_text, hunks):
    old = old_text.split("\n")
    out = []
    removed = []
    added = []      # (new_lineno, anchors(list of removed old lines in block), prev_old_line)
    new_to_old = {}
    pos = 1  # next old line (1-based) not yet emitted
    for h in hunks:
        if h.old_start < pos:
            raise ValueError("overlapping hunks")
        while pos < h.old_start or (h.old_len == 0 and pos <= h.old_start and pos <= len(old) and pos < h.old_start + 1 and False):
            out.append(old[pos - 1]); new_to_old[len(out)] = pos; pos += 1
        if h.old_len == 0:
            # insertion after old line old_start (git convention: old_start is the line before)
            while pos <= h.old_start:
                out.append(old[pos - 1]); new_to_old[len(out)] = pos; pos += 1
        i = 0
        lines = h.lines
        while i < len(lines):
            l = lines[i]
            tag = l[:1]
            if tag == " ":
                if pos > len(old) or old[pos - 1] != l[1:]:
                    raise ValueError(f"context mismatch at old line {pos}")
                out.append(old[pos - 1]); new_to_old[len(out)] = pos; pos += 1
                i += 1
            elif tag in "-+":
                # one change block: all '-' and '+' until the next context line
                block_removed = []
                block_added = []
                prev_old = pos - 1
                while i < len(lines) and lines[i][:1] in "-+":
                    l2 = lines[i]
                    if l2[0] == "-":
                        if pos > len(old) or old[pos - 1] != l2[1:]:
                            raise ValueError(f"removed-line mismatch at old line {pos}")
                        removed.append(pos); block_removed.append(pos); pos += 1
                    else:
                        out.append(l2[1:]); block_added.append(len(out))
                    i += 1
                for n in block_added:
                    added.append((n, tuple(block_removed), prev_old))
            else:
                raise ValueError("bad hunk line")
    while pos <= len(old):
        out.append(old[pos - 1]); new_to_old[len(out)] = pos; pos += 1
    a = Applied()
    a.new_lines, a.removed, a.added, a.new_to_old = out, removed, added, new_to_old
    a.old_to_new = {o: n for n, o in new_to_old.items()}
    return a


# ----------------------------------------------------------------------------- new-tree structure
class NewTree:
    def __init__(self, tree, nlines):
        self.stmts = []          # (start, end, node, parent_block, index_in_block, owner)
        self.func_of_line = {}
        self.doc_lines = set()
        self.str_lines = set()   # lines inside non-docstring multi-line string constants
        self.line_stmt = {}      # line -> innermost simple statement / header record
        self.funcs = {}          # qualname -> (start, end, node)
        self.class_lines = set() # lines at class-body level (not in a def)
        self.module_lines = set()
        self.tc_lines = set()
        self.lambdas = {}        # line -> (start, end) of the innermost multi-line lambda
        self._walk(tree.body, None, None, "", "module")

    def _is_tc(self, node):
        t = node.test
        return (isinstance(t, ast.Name) and t.id == "TYPE_CHECKING") or (isinstance(t, ast.Attribute) and t.attr == "TYPE_CHECKING")

    def _walk(self, body, owner_func, block_owner, qual, ctx):
        # ctx: "module" | "class" | "func"
        for idx, st in enumerate(body):
            s, e = st.lineno, st.end_lineno
            rec = {"start": s, "end": e, "node": st, "block": body, "idx": idx, "owner": block_owner, "func": owner_func, "ctx": ctx}
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{qual}.{st.name}" if qual else st.name
                ds = min([d.lineno for d in st.decorator_list] + [s])
                self.funcs[name] = (ds, e, st)
                for ln in range(ds, e + 1):
                    self.func_of_line.setdefault(ln, (name, ds, e, st))
                bs = st.body[0].lineno if st.body else e
                for ln in range(ds, bs):
                    self.line_stmt.setdefault(ln, rec)
                self._doc(st.body)
                self._walk(st.body, (name, ds, e, st), st, name, "func")
            elif isinstance(st, ast.ClassDef):
                name = f"{qual}.{st.name}" if qual else st.name
                ds = min([d.lineno for d in st.decorator_list] + [s])
                bs = st.body[0].lineno if st.body else e
                for ln in range(ds, bs):
                    self.line_stmt.setdefault(ln, rec)
                    if ctx == "module": self.module_lines.add(ln)
                    elif ctx == "class": self.class_lines.add(ln)
                self._doc(st.body)
                self._walk(st.body, owner_func, st, name, "class" if ctx != "func" else "func")
            elif isinstance(st, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar, ast.Match)):
                self.line_stmt.setdefault(s, rec)
                if ctx == "module": self.module_lines.add(s)
                elif ctx == "class": self.class_lines.add(s)
                tc = isinstance(st, ast.If) and self._is_tc(st)
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(st, field, None)
                    if sub:
                        if tc and field == "body":
                            for ln in range(sub[0].lineno, sub[-1].end_lineno + 1):
                                self.tc_lines.add(ln)
                        self._walk(sub, owner_func, st, qual, ctx)
                for hnd in getattr(st, "handlers", []) or []:
                    self.line_stmt.setdefault(hnd.lineno, rec)
                    self._walk(hnd.body, owner_func, st, qual, ctx)
                for c in getattr(st, "cases", []) or []:
                    self.line_stmt.setdefault(c.pattern.lineno, rec)
                    self._walk(c.body, owner_func, st, qual, ctx)
            else:
                for ln in range(s, e + 1):
                    self.line_stmt.setdefault(ln, rec)
                    if ctx == "module": self.module_lines.add(ln)
                    elif ctx == "class": self.class_lines.add(ln)
                if e > s:
                    for node in ast.walk(st):
                        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.end_lineno > node.lineno:
                            for ln in range(node.lineno, node.end_lineno + 1):
                                self.str_lines.add(ln)
                        elif isinstance(node, ast.Lambda) and node.end_lineno > node.lineno:
                            for ln in range(node.lineno, node.end_lineno + 1):
                                cur = self.lambdas.get(ln)
                                if cur is None or (node.end_lineno - node.lineno) < (cur[1] - cur[0]):
                                    self.lambdas[ln] = (node.lineno, node.end_lineno)

    def _doc(self, body):
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            for ln in range(body[0].lineno, body[0].end_lineno + 1):
                self.doc_lines.add(ln)

    def module_doc(self, tree):
        self._doc(tree.body)


# ----------------------------------------------------------------------------- analysis
class Selector:
    def __init__(self, idx):
        self.idx = idx
        self.ms = {n: ms for n, (tid, ms) in idx.tests.items()}
        self._src_cache = {}

    def read_old(self, rel):
        if rel in self._src_cache:
            return self._src_cache[rel]
        p = os.path.join(REPO, rel)
        try:
            with open(p, "rb") as f:
                txt = f.read().decode("utf-8")
        except FileNotFoundError:
            txt = None
        self._src_cache[rel] = txt
        return txt

    # --- old-line test lookup
    def tests_old_line(self, fe, L, kind_hint=None):
        """Tests that executed the statement containing old line L.

        Always the whole statement (or compound-statement header), never just the line: a change to one
        line of a multi-line expression alters every execution of that statement, including executions
        that short-circuited before reaching the line."""
        idx = self.idx
        lines = fe.get("lines")
        m = 0
        lam = fe.get("lambdas", {}).get(L)
        if lam is not None and L > lam[0] and lines:
            # strictly inside a multi-line lambda: only its callers matter (the first line also runs
            # when the enclosing statement builds the function object, so it is left out)
            for ln in range(lam[0] + 1, lam[1] + 1):
                sid = lines.get(ln)
                if sid is not None:
                    m |= idx.m(sid)
            if m:
                return m
        span = fe.get("spans", {}).get(L, (L, L))
        if lines:
            for ln in range(span[0], span[1] + 1):
                sid = lines.get(ln)
                if sid is not None:
                    m |= idx.m(sid)
        if m:
            return m
        kind = fe["kind"][L] if "kind" in fe and L < len(fe["kind"]) else K_NONE
        if kind in (K_NONE, K_DOC, K_TC):
            return 0
        if kind in (K_DEF, K_FUNC):
            return self.func_body_tests(fe, L)
        # module/class level line without coverage (dead branch at import) -> importers
        return idx.m(fe["importers"])

    def class_attr_tests(self, fe, names, fallback):
        """Tests that can observe a change to class attributes `names`: those that executed a line
        reading one of the names, restricted to tests that loaded this module at all."""
        if not names or not DEFUSE:
            return fallback
        m = 0
        for n in names:
            sid = self.idx.attr_uses.get(n)
            if sid is None:
                continue
            m |= self.idx.m(sid)
        return m & self.idx.m(fe["importers"])

    def func_body_tests(self, fe, L):
        best = None
        for (s, e, bs, sid, name) in fe.get("funcs", []):
            if s <= L <= e and (best is None or (e - s) < (best[1] - best[0])):
                best = (s, e, sid)
        if best is None:
            return self.idx.m(fe["importers"])
        return self.idx.m(best[2])

    def old_func_by_name(self, fe, name):
        for (s, e, bs, sid, fname) in fe.get("funcs", []):
            if fname == name:
                return (s, e, bs, sid)
        return None

    # --- per file
    def analyze_file(self, f):
        idx = self.idx
        rel_old, rel_new = f["old"], f["new"]
        rel = rel_new or rel_old
        fe_old = idx.files.get(rel_old) if rel_old else None
        fe_new = idx.files.get(rel_new) if rel_new else None
        m = 0
        if f["binary"]:
            for fe in (fe_old, fe_new):
                if fe:
                    m |= idx.m(fe["readers"]) | idx.m(fe["any"])
            return m
        if f["deleted"] or (rel_old and rel_new and rel_old != rel_new):
            # deleted or renamed: everything that imported, executed or read the old path
            if fe_old:
                m |= idx.m(fe_old["importers"]) | idx.m(fe_old["any"]) | idx.m(fe_old["readers"])
            m |= self.dir_tests(rel_old)
            if f["deleted"]:
                return m
        if f["new_file"] or (fe_old is None and fe_new is None and not os.path.exists(os.path.join(REPO, rel))):
            m |= self.dir_tests(rel)
            if rel.endswith(".py") and self.is_dialect_module(rel):
                m |= self.dialect_enumeration_tests()
            return m
        fe = fe_old or fe_new
        if fe is None:
            # file exists in the tree but nothing ever touched it (docs, configs): harmless
            return m
        old_text = self.read_old(rel_old or rel)
        if old_text is None:
            return m | idx.m(fe["any"]) | idx.m(fe["importers"]) | idx.m(fe["readers"])
        try:
            ap = apply_hunks(old_text, f["hunks"])
        except Exception as e:  # patch does not apply cleanly against our tree: be conservative
            log(f"  apply failed for {rel}: {e}")
            return m | idx.m(fe["any"]) | idx.m(fe["importers"]) | idx.m(fe["readers"])
        if rel.endswith(".py"):
            return m | self.analyze_python(rel, fe, old_text, ap)
        return m | self.analyze_data(rel, fe, old_text, ap)

    def dir_tests(self, rel):
        d = os.path.dirname(rel)
        sid = self.idx.dir_readers.get(d)
        return self.idx.m(sid) if sid is not None else 0

    def is_dialect_module(self, rel):
        return rel.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/tokenizers/", "sqlglot/typing/"))

    def dialect_enumeration_tests(self):
        m = 0
        for rel in ("tests/test_dialect_imports.py", "tests/test_dialect_entry_points.py"):
            fe = self.idx.files.get(rel)
            if fe:
                m |= self.idx.m(fe["importers"])
        return m

    def analyze_data(self, rel, fe, old_text, ap):
        idx = self.idx
        readers = idx.m(fe["readers"]) | idx.m(fe["any"])
        if rel.startswith("tests/fixtures/") and rel.endswith(".sql"):
            def norm(lines):
                return [l for l in lines if l and not l.startswith("--")]
            if norm(old_text.split("\n")) == norm(ap.new_lines):
                return 0
        return readers

    def analyze_python(self, rel, fe, old_text, ap):
        idx = self.idx
        new_text = "\n".join(ap.new_lines)
        try:
            tree = ast.parse(new_text)
        except (SyntaxError, ValueError):
            return idx.m(fe["importers"]) | idx.m(fe["any"])
        m = 0
        kind = fe.get("kind", b"")
        # removed / modified lines: what executed them in the base tree
        class_assign = fe.get("class_assign", {})
        for L in ap.removed:
            k = kind[L] if L < len(kind) else K_NONE
            if k in (K_NONE, K_DOC, K_TC):
                continue
            t = self.tests_old_line(fe, L)
            if k == K_CLASS and L in class_assign and (fe.get("lambdas", {}).get(L) is None or L == fe["lambdas"][L][0]):
                t = self.class_attr_tests(fe, class_assign[L], t)
            m |= t
        if not ap.added:
            return m
        nt = NewTree(tree, len(ap.new_lines))
        nt.module_doc(tree)
        for (N, anchors, prev_old) in ap.added:
            text = ap.new_lines[N - 1].strip()
            if N in nt.doc_lines or N in nt.tc_lines:
                continue
            if (not text or text.startswith("#")) and N not in nt.str_lines:
                continue
            rec = nt.line_stmt.get(N)
            if rec is None:
                # inside a multi-line string / expression we did not map: use the enclosing function
                m |= self.enclosing_new(fe, nt, N)
                continue
            # insertion inside an existing statement (e.g. a new dict entry): map the statement's other lines;
            # strictly inside a multi-line lambda only the lambda's body lines matter
            lam = nt.lambdas.get(N)
            if lam is not None and N > lam[0]:
                lmapped = [ap.new_to_old[x] for x in range(lam[0] + 1, lam[1] + 1) if x in ap.new_to_old]
                lm = 0
                for o in lmapped:
                    lm |= self.tests_old_line(fe, o)
                if lm:
                    m |= lm
                    continue
            mapped = [ap.new_to_old[x] for x in range(rec["start"], rec["end"] + 1) if x in ap.new_to_old]
            if mapped:
                t = 0
                for o in mapped:
                    t |= self.tests_old_line(fe, o)
                if rec["ctx"] == "class":
                    t = self.class_attr_tests(fe, self.assign_names(rec["node"]), t)
                m |= t
                continue
            # a genuinely new statement
            if rec["ctx"] == "class":
                m |= self.class_attr_tests(fe, self.assign_names(rec["node"]), idx.m(fe["importers"]))
                continue
            if rec["ctx"] == "module":
                m |= idx.m(fe["importers"])
                continue
            m |= self.new_statement_tests(fe, nt, ap, rec)
        return m

    @staticmethod
    def assign_names(node):
        if isinstance(node, ast.Assign):
            return [n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)]
        if isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            return [node.target.id]
        return []

    def enclosing_new(self, fe, nt, N):
        f = nt.func_of_line.get(N)
        if f is None:
            return self.idx.m(fe["importers"])
        of = self.old_func_by_name(fe, f[0])
        if of is None:
            return self.idx.m(fe["importers"])
        return self.idx.m(of[3])

    def new_statement_tests(self, fe, nt, ap, rec):
        idx = self.idx
        f = rec["func"]
        of = self.old_func_by_name(fe, f[0]) if f else None
        if of is None:
            # new function/method: it may override an inherited method, so whoever imports the module
            return idx.m(fe["importers"])
        # previous sibling that exists in the base tree
        block, i = rec["block"], rec["idx"]
        while i > 0:
            i -= 1
            prev = block[i]
            ln = min([d.lineno for d in getattr(prev, "decorator_list", [])] + [prev.lineno])
            o = ap.new_to_old.get(ln)
            if o is not None:
                return self.tests_old_line(fe, o)
            # the previous sibling is new too; keep walking back
        owner = rec["owner"]
        if owner is None or isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return idx.m(of[3])  # first statement of the function body: all its callers
        # first statement of a compound block: whoever evaluated the block header
        hdr = owner.lineno
        # for else/except/finally bodies the header line is the compound statement's start; fine (superset)
        o = ap.new_to_old.get(hdr)
        if o is not None:
            return self.tests_old_line(fe, o)
        return idx.m(of[3])

    # --- whole commit
    def select(self, patch_text):
        files = parse_patch(patch_text)
        if not files:
            return self.idx.all_mask
        m = 0
        for f in files:
            try:
                m |= self.analyze_file(f)
            except Exception as e:
                log(f"  analysis error in {f.get('new') or f.get('old')}: {e!r}")
                fe = self.idx.files.get(f.get("new") or f.get("old") or "")
                if fe:
                    m |= self.idx.m(fe["importers"]) | self.idx.m(fe["any"]) | self.idx.m(fe["readers"])
                else:
                    m |= self.idx.all_mask
        return m

    # --- packing
    def pack(self, mask):
        if not mask:
            return []
        ms = self.ms
        tests = []
        n = 0
        while mask:
            if mask & 1:
                tests.append(n)
            mask >>= 1
            n += 1
        tests.sort(key=lambda t: -ms[t])
        best = None
        for k in range(1, min(MAX_WORKERS, len(tests)) + 1):
            loads = [0] * k
            groups = [[] for _ in range(k)]
            for t in tests:
                i = loads.index(min(loads))
                loads[i] += ms[t]
                groups[i].append(t)
            cost = max(loads) + WORKER_MS * k
            if best is None or cost < best[0]:
                best = (cost, groups)
        return best[1]


def main():
    t0 = time.process_time()
    idx = Index(INDEX_PATH)
    sel = Selector(idx)
    t1 = time.process_time()
    commits_dir = os.path.join(DATA, "commits")
    names = sorted(n for n in os.listdir(commits_dir) if n.endswith(".patch"))
    out = []
    for name in names:
        cid = name[:-6]
        try:
            with open(os.path.join(commits_dir, name), "rb") as f:
                text = f.read().decode("utf-8", errors="replace")
            mask = sel.select(text)
            workers = sel.pack(mask)
        except Exception as e:
            log(f"{cid}: fatal {e!r}; selecting everything")
            workers = sel.pack(idx.all_mask)
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    log(f"index load {t1 - t0:.3f}s, {len(names)} commits in {time.process_time() - t1:.3f}s cpu")


if __name__ == "__main__":
    main()
