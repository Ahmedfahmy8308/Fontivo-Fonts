# Changelog

All notable changes to the Fontivo Fonts distribution infrastructure will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-20

### Added
- Initial stable release of the curated Fontivo Fonts distribution infrastructure.
- 11,207 verified open-source font files organized across 4,438 typographic families.
- 15 deterministic, highly compressed package archives (`fontivo-essential-001.zip` through `015.zip`, ~611 MB total).
- 7 comprehensive typographic script categories: Latin (6,823), Urdu (1,581), Persian (1,264), Arabic (946), Hebrew (79), Indic (47), and Symbols (467).
- Decoupled, content-addressed delivery model with SHA-256 integrity verification.
- Canonical JSON distribution manifest (`Manifests/manifest.json`).
- Python repository management suite:
  - `build.py`: Deterministic, incremental package builder.
  - `update.py`: Safe font lifecycle management CLI (`add`, `remove`, `modify`, `metadata`).
  - `scan.py`: Disk-to-state integrity verification tool.
  - `publish-release.py`: Release packaging and publishing automation tool.
  - `verify.py`: 10-scenario acceptance test suite.
- Security hardening across all tools: Path Traversal defense, Zip Slip defense, and command injection guards.
