<div align="center">



# DriverDex
### is an automation tool for managing
*A structured repository of Windows drivers.*

**Drivers:** ![Drivers](https://img.shields.io/badge/drivers-9215-brightgreen?style=flat-square) &nbsp;|&nbsp; **Workflow:** [![D1 Sync & Deploy](https://github.com/rhshourav/driverdex/actions/workflows/d1-sync.yml/badge.svg)](https://github.com/rhshourav/driverdex/actions/workflows/d1-sync.yml) &nbsp;|&nbsp; **Page:** [![Pages](https://img.shields.io/website?url=https%3A%2F%2Fdriverdex.pages.dev&label=driverdex.pages.dev&logo=cloudflare)](https://driverdex.pages.dev) &nbsp;|&nbsp; **Worker:** [![Worker](https://img.shields.io/website?url=https%3A%2F%2Fapp.driverdex.workers.dev&label=app.driverdex.workers.dev&logo=cloudflare)](https://app.driverdex.workers.dev) &nbsp;|&nbsp; **Worker:** [![Check Worker](https://img.shields.io/endpoint?url=https%3A%2F%2Fdriverdex-check.driverdex.workers.dev%2Fbadge&logo=cloudflare&label=driverdex-check.driverdex.workers.dev)](https://driverdex-check.driverdex.workers.dev)

<img src="banner/banner.png" alt="DriverDex Banner" />
</div>









**DriverDex Builder** is an automation tool for managing **DriverDex (Large Driver Collection)**, a structured repository of Windows drivers. It scans driver packs, classifies them by device type, compresses them into upload-friendly archives, updates manifests, and pushes everything to GitHub with version tracking.

DriverDex now includes a second companion tool, **DriverDex AutoSync**, which can scan the local machine’s installed OEM drivers, compare them against the repo, and upload only new or outdated drivers automatically.

---

## 📦 What is DriverDex?

**DriverDex (Large Driver Collection)** is a centralized collection of Windows drivers designed to be:

- 📚 Organized by driver type such as Network, Audio, Display, Storage, USB, Bluetooth, and more
- 🔄 Continuously updated with version tracking and supersession history
- ⚡ Easy to deploy and integrate
- 🧾 Indexed through manifest files and a repository index

---

## 🧰 Tools in this repository

### **DriverDex Builder**
For curated driver packs, packaging, manifest generation, and GitHub publishing.

### **DriverDex AutoSync**
For fully automatic sync from a local PC’s installed drivers to the repository. It:
- scans the local DriverStore,
- compares driver versions against the repo,
- uploads only **NEW** and **OUTDATED** drivers,
- skips already current drivers,
- commits one driver group at a time.

---

## ✨ Features

### DriverDex Builder
- 🔍 **Deep INF parsing**
  - Extracts provider, version, HWIDs, OS targets, architecture, and descriptions

- 🧠 **Smart classification**
  - Uses INF `Class=` data first, then HWID patterns as a fallback

- 📁 **Installer package support**
  - Archives full installer directories when EXEs live beside DLLs/config files
  - Keeps sibling files together so nothing gets left behind

- 🗜️ **Automatic packaging**
  - Builds optimized `.7z` archives
  - Splits archives into 15 MB volumes to avoid GitHub blob limits
  - Uses fast LZMA2 compression, with BCJ filtering when helpful

- 🧾 **Manifest system**
  - Generates per-category manifests such as `Net.manifest.json`, `Audio.manifest.json`, and `Firmware.manifest.json`
  - Maintains `driverdex_index.json` for discovery
  - Tracks version history and superseded entries

- 🔁 **Version control logic**
  - Detects newer and older drivers
  - Marks old versions as deprecated instead of deleting them

- ⚠️ **Duplicate and conflict detection**
  - Pre-flight audit with options to skip, replace, or keep both

- 📤 **GitHub automation**
  - Auto-detects the repo
  - Commits and pushes via GitHub REST + LFS
  - Handles retries and safe rebasing behavior

- 🚄 **Pipeline mode**
  - Archives and uploads driver groups in an interleaved flow to reduce peak disk usage

- 🧠 **Session tracking**
  - Persists run state so partially completed sessions can resume more safely

### DriverDex AutoSync
- 🖥️ **Local driver discovery**
  - Reads installed OEM drivers from the local Windows DriverStore

- 🔎 **Repo comparison**
  - Marks drivers as **NEW**, **OUTDATED**, or **UP-TO-DATE**

- 📦 **Selective upload**
  - Uploads only new and outdated drivers
  - Skips drivers already present at the same or newer version

- 🧵 **Parallel upload**
  - Uses the parallel upload path for fast syncing

- 📝 **Per-driver commits**
  - Commits one driver group at a time for clearer history

- 📣 **Telemetry**
  - Sends concise status updates for scan, upload, and completion events

---

## 🛠️ Requirements

- Python **3.7+**
- Git installed and available in `PATH`
- GitHub SSH access configured
- A valid GitHub token for repository API access when running the builder or sync tool
- Windows for DriverDex AutoSync, since it reads the local DriverStore

---

## ⚡ Quick Start

### 🔹 PowerShell (recommended)

```powershell
irm https://raw.githubusercontent.com/rhshourav/driverdex/main/get-driverdex.ps1 | iex
```

### 🔹 Manual setup

```bash
git clone https://github.com/rhshourav/driverdex.git
cd driverdex
python driverdex_builder.py
```

---

## 🔄 Typical workflow

1. Verify environment requirements
2. Load manifests and repository index
3. Scan driver folder for `.inf` files
4. Extract metadata and classify drivers
5. Compress into `.7z` packages
6. Audit duplicates and version conflicts
7. Update manifests and index
8. Commit and push to GitHub

For **DriverDex AutoSync**, the flow is:

1. Download repo manifests
2. Scan installed local drivers
3. Compare versions against the repo
4. Upload only new and outdated drivers
5. Commit each driver group to GitHub

---

## 📁 Repository structure

```text
drivers/
  ├── Net/
  ├── Display/
  ├── Audio/
  ├── Storage/
  └── ...
manifests/                       # all manifests live here
  ├── drivers.manifest.json      # legacy / single-manifest support
  ├── installers.manifest.json   # installer archive metadata
  ├── Net.manifest.json          # per-category manifests (REST builder / AutoSync)
  ├── Display.manifest.json
  └── ...
driverdex_index.json                  # manifest index (references manifests/*.json)
drivers.db                      # SQLite index
README.md
driverdex_builder.py
driverdex_autosync.py
```

---

## 🔐 GitHub SSH setup

DriverDex Builder uses SSH for Git operations.

### Windows

```powershell
ls ~/.ssh/id_*.pub
ssh-keygen -t ed25519 -C "your_email@example.com"
Start-Service ssh-agent
ssh-add ~/.ssh/id_ed25519
type $env:USERPROFILE\.ssh\id_ed25519.pub
```

Add the key in GitHub SSH settings.

### Linux

```bash
ls ~/.ssh/id_*.pub
ssh-keygen -t ed25519 -C "your_email@example.com"
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

Add the key in GitHub SSH settings.

### macOS

```bash
ls ~/.ssh/id_*.pub
ssh-keygen -t ed25519 -C "your_email@example.com"
eval "$(ssh-agent -s)"
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
pbcopy < ~/.ssh/id_ed25519.pub
```

Add the key in GitHub SSH settings.

### Test connection

```bash
ssh -T git@github.com
```

Expected output:

```text
Hi <your-username>! You've successfully authenticated...
```

### Troubleshooting

**Permission denied (publickey)**  
Your SSH key is not added to GitHub.

**Connection timeout**  
Try port 443:

```sshconfig
Host github.com
  Hostname ssh.github.com
  Port 443
```

**Multiple keys issue**

```sshconfig
Host github.com
  User git
  IdentityFile ~/.ssh/id_ed25519
```

---

## ⚠️ Disclaimer

**DriverDex (Large Driver Collection)** is a community-driven repository that aggregates and organizes publicly available Windows drivers.

- This project does **not create or modify drivers**.
- All drivers remain the property of their respective vendors.
- Drivers are distributed for convenience, archival, and deployment purposes only.

### No warranty

- All drivers are provided **as-is** without any warranty.
- The maintainers of DriverDex are not responsible for any damage, data loss, or system issues caused by using these drivers.
- Users are responsible for verifying compatibility with their hardware.

### Security notice

- Drivers are **not guaranteed to be virus-free**
- Scan files before use
- Prefer official vendor sources when possible

### Legal

- If you are a copyright owner or vendor and want a driver removed, open an issue or contact the repository owner.
- Any content found to violate rights will be removed promptly.

### Usage responsibility

By using this repository, you agree that you understand the risks of installing drivers and take full responsibility for your system.

---

## 👨‍💻 Author

**rhshourav**  
GitHub: https://github.com/rhshourav
