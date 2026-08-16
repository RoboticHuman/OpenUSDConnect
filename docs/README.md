# Documentation

Choose the guide that matches the task. Setup and command details stay in the
linked pages so each topic has one maintained reference.

## Start

For a first run or a quick orientation:

- [Project overview](../README.md) introduces the server, integrations,
  dependency groups, and core concepts.
- [Blender getting started](getting-started.md) covers the base-file connection
  and the optional server-provided USD file.
- [Server-provided USD files](live-open.md) covers live-open paths, mounts, write
  fallback, metadata, authentication, and diagnostics.
- [Runnable examples](../examples/README.md) offers smaller demonstrations organized
  by launch style and feature.

## Use

For artists, technical directors, operators, and tool users:

| Task | Guide |
| --- | --- |
| Find supported commands and options | [Command-line reference](cli-reference.md) |
| Install and operate the Blender addon | [Blender addon usage](blender-addon-usage.md) |
| Connect Pixar's reference USD viewer | [usdview integration](../integrations/usdview/README.md) |
| Install and operate the Unreal Engine plugin | [Unreal Engine plugin](../integrations/unreal/OpenUSDConnect/README.md) |
| Author and inspect scenes through an MCP client | [MCP server usage](mcp-server-usage.md) |
| Work with synchronized materials and shaders | [Live material editing](live-material-editing.md) |

## Understand

For the design and behavior behind the user workflows:

- [System overview](../README.md#how-it-works) describes the server, event log,
  synchronization modes, and clients.
- [Server-provided USD files](live-open.md) explains generated snapshots, local
  mirrors, WebDAV access, and write fallback.
- [Shared stage architecture](shared-stage-architecture.md) explains direct
  synchronization of authored USD layer graphs.
- [Client recovery](client-recovery.md) covers reconnect, replay, compaction,
  and recovery states.

## Extend

For developers adding a host, client, protocol-facing tool, or synchronization mode:

- [USD-native integration contract](usd-native-integration.md) covers lifecycle,
  stage ownership, managed clients, publishers, receivers, and resolver behavior.
- [Shared stage architecture](shared-stage-architecture.md) explains exact file-layer
  synchronization and how it differs from managed collaboration layers.
- [MCP integration layout](../integrations/mcp/README.md#layout) identifies the MCP
  extension points and module responsibilities.
- [Unreal plugin developer notes](../integrations/unreal/OpenUSDConnect/PLUGIN_DEV.md)
  documents threading, framing, module boundaries, and current gaps.

## Contribute

For contributors validating or profiling changes:

- [Testing setup](testing-setup.md) describes the unit, integration, asset, Unreal,
  and visual test tiers, including their external requirements.
- [Profiling](profiling.md) covers server and Blender sampling workflows and the main
  performance paths to inspect.
- [Development commands](cli-reference.md#development-commands) lists the maintained
  schema generation, addon packaging, and diagnostic entry points.
