# Security policy

CogniStore is an alpha project. Public source availability and passing CI do
not establish production qualification, deployment approval or pilot acceptance.
See the [release candidate guide](docs/release_candidate.md) for the remaining
qualification requirements and the scope of retained evidence.

## Reporting a vulnerability

Report suspected vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/melliott18/CogniStore/security/advisories/new).
Do not put exploit details, credentials, customer data or other sensitive
information in a public issue or pull request.

Include the affected commit or version, component, expected and observed
behavior, and a minimal reproduction using synthetic data and temporary local
storage. Describe any required configuration and known impact. Remove real
tokens, keys and personal data from logs and attachments.

The maintainer reviews reports against current `main` and coordinates fixes
and disclosure privately. Older versions may require an upgrade; no supported
production release or response-time commitment is currently advertised.
