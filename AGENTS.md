# Fontivo-Fonts — Agent & Architecture Guide

## 1. Repository Scope & Mission
- **Repository:** [Ahmedfahmy8308/Fontivo-Fonts](https://github.com/Ahmedfahmy8308/Fontivo-Fonts)
- **Role:** Curated, deduplicated offline font library distributed as a single GitHub Release (`v1.0.3`: `10,939` unique fonts, `4,430` families, `15` packs, `864.01 MB` compressed).
- **Curated & Maintained By:**
  - **Eng. Mohamed Elashwah** — Brand Identity & Fonts ([Portfolio](https://mohamed-elashwah.me/))
  - **Eng. Ahmed Fahmy** — Technology & Programming ([GitHub](https://github.com/Ahmedfahmy8308) • [Portfolio](https://ahmed-fahmy.engineer/))

## 2. Ecosystem Relationships & Links
| Repo | Purpose | Link |
| :--- | :--- | :--- |
| **Fontivo-Fonts** (this repo) | 15 ZIP packs + SHA-256 manifest (`v1.0.3`) | `https://github.com/Ahmedfahmy8308/Fontivo-Fonts` |
| **Fontivo-App** | Downloads, verifies SHA-256, extracts & activates packs in RAM (Desktop **`v1.1.1`**) | `https://github.com/Ahmedfahmy8308/Fontivo` |
| **Fontivo-Site** | Displays live pack catalog, counts & release metadata | `https://github.com/Ahmedfahmy8308/Fontivo-Site` |

## 3. Cross-Repo Ripple Rules (What Must Sync Elsewhere)
Whenever a new release or pack update is published in `Fontivo-Fonts`:
1. **Update `Fontivo-App`:**
   - Update `FontPackManifest::default_manifest()` in `Fontivo-App/src/fontivo_engine/downloader.rs` (tag, URLs, SHA-256, byte sizes, font counts).
   - Update assertions in `Fontivo-App/tests/server/catalog.rs`.
2. **Update `Fontivo-Site`:**
   - Update `FONTIVO_FONTS_VERSION`, `FONTIVO_FONTS_TOTAL`, `FONTIVO_FONTS_FAMILIES`, `FONTIVO_FONTS_PACKS`, `FONTIVO_FONTS_SIZE_FORMATTED` in `Fontivo-Site/src/config/version.ts`.
   - Update `fontsLibrary` in `Fontivo-Site/public/api/manifest.json` and `Fontivo-Site/public/llms.txt`.
   - Rebuild `Fontivo-Site` (`npm test; npm run build`).

## 4. What Can & Cannot Be Modified (Scope Guardrails)
- **Allowed:**
  - Pack curation, deduplication, metadata indexing, SHA-256 checksum generation, and release packaging.
- **Forbidden / Non-Negotiable Rules:**
  - **Single Consolidated Release:** Keep packs consolidated under a single active release tag (currently `v1.0.3`) — do NOT scatter packs across fragmented partial releases.
  - **Exact Script Coverage:** Only verified Latin, Arabic, Persian, and Urdu packs; no unverified or unsupported script stubs (`Hebrew`, `Indic`, `Symbols` were removed).
  - **Archive Security:** All `.zip` packs must use flat/safe relative paths (no `..` traversal / Zip-Slip) and match published SHA-256 hashes.
