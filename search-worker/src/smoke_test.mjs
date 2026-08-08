import { tokenize, scoreCandidate, hwidTruncationLevels, looksLikeHwid, looksLikeDriverId } from "./index.js";

const rows = [
  {
    driver_id: "drv-a1b2c3d4e5f6",
    display_name: "AMD Radeon RX 5700 XT",
    provider: "AMD",
    category: "Display",
    driver_type: "GPU",
    pack: "AMD_Radeon_x64",
    version: "24.3.1",
    arch: "x64",
    descriptions: '["AMD Radeon RX 5700 XT","AMD Radeon Graphics"]',
    os_targets: '["Windows 10/11"]',
    source_manifest: "Display.manifest.json",
  },
  {
    driver_id: "drv-9b879c133031d3f9",
    display_name: "HID Global Crescendo C2300 iCLASS",
    provider: "HID Global",
    category: "SmartCard",
    driver_type: "Security",
    pack: "ViewSonic_Monitor_x64",
    version: "1.1.0.42",
    arch: "x64",
    descriptions:
      '["HID Global","HID Global Crescendo C2300","HID Global Crescendo C2300 iCLASS","HID Global Crescendo Key"]',
    os_targets: '["Windows 10/11"]',
    source_manifest: "SmartCard.manifest.json",
  },
  {
    driver_id: "drv-nvidia123456",
    display_name: "NVIDIA GeForce RTX 4070",
    provider: "NVIDIA",
    category: "Display",
    driver_type: "GPU",
    pack: "NVIDIA_x64",
    version: "552.44",
    arch: "x64",
    descriptions: '["NVIDIA GeForce RTX 4070"]',
    os_targets: '["Windows 11"]',
    source_manifest: "Display.manifest.json",
  },
];

function assert(cond, msg) {
  if (!cond) throw new Error("FAIL: " + msg);
  console.log("ok  -", msg);
}

// 1. Multi-token, any order -> same candidate found either way.
const a = tokenize("amd radeon 5700");
const b = tokenize("5700 radeon amd");
const scoredA = rows.map((r) => scoreCandidate(r, a)).map((s) => s >= 0);
const scoredB = rows.map((r) => scoreCandidate(r, b)).map((s) => s >= 0);
assert(JSON.stringify(scoredA) === JSON.stringify(scoredB), "token order does not change which rows match");
assert(scoredA[0] === true && scoredA[1] === false && scoredA[2] === false, "amd+radeon+5700 matches only the AMD row");

// 2. Typo tolerance: "radoen" (typo for radeon) should still match the AMD row.
const typoTokens = tokenize("radoen rx 5700");
const typoScore = scoreCandidate(rows[0], typoTokens);
assert(typoScore > 0, "'radoen' (typo) still matches 'Radeon' via bounded Levenshtein");

// 3. A driver with empty hwids must still be findable via descriptions text.
const crescendoTokens = tokenize("crescendo iclass");
assert(scoreCandidate(rows[1], crescendoTokens) > 0, "HID Global Crescendo (no hwids) found via descriptions text");

// 3b. Multi-criteria across different fields: vendor + category, any order.
const hidTokens = tokenize("smartcard hid");
assert(scoreCandidate(rows[1], hidTokens) > 0, "'smartcard hid' matches via category + provider, order-independent");

// 4. A token that matches nothing should exclude the record (AND across tokens).
const nonsense = tokenize("amd banana");
assert(scoreCandidate(rows[0], nonsense) === -1, "unrelated extra token ('banana') correctly excludes the record");

// 5. NVIDIA typo + version fragment, multi-criteria in a different order.
const nvidiaTokens = tokenize("4070 nvida");
assert(scoreCandidate(rows[2], nvidiaTokens) > 0, "'4070 nvida' (typo'd vendor + model number) matches the NVIDIA row");

// 6. HWID truncation ladder.
const levels = hwidTruncationLevels("PCI\\VEN_10DE&DEV_2684&SUBSYS_408A1043&REV_A1");
assert(levels[0] === "PCI\\VEN_10DE&DEV_2684&SUBSYS_408A1043&REV_A1", "level 0 is the exact full hwid");
assert(levels[levels.length - 1] === "PCI\\VEN_10DE", "last level is the generic vendor+device compatible ID");

// 7. Mode detection.
assert(looksLikeHwid("PCI\\VEN_10DE&DEV_2684") === true, "hwid-shaped query is detected as hwid mode");
assert(looksLikeDriverId("drv-a1b2c3d4e5f6") === true, "drv-... query is detected as driver-id mode");
assert(looksLikeHwid("nvidia 552 win11") === false, "plain text query is NOT misdetected as hwid mode");

console.log("\nAll smoke tests passed.");
