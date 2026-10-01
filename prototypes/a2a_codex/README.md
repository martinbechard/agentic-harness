# Superseded A2A investigation (incomplete)

Work stopped before an A2A host or live run when Martin selected an ACP investigation.
`codex_agent.py` is an untested partial app-server bridge, not a runnable A2A agent.
The active prototype is `../acp_codex` and uses the maintained codex-acp adapter.
Do not use this partial code for dispatch.

The official A2A specification provides JSON-RPC/HTTP, HTTP+JSON, and gRPC
bindings; it does not define standard stdio. ACP does support standard stdio.
Source: https://a2a-protocol.org/latest/topics/custom-protocol-bindings/
