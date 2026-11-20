# Contributing to Fontivo Fonts

Thank you for your interest in contributing to Fontivo Fonts!

## Guidelines for Font Submissions

1. **Permissive Licensing**:
   All submitted fonts must be released under an open-source license:
   - SIL Open Font License (OFL 1.1)
   - Apache License 2.0
   - Ubuntu Font License (UFL)
   - Public Domain / CC0

2. **Technical Standards**:
   - Files must be valid OpenType (`.otf`), TrueType (`.ttf`), or OpenType collections (`.ttc`).
   - Binaries must pass validation by `fontTools` without table corruption.
   - Files should ideally be under 5 MB to maintain lightweight package distribution.

3. **Submitting via CLI**:
   Maintainers can add new fonts using:
   ```bash
   python Scripts/update.py add path/to/font.ttf --category <Category>
   ```

4. **Testing Before Submitting a PR**:
   Run the repository acceptance test suite:
   ```bash
   python Scripts/verify.py
   ```
   Ensure all 10 tests pass before opening a Pull Request.

## Reporting Issues

- For font rendering issues or licensing inquiries, open a GitHub Issue with sample text and font family name.
- For security vulnerabilities, refer to [SECURITY.md](SECURITY.md).
