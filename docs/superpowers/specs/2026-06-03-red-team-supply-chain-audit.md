# Red-team supply-chain & dangerous-API audit (M0)

**Date:** 2026-06-03 · **Pass 5** of the red-team engagement. **Scope:** a static sweep
of the whole `src/kdive` tree for the supply-chain and injection surfaces that a
dependency CVE scan does not cover — XML parsing, unsafe deserialization/exec/shell,
SQL construction, dependency pinning/integrity, path traversal, and secret handling.
**Method:** structure- and pattern-based search (`rg`/`ast-grep`) plus reading each hit.

**Outcome: clean — no findings.** Recorded for provenance (a dated record that these
surfaces were swept), not because anything changed.

## What was checked

### XML parsing — defusedxml at every trust boundary
The only XML *parses* are of libvirtd-emitted documents, and both go through
`defusedxml.ElementTree.fromstring`:
- `providers/local_libvirt/discovery.py` — capabilities + domain-metadata parse.
- `providers/local_libvirt/install.py` — the `XMLDesc()` rewrite (hardened in pass 2,
  PR #54).

Everywhere else `xml.etree.ElementTree` is used for **construction only**
(`provisioning.render_domain_xml`, install's `<os>` rewrite — `ET.SubElement`/`tostring`,
no untrusted parse). The new debug / vmcore / retrieve / connect / gdb-MI planes parse no
XML. No `minidom`/`xml.sax`/`xml.dom`/`lxml` use. The `ProvisioningProfile.parse` /
`BuildProfile.parse` hits are pydantic model parsers (dict→model), not XML.

### Unsafe deserialization / exec / shell — none
No `yaml.load`, `pickle`, `marshal`, `eval`, `exec`, `os.system`, `os.popen`, or
`__import__`. Every `subprocess` call uses a **fixed list argv with `shell=False`**
(`build.py` `make`/`objcopy`, `debug_gdbmi.py` `gdb`, `retrieve.py` `crash`), with scoped
`# noqa: S603` justifications. The argv values are workspace paths derived from validated
UUIDs (`{staging_root}/{system_id}/{run_id}`), not profile-controlled strings. The real
fetch / checkout / `crash` / `gdb` implementations are `live_vm`-gated stubs that raise
`MISSING_DEPENDENCY` in M0, so the external-process injection surface is inert in the
shipped configuration.

### SQL — parameterized / composed, never interpolated
All dynamic SQL uses `psycopg.sql.SQL`/`Identifier` composition (`db/repositories.py`);
all value binding uses `%s` parameters. No f-string / `.format` / `%`-interpolated query
text reaches `execute()` in `src`.

### Dependency pinning & lockfile integrity
Every runtime and dev dependency is exact-pinned (`==`) in `pyproject.toml`; the only
range is the `uv_build` build-backend bound (`>=0.11.18,<0.12.0`). `uv.lock` carries
sha256 hashes (404 entries) so `--require-hashes` installs are reproducible. `pip-audit`
(pinned `@2.10.0`) runs in CI against the exported runtime (strict) and dev
(informational) requirement sets.

### Path traversal & secrets
- The file-ref secret backend resolves every ref through
  `security/paths.py::confine_to_root` before reading (`security/secrets.py`).
- `.secrets.baseline` + the detect-secrets pre-commit hook + the CI `pre-commit hooks`
  job guard against committed credentials.

## Residual / not in scope
- **CVE currency** is owned by CI's `pip-audit` job (re-runs per PR), not this one-time
  sweep.
- The **`live_vm` real subprocess seams** (git checkout of `kernel_source_ref`, `crash`
  argv, `gdb` target) should be re-audited for argument-injection **when they leave stub
  status** — at that point untrusted profile/ref values reach an external process argv.
  Flagged for the milestone that implements them.
