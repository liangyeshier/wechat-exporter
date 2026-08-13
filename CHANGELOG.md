# Changelog

All notable changes to this project are documented here.

## Unreleased

## 0.2.0 - 2026-08-13

- Replace the partially blurred real-data screenshot with a fully synthetic demo screenshot.
- Add a comprehensive Chinese disclaimer and stricter documentation privacy guidance.
- Rework HTML exports into a closer WeChat-style conversation layout with
  dedicated message cards and responsive desktop/mobile rendering.
- Preserve conversation/account names, remarks, nicknames, WeChat IDs, internal
  IDs, sender identity, and stable 1-based message sequence numbers.
- Add ordered A4 PNG/PDF page footers (`page X of Y`) and page-counted filenames.
- Generate JSON/TXT archive manifests with a SHA-256 message chain and hashes for
  every exported artifact, with explicit legal-admissibility limitations.
- Show a complete local date and second-level time on every message, while
  preserving ISO 8601, UTC-offset, and Unix timestamps for machine verification.
- Fix A4 page breaks so a message timestamp can never be drawn on the previous page.

## 0.1.0 - 2026-08-13

- Add a packaged Apple Silicon macOS application and reproducible build scripts.
- Add current WeChat 4.1+ key setup using a separately copied, ad-hoc-signed app and LLDB.
- Add friend, group, and official-account filtering in the local web interface.
- Add key status, update, progress, and application exit controls.
- Verify real local account discovery, group listing, message preview, and TXT/CSV export.
- Add privacy guidance, security reporting, release scripts, and sensitive-artifact exclusions.
