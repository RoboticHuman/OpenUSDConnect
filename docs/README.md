# Documentation

Choose the path that matches what you are trying to do. Setup and command details
stay in the linked guides so there is one authoritative place to maintain them.

## Start

For a first run or a quick orientation:

- [Project overview and getting started](../README.md#getting-started) introduces the
  server, integrations, dependency groups, and core concepts.
- [Blender getting started](getting-started.md) walks through the standard server,
  manual connection, and optional seamless live-open workflow.
- [Live-open and VFS](live-open.md) covers paths, mounts, write fallback, metadata,
  authentication, and diagnostics in depth.
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
