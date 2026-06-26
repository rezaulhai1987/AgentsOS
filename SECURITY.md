# Security

## Reporting

Open a private security advisory at
https://github.com/rezaulhai1987/AgentsOS/security/advisories/new
or email the maintainer via the GitHub profile.

## Tokens

Never commit tokens. The runtime reads `GITHUB_TOKEN` from the
environment; rotate any token that lands in chat, logs, or commits.

## Sandboxing

The `process` sandbox backend has no isolation. Use the `docker`
backend (v0.5) for any untrusted code execution.