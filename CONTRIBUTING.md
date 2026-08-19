# Contributing to DriverDex

Thank you for your interest in contributing to **DriverDex**! Whether you are submitting missing Windows drivers, fixing bugs in `driverdex_builder.py` / `driverdex_autosync.py`, or improving documentation, your help keeps this repository accurate and up-to-date.

---

## 1. Ways to Contribute

Contributions generally fall into two categories:

1. **Driver Contributions:** Adding missing drivers, updating outdated INF manifests, or fixing category misclassifications.
2. **Tooling & Code Contributions:** Enhancing the Builder/AutoSync scripts, optimizing `.7z` packaging logic, or fixing GitHub API integration bugs.

---

## 2. Driver Submission Guidelines

Because DriverDex relies on strict version control and automated manifest indexing, driver submissions must follow these rules:

* **Unmodified Vendor Drivers Only:** Submissions must consist of official, unmodified vendor drivers. **Never submit repacked or modified executables/DLLs.**
* **Preserve Installer Siblings:** If an INF file depends on adjacent configuration files or setup executables, keep them together in the source folder so `driverdex_builder.py` can package them properly.
* **Scan Before Submitting:** Run an anti-virus scan on all driver files prior to opening a Pull Request.
* **Vendor Copyright:** Respect original vendor licenses. DriverDex aggregates drivers for compatibility and archival purposes only.

---

## 3. Code & Tooling Workflow

If you are contributing code improvements to `driverdex_builder.py` or `driverdex_autosync.py`:

<Image src="image_agent_tag_2991106331888220379" alt="Git branching workflow diagram showing feature branches merging into development" caption="Standard feature-branch workflow for repository changes" />

<Sequence>

{/* Reason: Critical sequence for setting up local development and opening a valid PR without breaking repository manifests. */}

  <Step title="Fork & Clone" subtitle="Set up your local copy">
    Fork the repository on GitHub and clone it locally:
    ```bash
    git clone [https://github.com/YOUR_USERNAME/driverdex.git](https://github.com/YOUR_USERNAME/driverdex.git)
    cd driverdex
    ```
  </Step>

  <Step title="Create a Feature Branch" subtitle="Isolate your changes">
    Create a descriptive branch for your feature or fix:
    ```bash
    git checkout -b feature/inf-parser-optimization
    # or for driver updates
    git checkout -b drivers/update-realtek-net
    ```
  </Step>

  <Step title="Run Local Verification" subtitle="Test manifest integrity">
    Before committing, run a pre-flight test with DriverDex Builder to ensure manifests (`driverdex_index.json` and category `.manifest.json` files) parse cleanly:
    ```bash
    python driverdex_builder.py --dry-run
    ```
  </Step>

  <Step title="Commit with Clear Messages" subtitle="Keep commit history clean">
    Write concise commit messages describing what was changed:
    ```bash
    git commit -m "fix(builder): resolve HWID regex matching for dual-ID Bluetooth controllers"
    ```
  </Step>

  <Step title="Push and Open Pull Request" subtitle="Submit for review">
    Push your branch to GitHub and open a PR against the `main` branch. Provide a summary of testing steps in the PR description.
  </Step>
</Sequence>

---

## 4. Coding Standards

* **Python Version:** Compatible with **Python 3.7+**.
* **Dependencies:** Keep external dependencies minimal. Standard library modules are preferred unless heavy performance requirements dictate otherwise (e.g., `7z` system binary calls).
* **Git Commit Granularity:** When adding driver sets, follow AutoSync's pattern by committing **one driver group/category at a time** to ensure transparent git history.
* **Manifest Protection:** Never manually hand-edit version hashes inside `drivers.db` or `driverdex_index.json` unless fixing a corrupted index—let `driverdex_builder.py` generate these automatically.

---

## 5. Security & Vulnerability Reporting

If you find a security vulnerability, compromised driver binary, or credential leak:
* **Do NOT open a public issue.**
* Report it directly to the maintainer via private security advisory or contact email listed in `README.md`.

Thank you for helping build a faster, cleaner driver repository!
