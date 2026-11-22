"""Run a list of tests (pytest node ids) with unittest against the tree in the current directory and
print, as one JSON line on the real stdout, the node ids that failed. Everything the tests print goes to
stderr. A test whose module cannot be loaded counts as failed; anything unexpected marks every requested
test as failed (the caller then falls back to selecting them all)."""
import json
import os
import sys
import unittest


def main():
    ids = json.loads(sys.stdin.read())
    real_stdout = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)          # tests' prints land on stderr
    sys.stdout = sys.stderr
    failed = set()
    dotted_to_nid = {}
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for nid in ids:
        try:
            path, cls, meth = nid.split("::")
            mod = path[:-3].replace("/", ".")
            dotted = f"{mod}.{cls}.{meth}"
            dotted_to_nid[dotted] = nid
            suite.addTest(loader.loadTestsFromName(dotted))
        except Exception:
            failed.add(nid)

    class Result(unittest.TestResult):
        def _mark(self, test):
            nid = dotted_to_nid.get(test.id())
            if nid is None:
                failed.update(ids)  # a loader placeholder or an unknown id: be conservative
            else:
                failed.add(nid)

        def addFailure(self, test, err):
            self._mark(test)

        def addError(self, test, err):
            self._mark(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._mark(test)

        def addUnexpectedSuccess(self, test):
            self._mark(test)

    try:
        suite.run(Result())
    except BaseException:
        failed.update(ids)
    real_stdout.write(json.dumps(sorted(failed)) + "\n")
    real_stdout.flush()


if __name__ == "__main__":
    main()
