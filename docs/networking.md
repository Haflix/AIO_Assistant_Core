# Networking

*Last updated for AIO Assistant Core 0.22.0*

PluginCore ships with an optional `NetworkManager` that bridges plugin calls between nodes over an mTLS-pinned TCP protocol. With networking enabled, calling `await self.execute("OtherPlugin", ...)` works whether `OtherPlugin` is on this node or another node. The same applies to `publish_event` and `request_event`.

This document covers the trust model, peer configuration, per-node port handling, how remote dispatch flows, and the wire protocol for advanced readers.

For the full configuration block reference, see [configuration](./configuration.md). For local notifier semantics that the remote `publish_event` / `request_event` paths extend, see [notifier](./notifier.md).

---

## Trust model

Every node holds its own self-signed certificate. Peers trust each other by **certificate pinning** on the SubjectPublicKeyInfo (SPKI) — there is no CA, no chain, no DNS-name verification. Each node's `peers:` config lists the exact other nodes it accepts.

- Each node has `cert.pem` and `key.pem` in `keys_dir` (default `_keys/`). When neither file is present, NetworkManager generates a self-signed pair via `_load_or_generate_identity` (`networking.py:556-630`).
- The fingerprint is the sha256 of the SPKI DER, prefixed with `sha256:`. NetworkManager derives this at parse time from each peer's cert PEM.
- mTLS contexts are built by `_create_server_ssl_context` / `_create_client_ssl_context` / `_create_pinned_ssl_context` (`networking.py:652-678`). Both sides require client certificates.
- On every inbound connection, `_handle_client` (`networking.py:1146-1148`) extracts the peer's SPKI fingerprint as its FIRST act, BEFORE any protocol message is read or sent, and looks the fingerprint up in `peers_by_fingerprint`. A pin failure closes the socket silently — no `_send_message` ever fires on a non-pinned peer.
- `start()` (`networking.py:949-957`) hard-errors if `peers:` is empty when networking is enabled, because an empty trust store would reject every inbound connection with an opaque OpenSSL error.

The legacy `node_ips:` schema is removed. Presence of `node_ips:` in a config raises `RuntimeError` at boot with migration guidance pointing at the `peers:` schema.

The legacy top-level networking config fields `secret`, `cert_file`, and `key_file` (from earlier releases) are silently accepted but ignored after the PR4 K-3 cert-pinning rework — fingerprint pinning replaces them.

---

## Peer configuration

The `networking.peers:` list is the authoritative trust store. Each entry binds a peer hostname to an IP/port, a certificate, and an optional fingerprint assertion.

```yaml
networking:
  enabled: true
  port: 2510                  # cluster default port; per-peer overrides allowed
  hostname: ""                # empty = socket.gethostname()
  keys_dir: "_keys"           # where this node's cert.pem / key.pem live
  pool_size: 5                # connection-pool depth per (ip, port)
  discover_nodes: true
  direct_discoverable: true
  auto_discoverable: false

  peers:
    - hostname: alpha
      address: "10.0.0.1"
      cert_file: "_keys/peers/alpha.pem"
      fingerprint: "sha256:abcd...ef"  # optional; if set must match derived
      system_caller: false             # optional

    - hostname: beta
      address: "10.0.0.2:2511"          # per-peer port
      cert_pem: |
        -----BEGIN CERTIFICATE-----
        MIIBIjANBg...
        -----END CERTIFICATE-----

    - hostname: gamma
      address: "[fe80::1]:2510"         # bracketed IPv6
      cert_file: "_keys/peers/gamma.pem"
```

| Field            | Type    | Required | Notes                                                                                                          |
|------------------|---------|----------|----------------------------------------------------------------------------------------------------------------|
| `hostname`       | `str`   | yes      | Peer's logical name. Used as the routing key in the advert tables.                                             |
| `address`        | `str`   | yes      | `"ip"`, `"ip:port"`, `"[ipv6]"`, or `"[ipv6]:port"`. Bare IPv6 (multiple colons, no brackets) defaults to cluster port. |
| `cert_file`      | `str`   | yes (one of) | Path to the peer's PEM cert. Resolved relative to `keys_dir.parent` if not absolute.                        |
| `cert_pem`       | `str`   | yes (one of) | Inline PEM body. Use a YAML block scalar. Cannot be combined with `cert_file`.                              |
| `fingerprint`    | `str`   | optional | `sha256:<hex>`. If set, must match the derived fingerprint, else `RuntimeError` on parse.                     |
| `system_caller`  | `bool`  | optional | Default `false`. When `true`, this peer's calls inherit `"system"` author privileges (see [notifier](./notifier.md)). |

Validation errors that surface at parse time:

- Missing `hostname` or `address`.
- Both `cert_file` and `cert_pem` present.
- Neither `cert_file` nor `cert_pem` present.
- Cert content does not start with `-----BEGIN CERTIFICATE-----` after stripping.
- Cert content is not valid PEM-encoded X.509.
- Declared `fingerprint` does not match derived.
- Duplicate fingerprint across the peers list.
- Duplicate `(ip, port)` across the peers list.
- Bracketed IPv6 with empty brackets, missing closing bracket, or non-integer port.

A printable fingerprint helper is available via the networking CLI for paste-into-config workflows.

---

## Per-node port

Most clusters use one port for everything (`networking.port`, default `2510`). When you need otherwise (e.g. running a parent and a sub-node on the same machine), each peer entry's `address:` may override the port:

- `"10.0.0.1"` → `(10.0.0.1, cluster_default_port)`
- `"10.0.0.1:2511"` → `(10.0.0.1, 2511)`
- `"[::1]:2510"` → `(::1, 2510)`

Connection pools are keyed by `(ip, port)` (`networking.py:189`), so a parent and sub-node on the same IP get separate pools. `Node` objects (`networking_classes.py:28-88`) carry an optional `port` field; `None` means "use cluster default".

Use cases:

- Two PluginCore instances on the same host (parent + isolated child node).
- A peer behind NAT exposing a non-standard port.

---

## Hostname-based routing

Peers are addressed by **hostname**, not IP. Internal advert tables (`networking.py:206-249`) all key on `hostname`, so reconnect or IP change does not invalidate them:

| Table                    | Shape                                              | Purpose                                                |
|--------------------------|----------------------------------------------------|--------------------------------------------------------|
| `_inbound_adverts`       | `Dict[hostname, Dict[sub_uuid, AdvertSub]]`        | What each peer told us about its subscriptions.        |
| `_inbound_global_order`  | `Dict[(hostname, sub_uuid), AdvertSub]`            | Insertion-ordered for `request_event` fall-through.    |
| `_outbound_adverts`      | `Dict[hostname, Dict[our_sub_uuid, AdvertSub]]`    | What we have already told each peer.                   |

This stays stable across reconnects and IP changes. As long as the hostname matches the manifest, advert state survives.

---

## Discovery and heartbeat

Three flags control discovery behaviour:

| Flag                  | Default | Effect                                                                                                              |
|-----------------------|---------|---------------------------------------------------------------------------------------------------------------------|
| `discover_nodes`      | `false` | Toggles the periodic `update_all_nodes` loop. When `true`, the node periodically retries dead/missing peers.        |
| `direct_discoverable` | `false` | Peer can find this node when it explicitly knows the IP.                                                            |
| `auto_discoverable`   | `false` | Peer can find this node via subnet scan. Setting `true` auto-coerces `direct_discoverable=true`.                    |

Discovery does not bypass trust: a discovered peer still needs a matching cert / fingerprint in `peers:` to connect. Auto-discovery is helpful in development; in production, an explicit peer list is usually clearer.

Heartbeat parameters are currently hardcoded (`networking.py:202-204`):

| Knob                | Value     | Meaning                                                                |
|---------------------|-----------|------------------------------------------------------------------------|
| `heartbeat_interval`| `10.0` s  | How often to ping every peer.                                          |
| `lookup_interval`   | `60.0` s  | How often the node-lookup loop runs.                                   |
| `liveness_timeout`  | `30.0` s  | A peer is considered dead if its last heartbeat is older than this.    |

The heartbeat loop iterates the node list and calls `heartbeat_node(node, timeout=liveness_timeout)`. Failures route through `_mark_node_dead`, which drops advert state for that peer. `Node.is_alive(timeout=30)` returns `True` if the last heartbeat was within `timeout` seconds.

> Exposing `heartbeat_interval` / `lookup_interval` / `liveness_timeout` as user-tunable config keys is tracked as a pending framework improvement. Today these values are set in `NetworkManager.__init__` (`networking.py:202-204`) and cannot be overridden via YAML.

---

## The `remote: true` flag

A plugin endpoint is reachable from peer nodes if and only if BOTH conditions hold:

1. The plugin's manifest has top-level `remote: true`.
2. The endpoint's entry has `remote: true`.

`find_endpoint` (`PluginCore.py:2549-2556`) checks both. Forgetting either produces an "endpoint not found" error from a peer caller.

The `accessible_by_other_plugins` flag is NOT consulted for inbound peer requests — that flag only gates LOCAL cross-plugin access. So an endpoint can be `accessible_by_other_plugins: false` (only this plugin can call it locally) and still be `remote: true` (peers can call it across the wire).

---

## Remote `execute` flow

When `find_endpoint` finds the endpoint on a `RemotePlugin` proxy instead of a local plugin, `PluginCore._process_request` (`PluginCore.py:2660-2772`) takes the remote branch.

```
   Plugin A on Node alpha
     |
     | await self.execute("PluginX", "method", args=..., hosts="any")
     v
   PluginCore.execute (alpha)
     |
     | find_endpoint
     |     ├── try local plugins: no PluginX here
     |     └── iterate self.network.nodes:
     |             call network.node_has_endpoint(IP, "PluginX", "method")
     |             hit on beta
     | --> returns (RemotePlugin, endpoint_dict, Node(beta))
     |
     | self.network.execute_remote(IP=beta.IP, plugin="PluginX",
     |                              method="method", args=..., timeout=...,
     |                              request_id=request.id)
     |
     |  [MSG_EXECUTE (1)] --> beta
     |
     |  [MSG_RESULT (10)] OR [MSG_ERROR (12)] <-- beta
     |
     v
   return value (or RequestException / NetworkRequestException)
```

`request_id` round-trips so both sides correlate. Streaming execute uses `MSG_EXECUTE_STREAM` (id 2) and yields chunks via `MSG_STREAM_CHUNK` (id 11) with `MSG_END_STREAM` (id 13) as the terminator.

---

## Remote `request_event` flow

`request_event` is 1:1 with insertion-order tie-break. After failing to find a local match, the framework iterates `_inbound_global_order` in insertion order. For each candidate, the per-peer host filter, the sub-level remote-publisher filter, the author filter, and the topic-pattern match all apply.

```
   Local PluginCore.request_event
     |
     | find_first(topic) on local registry
     |     ├── hit -> dispatch locally, return
     |     └── miss -> proceed to remote candidates
     |
     | iterate self.network._inbound_global_order in insertion order:
     |     for each candidate sub on a peer:
     |         apply _hosts_match (peer-level)
     |         apply _sub_accepts_remote_publisher (sub.hosts vs author_host)
     |         apply _sub_accepts_author (authors / blocked_authors)
     |         apply _topic_matches (does the candidate's pattern match this topic)
     |         survivor -> try this peer:
     |             [MSG_REQUEST_EVENT (16)] --> peer
     |             [MSG_RESULT / MSG_ERROR] <-- peer
     |
     |             on NetworkRequestException -> SKIP, try next candidate
     |             on NoLocalSubException     -> SKIP, try next candidate
     |             on RequestException        -> propagate immediately
     |             on success                 -> return result
     |
     | exhausted candidates -> raise RequestException("no subscriber matched")
```

**Why distinguish `NoLocalSubException` from `NetworkRequestException`:** the former is "the peer was reachable, just didn't have a matching sub either" — fall-through is safe. A generic `RequestException` propagating from a peer means a matched handler ran and raised; surfacing that error is the correct behavior, because replacing it with another peer's response would silently mask a real failure.

`NoLocalSubException` is a subclass of `RequestException`, so callers that just `except RequestException:` see one type. The framework only distinguishes the two for fall-through control.

The streaming variant (`request_event_stream`, `MSG_REQUEST_EVENT_STREAM`, id 17) has identical fall-through semantics PRE-FIRST-CHUNK. Once the producer yields the first chunk, the dispatch is committed to that peer; later errors do not redirect.

---

## Remote `publish_event` flow

`publish_event` is 1:N fan-out. Local subs are dispatched in-process; for every advertised remote sub on every reachable peer that survives per-peer and sub-level filters, the framework spawns a tracked task that sends `MSG_PUBLISH_EVENT` (id 15) over the wire (`PluginCore.py:4178-4262`).

```
   Local PluginCore.publish_event
     |
     | dispatch locally to every matching local sub
     |
     | for every advertised sub on every reachable peer that survived
     |     per-peer + sub-level filters:
     |         spawn fire-and-forget network.publish_event_remote task
     |         [MSG_PUBLISH_EVENT (15)] --> peer
     |
     | tasks tracked in _inflight_publishes[hostname]
     |
     v
   return total scheduled count (local + remote)
```

Per-peer ordering is preserved (one TCP stream per peer). Across peers, dispatches happen concurrently. Errors in remote publishes are logged but never raised — `publish_event` is fire-and-forget. The return value is the count of subscribers (local + remote) the dispatch was scheduled for, not the count that completed successfully.

Inflight tasks tracked in `_inflight_publishes[hostname]` are cancelled when the corresponding peer disconnects, and drained on shutdown.

---

## Subscription advert protocol

Two message types keep peer subscription tables in sync:

| Wire ID | Name              | Purpose                                                                                                           |
|---------|-------------------|-------------------------------------------------------------------------------------------------------------------|
| 18      | `MSG_SUB_ADVERTISE` | Initial sub-snapshot exchange between peers (sent once per connection).                                         |
| 19      | `MSG_SUB_DELTA`     | Incremental subscribe/unsubscribe delta. Broadcast on every `_register_yaml_subscriptions` call after `plugin_lock` is released, and on every `subscribe_event` / `unsubscribe_event`. |

Add-deltas broadcast AFTER the local registration completes; remove-deltas broadcast AFTER local removal. Each peer applies the delta to its own `_inbound_adverts` and `_inbound_global_order`.

---

## Wire protocol message types

For tooling authors and protocol debuggers. Constants live at `networking.py:56-87`. Each message is length-prefixed and routed by type ID. Numbers are part of the wire format and must not be repurposed.

| ID | Name                       | Direction | Purpose                                                                       |
|----|----------------------------|-----------|-------------------------------------------------------------------------------|
| 1  | `MSG_EXECUTE`              | request   | Remote `execute` call.                                                        |
| 2  | `MSG_EXECUTE_STREAM`       | request   | Remote streaming `execute`.                                                   |
| 3  | `MSG_HAS_ENDPOINT`         | request   | Endpoint existence check (used by `find_endpoint` / `node_has_endpoint`).     |
| 4  | `MSG_PING`                 | request   | Heartbeat.                                                                    |
| 5  | `MSG_INFO`                 | request   | Discovery info exchange.                                                      |
| 6  | `MSG_FIND_TAGGED_ENDPOINTS`| request   | Tag-based discovery.                                                          |
| 10 | `MSG_RESULT`               | response  | Single response payload.                                                      |
| 11 | `MSG_STREAM_CHUNK`         | response  | One streamed chunk.                                                           |
| 12 | `MSG_ERROR`                | response  | Error response.                                                               |
| 13 | `MSG_END_STREAM`           | response  | Stream terminator.                                                            |
| 14 | `MSG_STREAM_ITEM_END`      | response  | End of one item in a stream response.                                         |
| 15 | `MSG_PUBLISH_EVENT`        | request   | Fire-and-forget publish.                                                      |
| 16 | `MSG_REQUEST_EVENT`        | request   | 1:1 request.                                                                  |
| 17 | `MSG_REQUEST_EVENT_STREAM` | request   | Streaming 1:1 request.                                                        |
| 18 | `MSG_SUB_ADVERTISE`        | request   | Initial sub-snapshot exchange between peers.                                  |
| 19 | `MSG_SUB_DELTA`            | request   | Incremental subscribe / unsubscribe delta.                                    |

IDs 7, 8, 9 are reserved and must not be reused; they held legacy `MSG_NOTIFY`, `MSG_TOPIC_REQUEST`, and `MSG_TOPIC_REQUEST_STREAM`, retired in PR3 Stage D when `notify` / `request_topic` were removed. ID 20 is also reserved (formerly `MSG_AUTH`, removed in PR4 Stage K-3 when SPKI-pinned mTLS replaced the shared-secret auth).

The sentinel `REMOTE_NO_RESULT` (`networking.py:91`) distinguishes "handler returned `None`" (a valid result) from "no remote handler responded" (treated as no-match for fall-through).

---

## Network-side exceptions

| Exception                 | Base                | When                                                                                                                                                                |
|---------------------------|---------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `NetworkRequestException` | `RequestException`  | Network-level failure during a remote dispatch (timeout, connection failure, peer error response).                                                                  |
| `NoLocalSubException`     | `RequestException`  | Peer signaled "no local sub matched" on a remote `request_event` / `request_event_stream`. Distinct subclass so fall-through can preserve insertion order.          |
| `NodeException`           | `Exception`         | Generic node-level error (e.g. node unknown / disabled in helper paths).                                                                                            |
| `RequestException`        | `Exception`         | Generic plugin-call error. Bubbles up across the wire.                                                                                                              |

For most callers, `except RequestException:` covers all of the above (since `NetworkRequestException` and `NoLocalSubException` are subclasses). Catch `NoLocalSubException` separately only if you need to distinguish "peer-side miss" from "peer-side handler raised".

---

## Adding a new node to an existing cluster

Once at least one node already has a `cert.pem` and is in another node's `peers:` list, the normal mTLS exchange flow applies. To add a new node to that running cluster:

1. Decide the new node's hostname (`networking.hostname`, or empty for `socket.gethostname()`) and port (`networking.port`, default `2510`).
2. Obtain the new node's `cert.pem` and SPKI fingerprint via the networking CLI fingerprint helper.
3. On every existing node, add a `peers:` entry for the new node with hostname, address, and `cert_file:` (or inline `cert_pem:`). Optionally pin `fingerprint` for defense in depth.
4. On the new node, add `peers:` entries for every existing node (each with their cert and address).
5. Set `networking.enabled: true` on the new node and confirm the existing nodes still have it enabled.
6. Restart the new node (or hot-reload config). Existing nodes can pick up the new peer via config reload as well. Each side now pins the other; mTLS handshake succeeds; advert exchange runs; calls flow.

If a peer is offline at startup, that is fine — heartbeat / discovery loops bring it in when it comes back. What CANNOT be left empty is the `peers:` list when networking is enabled.

---

## Cluster bootstrap — pending framework fix

Standing up a brand-new cluster from zero certificates (no node has a `cert.pem` yet) is currently NOT supported by a clean operator workflow.

The reason is the order of operations inside `NetworkManager.start()` (`networking.py:949-957`): the empty-`peers:` check raises `RuntimeError` BEFORE `_load_or_generate_identity()` runs. A fresh node therefore cannot legally start with an empty trust store to generate its own cert, but it has no other way to obtain one — and the printable-fingerprint CLI helper requires a pre-existing `cert.pem` to read.

This is tracked as a pending framework improvement (internal bug ID **B-068**). Once the fix lands, the supported zero-cert bootstrap path is expected to be one of:

- A dedicated `init-identity` (or equivalent) CLI subcommand that generates `cert.pem` / `key.pem` and prints the fingerprint without booting NetworkManager, OR
- Reordering `start()` so identity is loaded/generated BEFORE the peers-non-empty check, allowing a one-shot first-boot to produce a cert and exit cleanly.

Until the fix lands, fresh-cluster bootstrap requires generating cert pairs out-of-band (for example, with a one-off OpenSSL or `cryptography` script that mirrors `_load_or_generate_identity`) before any node is started with `networking.enabled: true`. Document and pin that out-of-band step locally; do not rely on it for production rollout planning.

---

## Operational notes

- Peers connect on demand; the connection pool is sized by `pool_size` (default `5` per `(ip, port)`).
- Subscribe / unsubscribe events ALWAYS log at INFO regardless of the `verbose_notifier` flag — useful when debugging cross-node subscription state.
- An empty `peers:` list is a hard error at `start()`. Verify the list is populated before enabling networking.
- Legacy fields `secret`, `cert_file`, and `key_file` may still appear at the top of `networking:` in older configs; they are silently accepted but ignored after the PR4 K-3 cert-pinning rework. The `node_ips:` field is a hard error.
- A pin failure on inbound mTLS is logged at DEBUG, not WARNING — a port scanner sweeping the listener would otherwise flood the log and bury real security events.
