# DriverDex Security Policy

Due to the nature of **DriverDex** (handling Windows system drivers, automated `.7z` packaging, hardware identification, and local `DriverStore` synchronization), project security is paramount. We take driver integrity, binary safety, and automated repository operations seriously.

---

## 1. Supported Versions

Security updates and vulnerability patches are actively maintained for the following components:

| Component | Version / Target | Supported |
| :--- | :--- | :--- |
| **DriverDex Builder** | Latest `main` branch | :white_check_mark: Supported |
| **DriverDex AutoSync** | Latest `main` branch | :white_check_mark: Supported |
| **Driver Manifests & Index** | Current Schema (`driverdex_index.json`) | :white_check_mark: Supported |
| Legacy / Unversioned Manifests | Older single-manifest formats | :x: Unsupported |

---

## 2. Reporting a Vulnerability or Malicious Driver

If you discover a security issue—such as a vulnerability in DriverDex execution scripts, compromised credentials, or a suspicious/malicious driver binary within the repository—**do NOT open a public GitHub Issue, PR, or Discussion.**

### Preferred Reporting Channel
* **Private Security Advisory:** Open a [Private Security Advisory](https://github.com/rhshourav/driverdex/security/advisories/new) via the GitHub Security tab.
* **Direct Email Contact:** Contact the maintainers privately at `security@driverdex.local` (or the repository owner's primary email).

### Information to Include
When submitting a report, please provide:
1. **Description of the Issue:** Detail the vulnerability or the flagged driver binary (including HWIDs, driver version, and folder path).
2. **Steps to Reproduce:** Exact steps, scripts, or environment setups required to trigger the exploit or behavior.
3. **Impact Assessment:** Potential risks to local Windows environments, DriverStore, or GitHub LFS storage.
4. **Suggested Mitigation (Optional):** Proposed code fixes or driver deletion steps if known.

---

## 3. Vulnerability Response Timeline

Upon receiving a valid report, the maintainers will adhere to the following workflow:

* **Initial Acknowledgment:** Within **24–48 hours** confirming receipt of the report.
* **Assessment & Verification:** Within **5 business days** to verify findings, evaluate severity, and isolate affected driver packages or code modules.
* **Remediation & Patching:** High-severity vulnerabilities or compromised drivers will be fixed, removed, or revoked immediately via a dedicated release or hotfix commit.
* **Public Disclosure:** Coordinated public disclosure will take place after the patch/removal is deployed.

---

## 4. Driver Security & Binary Integrity Policy

Because DriverDex indexes and aggregates driver files for Windows operating systems, all submissions and automated sync operations must abide by these strict security rules:

### Unmodified Vendor Binaries Only
* **No Repacked Executables:** All `.inf`, `.sys`, `.dll`, and `.cat` files must remain strictly in their original vendor-signed state.
* **Prohibition of Injected Files:** Adding third-party helper binaries, unverified setup wrappers, or custom scripts into driver directories is strictly forbidden.

### Tamper-Evident Manifests & Signatures
* **Catalog Validation:** Digital signatures (`.cat` files) must match the driver binaries.
* **Version & HWID Accuracy:** Intentionally altering `.inf` target strings or version strings to bypass DriverDex AutoSync supersession logic is classified as malicious behavior.

### Credential & Token Protection
* **GitHub Tokens & SSH Keys:** Never commit GitHub personal access tokens (PATs), private SSH keys (`id_ed25519`), or automated workflow credentials into the repository or local logs.
* **Safe Environment Variables:** Always pass repository authentication tokens via secure environment variables or local SSH agent configurations.

---

## 5. Security Best Practices for Users

While DriverDex automates driver distribution, users and administrators are advised to:
1. **Pre-scan Archives:** Always run an up-to-date anti-virus scanner on downloaded driver volumes before installing them on live hardware.
2. **Verify Hardware Compatibility:** Double-check driver HWIDs in `driverdex_index.json` before performing automated push or silent deployments.
3. **Use Official Vendors for Critical Systems:** In mission-critical environments, prefer acquiring drivers directly from the official hardware OEM.
