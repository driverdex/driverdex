## 📝 Description
Provide a clear, concise summary of the changes introduced in this Pull Request.

Fixes #(issue number if applicable)

---

## 🧰 Type of Change
Please check the option(s) that apply:

- [ ] 📦 **Driver Update / Addition** (New driver added, updated INF, or corrected HWID categorization)
- [ ] ⚙️ **Tooling Enhancement** (Updates to `driverdex_builder.py` or `driverdex_autosync.py`)
- [ ] 🗜️ **Packaging / Manifest Fix** (Fixes `.7z` volume splitting, `driverdex_index.json`, or category `.manifest.json` files)
- [ ] 🐛 **Bug Fix** (Non-breaking fix for an issue)
- [ ] 📖 **Documentation** (Updates to README, Code of Conduct, or internal guides)

---

## 🧪 Verification & Pre-Flight Checks

Before submitting, please confirm you have executed the following checks:

### For Tooling & Script Code Changes:
- [ ] Ran local execution tests with Python 3.7+ without throwing unhandled exceptions.
- [ ] Tested dry-run or pipeline options to ensure zero unexpected disk/file lock issues.

### For Driver Submissions & Manifest Updates:
- [ ] All submitted drivers are **unmodified vendor binaries** (clean `.inf`, `.sys`, `.dll`, `.cat`).
- [ ] Ran an updated anti-virus scan on all driver packages prior to upload.
- [ ] Verified that installer sibling files (EXE/DLL/config dependencies) remain intact.
- [ ] Ran `driverdex_builder.py` to auto-generate/update manifests cleanly—no manual, malformed JSON edits.

---

## 📑 Console / Audit Output
If applicable, paste terminal output, dry-run logs, or manifest snippets proving successful execution:

```text
Paste logs here
