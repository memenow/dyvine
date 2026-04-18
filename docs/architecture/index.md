# Architecture Diagrams Overview

_Last Updated: 2026-04-17_

## Summary

This directory contains five key architecture diagrams visualizing different aspects of the Dyvine API system:

1. **[Flowchart](./flowchart.md)**: End-to-end request processing flow
2. **[Sequence Diagram](./sequence.md)**: Component interaction sequence for typical requests
3. **[Control Flow Graph](./control-flow.md)**: Decision points and error paths
4. **[Dependency Graph](./dependency-graph.md)**: Module and package dependencies
5. **[Call Graph](./call-graph.md)**: Function and method call relationships

These diagrams describe the current production architecture and must be updated when the implementation changes.

## Key Findings

- Layered architecture with explicit operation tracking for asynchronous downloads
- Async/await request handling with persistent operation state
- Dedicated liveness, readiness, and startup probes
- Prometheus metrics exposed through an ASGI application
- Structured error handling and request-scoped correlation tracking

For detailed implementation, refer to project memories in `.serena/memories/`.
