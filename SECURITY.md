# Security Policy

## Supported versions

Only the latest GitHub release and the current `main` branch receive security fixes.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature when it is enabled. If it is
not available, contact the repository owner privately before opening a public issue.

Never attach or paste real WeChat databases, `keys.json`, passphrases, decrypted files,
contact identifiers, chat exports, or unredacted screenshots. A synthetic reproduction is
preferred. Include the application version, macOS version, WeChat version, and the smallest
redacted log needed to reproduce the issue.

Repository screenshots and documentation examples must use fully synthetic demo data. Do
not publish blurred or pixelated real screenshots: names, lengths, counts, ordering, and
surrounding UI state can still disclose private information.

## Security properties

- The web UI binds only to `127.0.0.1`.
- Original WeChat databases are opened read-only and immutable.
- Plaintext copies and key material remain outside the repository.
- The original `/Applications/WeChat.app` bundle is never modified by first-run setup.
- Captured passphrase material is held in a mode-`0600` temporary file and removed after
  per-database keys have been derived and verified.

The tool does not protect exports from other local users or malware that can read the same
macOS account. Users are responsible for FileVault, account access, backups, and deletion of
sensitive exports when no longer needed.
