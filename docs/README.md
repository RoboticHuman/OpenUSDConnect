# Documentation

Documentation is grouped by task. Command details live in one place and are
linked from the shorter workflow guides.

## Start

- [Project overview](../README.md): architecture, integrations, dependencies,
  and a headless smoke test.
- [Blender getting started](getting-started.md): build the add-on and connect
  two Blender processes.
- [Server-provided USD files](live-open.md): live-open paths, mounts, save
  translation, metadata, and diagnostics.
- [Runnable examples](../examples/README.md): small, isolated protocol and API
  examples.

## Use

| Task | Guide |
| --- | --- |
| Build or select an OpenUSD runtime | [Building and selecting OpenUSD](building-openusd.md) |
| Find commands and common options | [Command-line reference](cli-reference.md) |
| Install and operate the Blender addon | [Blender addon usage](blender-addon-usage.md) |
| Connect Pixar's reference USD viewer | [usdview integration](../integrations/usdview/README.md) |
| Install and operate the Unreal Engine plugin | [Unreal Engine plugin](../integrations/unreal/OpenUSDConnect/README.md) |
| Author and inspect scenes through an MCP client | [MCP server usage](mcp-server-usage.md) |
| Work with synchronized materials and shaders | [Live material editing](live-material-editing.md) |
| Build installable release artifacts | [Packaging and distribution](distribution.md) |

## Understand

- [System overview](../README.md#how-it-works): server, event log, clients, and
  synchronization modes.
- [Server-provided USD files](live-open.md): generated snapshots, local
  mirrors, WebDAV access, and save translation.
- [Shared stage architecture](shared-stage-architecture.md): field-level
  synchronization of authored USD layer graphs.
- [Client recovery](client-recovery.md): rejected transactions, quarantined
  edits, and recovery APIs.

## Extend

- [USD-native Python API](usd-native-integration.md): lifecycle, stage
  ownership, managed clients, publishers, receivers, and resolver behavior.
- [Shared stage architecture](shared-stage-architecture.md): exact file-layer
  synchronization and its protocol.
- [MCP integration layout](../integrations/mcp/README.md#layout): extension
  points and module ownership.
- [Unreal plugin developer notes](../integrations/unreal/OpenUSDConnect/PLUGIN_DEV.md):
  threading, framing, module boundaries, and known gaps.

## Contribute

- [Testing](testing-setup.md): unit, integration, asset, Unreal, and visual test
  tiers.
- [Profiling](profiling.md): server and Blender sampling workflows.
- [Development commands](cli-reference.md#development-commands): packaging,
  test launchers, benchmarks, and diagnostics.

## Troubleshoot

- [Blender common problems](getting-started.md#common-problems)
- [Server-provided file verification and diagnostics](live-open.md#verify-and-diagnose)
- [Client reconnect and recovery states](client-recovery.md)
- [Unreal Engine troubleshooting](../integrations/unreal/OpenUSDConnect/README.md#troubleshooting)
- [usdview requirements and discovery](../integrations/usdview/README.md#requirements)
