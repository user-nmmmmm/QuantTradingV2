"""Run pytest with usable temporary directories in the Windows sandbox.

Python 3.13's mode=0700 Windows ACL excludes the sandbox token even from
directories it just created. In this test process only, inherit workspace
permissions instead. Never changes ACLs of existing files or production code.
"""
from pathlib import Path
import os
import sys
import tempfile
import uuid


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    parent = root / 'outputs' / 'test_runtime'
    parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        original = os.mkdir

        def inherited_mkdir(path, mode=0o777, *, dir_fd=None):
            return original(path, 0o777 if mode == 0o700 else mode, dir_fd=dir_fd)

        os.mkdir = inherited_mkdir
    tempfile.tempdir = str(parent)
    target = (parent / f'pytest_{uuid.uuid4().hex}').resolve()
    if not target.is_relative_to(parent.resolve()) or target.exists():
        raise ValueError('Test directory must be new and inside outputs/test_runtime')
    import pytest
    return pytest.main(['-p', 'no:cacheprovider', '--basetemp', str(target), *sys.argv[1:]])


if __name__ == '__main__':
    raise SystemExit(main())
