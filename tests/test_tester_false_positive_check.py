"""
False-positive check for src/tester.py, using REAL, verbatim worker
output from two tasks individually reviewed and approved by a human
during the 2026-08-19/20 production session.

Fixture text is copied exactly as it was originally generated and
retrieved from the production database (agencyos.db) -- it is NOT
paraphrased or reconstructed. Do not edit the fixture strings.

- Fixture 1: task 4392810431, repo ansible/ansible (approved)
- Fixture 2: task 5185208728, repo python/cpython, docs-only (approved)
"""
import unittest
from src.tester import Tester
from src.planner import TaskSpec
from src.worker import WorkerResult


FIXTURE_1_ANSIBLE_XATTR = r'''### Root Cause Analysis

The `ansible.builtin.copy` module (and underlying file action plugins / module utils) relies on Python's standard file operations (`shutil.copy`, etc.) to duplicate files on the remote managed node. While Ansible has long supported preserving standard metadata such as ownership (`owner`), permissions (`mode`), and timestamps (`selevel`, `serole`, `setype`, `seuser`, and modification/access times), it lacked native support for copying and preserving file system **Extended Attributes (xattrs)**.

Extended attributes are key-value pairs associated permanently with files and directories, distinct from the normal attributes such as mode and owner. They are heavily used in modern Linux environments for security modules like SELinux, Integrity Measurement Architecture (IMA), Access Control Lists (ACLs - though often handled separately), and container/storage systems (e.g., Lustre, GlusterFS).

**Root Causes Addressed:**
1. **Absence of xattr copy logic:** The copy module file-transfer and staging sequence did not extract xattrs from the source file nor apply them to the destination file.
2. **Platform and Library Dependencies:** Extended attributes require OS-specific system calls (e.g., `listxattr`, `getxattr`, `setxattr` on Linux/BSD). Python's standard library lacks a high-level cross-platform API for this without relying on external packages or low-level `ctypes`/platform modules. Python 3.3+ provides `os.getxattr`, `os.setxattr`, and `os.listxattr` natively on Linux and macOS, but they need to be safely integrated into Ansible's file module-utils (`ansible/module_utils/basic.py` and action plugins).

---

### Modified Code Implementation

To resolve issue #86945, we introduce a new boolean parameter `preserve_xattrs` (defaulting to `false` for backwards compatibility) to the `copy` module, supported by core file manipulation helper functions in Ansible's module framework.

#### 1. Modify `ansible/module_utils/basic.py` (File/AnsibleModule utilities)
Add helper functions to safely read and write extended attributes using Python's built-in `os` module capabilities.

```python
# In ansible/module_utils/basic.py

import os

def get_sys_extended_attributes(path):
    """
    Retrieve extended attributes for a given file path.
    Returns a dictionary of {attr_name: attr_value}.
    """
    xattrs = {}
    if not hasattr(os, 'listxattr') or not hasattr(os, 'getxattr'):
        return xattrs
    try:
        # Some filesystems or files might not support xattrs
        for attr in os.listxattr(path):
            # Skip security namespaces or system namespaces if necessary,
            # or copy all user.* xattrs. Here we fetch all readable xattrs.
            try:
                xattrs[attr] = os.getxattr(path, attr)
            except OSError:
                # Permission denied or attribute not supported on specific items
                pass
    except (OSError, NotImplementedError):
        pass
    return xattrs

def set_sys_extended_attributes(path, xattrs):
    """
    Set extended attributes on a given file path from a dictionary.
    """
    if not hasattr(os, 'setxattr') or not xattrs:
        return
    for attr, value in xattrs.items():
        try:
            os.setxattr(path, attr, value)
        except (OSError, NotImplementedError):
            # Log or ignore if filesystem doesn't support or permissions insufficient
            pass
```

#### 2. Update `ansible/modules/copy.py`
Add the `preserve_xattrs` argument definition and integrate the xattr preservation step after the file is copied/moved into place.

```python
# Argument spec addition in ansible/modules/copy.py
argument_spec.update(
    dict(
        # ... existing options ...
        preserve_xattrs=dict(type='bool', default=False),
    )
)
```

Within the module execution flow, after the file contents are successfully transferred/copied:

```python
# Inside the copy execution logic:
preserve_xattrs = module.params.get('preserve_xattrs')

if preserve_xattrs:
    src_xattrs = get_sys_extended_attributes(src)
    # Perform copy...
    # After destination file is in place and ownership/permissions are set:
    set_sys_extended_attributes(dest, src_xattrs)
```

---

### Test Verification

To ensure robust implementation, unit and integration tests are added.

#### 1. Unit Test (`test/units/modules/test_copy.py`)
Mock the file system and verify that `preserve_xattrs=True` reads source xattrs and applies them to the destination.

```python
from units.compat.mock import patch, MagicMock
from ansible.modules import copy

@patch('ansible.module_utils.basic.os.listxattr')
@patch('ansible.module_utils.basic.os.getxattr')
@patch('ansible.module_utils.basic.os.setxattr')
def test_copy_preserve_xattrs(mock_setxattr, mock_getxattr, mock_listxattr):
    mock_listxattr.return_value = ['user.test_attr']
    mock_getxattr.return_value = b'test_value'

    # Run test execution context ensuring setxattr is invoked with correct parameters
    # ...
    mock_setxattr.assert_called_with('/path/to/dest', 'user.test_attr', b'test_value')
```

#### 2. Integration Test (`test/integration/targets/copy/tasks/xattrs.yml`)
Verify real filesystem integration on Linux platforms supporting xattrs (like ext4/xfs with user_xattr mounted).

```yaml
- name: Create a test file with extended attributes
  copy:
    content: "xattr test content"
    dest: "{{ remote_dir }}/source_file.txt"

- name: Set extended attribute using command/shell (or python)
  command: "setfattr -n user.ansible_test -v 'hello_xattr' {{ remote_dir }}/source_file.txt"

- name: Copy file preserving xattrs
  copy:
    src: "{{ remote_dir }}/source_file.txt"
    dest: "{{ remote_dir }}/dest_file.txt"
    remote_src: yes
    preserve_xattrs: yes

- name: Verify extended attribute exists on destination
  command: "getfattr -n user.ansible_test --only-values {{ remote_dir }}/dest_file.txt"
  register: xattr_result

- name: Assert xattr value matches
  assert:
    that:
      - xattr_result.stdout == 'hello_xattr'
```

---

### Documentation

Add documentation updates to `ansible/modules/copy.py` (DOCUMENTATION fragment).

```yaml
options:
  # ... existing options ...
  preserve_xattrs:
    description:
      - Whether to preserve extended attributes (xattrs) from the source file to the destination file.
      - Requires underlying file system support and appropriate permissions.
    type: bool
    default: no
    version_added: "2.18"
```'''


FIXTURE_2_TARFILE_DOCS = r'''### Root Cause Analysis

In Python's `tarfile` module, extraction filters (introduced to mitigate security vulnerabilities like arbitrary file writes via directory traversal) validate archive members to ensure they do not write outside the destination directory.

Specifically:
- The `'data'` filter strictly validates both member names (`name`) and symbolic/hard link targets (`linkname`) to ensure they remain safely within the destination directory.
- The `'fully_trusted'` filter performs no checks.
- The legacy/compatibility `'tar'` filter (which mimics traditional tar extraction behavior) checks and bounds the member `name` to prevent directory traversal, but **does not bound `linkname`**. This means that if an archive contains a symbolic link or hard link pointing to an absolute path or traversing outside the destination via relative paths (`../../etc/passwd`), the `'tar'` filter will allow it.

Because this behavior is intentional for backward compatibility with older `tar` semantics (where links were not vetted for containment), but poses a security/unexpected risk for users who assume all built-in filters restrict links, the documentation needs to explicitly state that the `'tar'` extraction filter does **not** bound link targets (`TarInfo.linkname`).

---

### Modified Documentation

We need to update `Doc/library/tarfile.rst` under the description of the `'tar'` extraction filter to explicitly document this limitation.

#### `Doc/library/tarfile.rst` Diff

```rst
--- a/Doc/library/tarfile.rst
+++ b/Doc/library/tarfile.rst
@@ -835,8 +835,9 @@ The following filters are built-in and can be passed as a string to

 .. data:: 'tar'

-    A filter that mimics standard tar defaults. It drops dangerous file types
-    and restricts unexpected absolute paths or directory traversals.
+    A filter that mimics standard tar defaults. It drops dangerous file types,
+    restricts unexpected absolute paths or directory traversals, but **does not**
+    bound link targets (``TarInfo.linkname``).

     .. versionadded:: 3.12

```

---

### Test Verification

Although this is a documentation-only fix, we can verify that the behavior is correctly tested in Python's standard library test suite (e.g., `Lib/test/test_tarfile.py`), ensuring that tests confirm that `'tar'` permits external link targets while `'data'` blocks them.

You can run the relevant tests using:
```bash
./python -m unittest test.test_tarfile
```

---

### Summary of Changes

1. **Root Cause Identified**: The `'tar'` extraction filter intentionally preserves legacy tar behavior by omitting validation on `TarInfo.linkname`, which needed explicit documentation.
2. **Documentation Updated**: Modified `Doc/library/tarfile.rst` to explicitly warn users that the `'tar'` filter does not bound link targets.
3. **Verification**: Ensured alignment with CPython's security design for tarfile extraction filters.'''


class TestTesterFalsePositiveCheck(unittest.TestCase):
    """
    Confirms src/tester.py does NOT reject genuinely good, real,
    human-approved Worker output. A false positive here would silently
    reject good work before it ever reaches the human review queue --
    a worse failure mode than the fabrication it was built to catch.
    """

    def setUp(self):
        self.tester = Tester()

    def test_fixture_1_ansible_xattr_passes(self):
        """Real, approved ansible/ansible task 4392810431 must pass the Tester."""
        ts = TaskSpec(
            opportunity_id="4392810431",
            task="Resolve issue #86945 (Add support for preserving extended "
                 "attributes in the copy module) in repository ansible/ansible.",
            priority="MEDIUM",
            expected_output="Working code fix addressing 'Add support for "
                             "preserving extended attributes in the copy module'. "
                             "Must include root cause analysis, modified code "
                             "implementation, test verification, and documentation.",
            estimated_cost=0.0001,
            input_tokens=150,
        )
        wr = WorkerResult(
            opportunity_id="4392810431",
            output=FIXTURE_1_ANSIBLE_XATTR,
            execution_time_sec=3.5,
            actual_cost=0.000685,
            prompt_tokens=150,
            completion_tokens=1400,
            model="gemini-3.5-flash-lite",
            http_status=200,
            status="SUCCESS",
            error_reason="",
        )
        result = self.tester.check(ts, wr)
        print("\n--- FIXTURE 1 (ansible xattr) ---")
        print("Passed        :", result.passed)
        print("Checked       :", result.checked_symbols)
        print("Unresolved    :", result.unresolved_symbols)
        print("Feedback      :", result.feedback)
        self.assertTrue(
            result.passed,
            f"FALSE POSITIVE on real, approved ansible task 4392810431. "
            f"Unresolved symbols: {result.unresolved_symbols}. "
            f"Feedback: {result.feedback}"
        )

    def test_fixture_2_tarfile_docs_passes(self):
        """Real, approved python/cpython docs-only task 5185208728 must pass the Tester."""
        ts = TaskSpec(
            opportunity_id="5185208728",
            task="Resolve issue #156026 (Doc/library/tarfile.rst: document that "
                 "the 'tar' extraction filter does not bound link targets) in "
                 "repository python/cpython.",
            priority="LOW",
            expected_output="Working code fix addressing 'Doc/library/tarfile.rst: "
                             "document that the tar extraction filter does not bound "
                             "link targets'. Must include root cause analysis, modified "
                             "code implementation, test verification, and documentation.",
            estimated_cost=0.0001,
            input_tokens=140,
        )
        wr = WorkerResult(
            opportunity_id="5185208728",
            output=FIXTURE_2_TARFILE_DOCS,
            execution_time_sec=2.1,
            actual_cost=0.002160,
            prompt_tokens=140,
            completion_tokens=900,
            model="gemini-3.5-flash-lite",
            http_status=200,
            status="SUCCESS",
            error_reason="",
        )
        result = self.tester.check(ts, wr)
        print("\n--- FIXTURE 2 (tarfile docs) ---")
        print("Passed        :", result.passed)
        print("Checked       :", result.checked_symbols)
        print("Unresolved    :", result.unresolved_symbols)
        print("Feedback      :", result.feedback)
        self.assertTrue(
            result.passed,
            f"FALSE POSITIVE on real, approved cpython docs task 5185208728. "
            f"Unresolved symbols: {result.unresolved_symbols}. "
            f"Feedback: {result.feedback}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
