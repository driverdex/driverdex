# DriverDex Privacy Policy

_Last updated: 2026-06-28_

DriverDex is a community-maintained driver database and installer. This document covers two separate things the installer does, because they have different privacy implications:

1. **Driver lookup** — happens automatically every time you run the script. Required for the tool to function.
2. **Database contribution** — happens only if you explicitly opt in when prompted. Never automatic, never silent.

If anything below ever stops matching what the script actually does, the code is the source of truth — please open an issue.

---

## 1. Driver lookup (always-on, required)

To find matching drivers, the installer reads the **hardware IDs** of devices on your system (strings like `PCI\VEN_8086&DEV_1234`) and sends them, one at a time, to the DriverDex API:

```
https://driverdex-check.driverdex.workers.dev/api/hwid/{hardware-id}
```

That's the entire request — a hardware ID string, nothing else. No installed-driver versions, device names, OS version, or system info are sent at this stage. The API responds with whatever matching driver packages exist; nothing is written to the database from this step.

If a match is selected for install, the driver package itself is downloaded from GitHub (`raw.githubusercontent.com`, `github.com`) — standard file downloads, same as opening any public URL.

## 2. Database contribution (opt-in only)

If your hardware has no match, or has a local driver the database doesn't know about, the script will ask — every time, with a default answer you can simply decline. If you say no, nothing further happens and the script doesn't ask again that session.

If you say yes, you're shown a preview of what would be sent **before** anything is transmitted, then choose how the contribution tool runs (interactive walkthrough, or a quick unattended pass).

**What is collected, if you opt in:**

|
 Collected 
|
 Why 
|
|
---
|
---
|
|
 Hardware IDs (
`PCI\VEN_...`
, 
`USB\VID_...`
) 
|
 Identifies the device 
|
|
 Device category & friendly name (from Windows) 
|
 Human-readable context for reviewers 
|
|
 Currently installed driver version, provider, and date 
|
 The actual data being contributed 
|
|
 Windows version & CPU architecture 
|
 Flags compatibility for future matches 
|

**What is never collected, at any point:**

- Your name, username, or any account information — DriverDex doesn't use accounts
- Files, documents, browsing history, or anything outside the device list above
- Serial numbers, MAC addresses, or other unique-to-you device identifiers
- Anything from devices you didn't select / weren't shown in the preview

**On IP addresses, specifically:** the contribution payload itself contains no IP or location field — that's not something the app collects or stores. That said, any HTTPS request inherently passes through standard web infrastructure (Cloudflare, GitHub) that may keep transient connection logs as a normal part of running a web service — same as visiting any website. DriverDex doesn't access, request, or retain those logs, but we can't make promises on behalf of third-party infrastructure providers. If that distinction matters for your use case, you're welcome to route the contribution through a VPN or proxy — nothing in the tool depends on your real IP.

## Review process

Contributions are not added to the live database automatically. They go into a review queue and are checked by maintainers before being merged — both to catch bad data and to confirm nothing outside the listed fields snuck in. Accepted submissions are credited to "the DriverDex community" collectively, not to an individual contributor, unless you separately choose to identify yourself (e.g. in a GitHub PR).

## Local data

- A session log is written to `%TEMP%\DriverDex-<date>.log` on **your machine only**. It records what the script did (HWIDs scanned, install results, errors) for troubleshooting. It is never uploaded automatically — if you file a bug report, you choose what (if anything) to paste from it.
- Downloaded driver files land in the folder you choose at the "Save drivers to" prompt. Nothing is deleted without your input except the script's own temp/scratch directory.

## Third-party services involved

|
 Service 
|
 Purpose 
|
|
---
|
---
|
|
`driverdex-check.driverdex.workers.dev`
|
 Driver-matching API (Cloudflare Workers) 
|
|
`github.com`
 / 
`raw.githubusercontent.com`
|
 Hosts the script, driver packages, and contribution submission scripts 
|
|
 Git LFS batch endpoint 
|
 Resolves large driver-package downloads — receives only a file hash/size, not anything about you 
|

## Your choices

- Decline any contribution prompt — the installer still works fully for driver lookup/install either way.
- Re-run the script later and contribute then — there's no time pressure or one-shot window.
- Run the **TUI mode** contribution option if you want to review the exact payload before it's sent, every time.
- Open a GitHub issue if you believe data was submitted that shouldn't have been, or if you have questions this document doesn't answer.

## Changes to this policy

This is a community project — if the contribution flow changes, this file will be updated alongside it in the same repo, same commit where practical. Check the [commit history](https://github.com/rhshourav/driverdex/commits/main/PRIVACY.md) for changes over time.

## Contact

Open an issue at **https://github.com/rhshourav/driverdex/issues** for privacy questions, data-removal requests, or anything else covered above.
