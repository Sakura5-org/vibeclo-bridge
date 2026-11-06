# Security

The bridge runs Claude Code on your computer for the devices you paired, so we take reports seriously.

**Please don't open a public issue for a vulnerability.** Email **support@sakura5.app** with the subject
"Security: Vibeclo bridge", a description, steps to reproduce and the bridge version (`clo2 status` or
`~/.clo2/app/VERSION`). We aim to reply within 3 days and to ship a fix through the signed auto-update.

In scope: the bridge (`bridge/`), the relay (`relay/`) and the installer (`install.sh`) — for example pairing without
the owner's approval, reading or injecting traffic, running commands from an unpaired device, bypassing the release
signature or the update rollback, or leaking secrets stored by the bridge.

Out of scope: what Claude Code itself does with the permissions you grant it (Standard / Full access), and attacks that
need an already-compromised computer or a paired device you control.
