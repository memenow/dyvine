# Architecture Diagrams Overview

_Last Updated: 2025-10-07_

## Summary

This directory contains five key architecture diagrams visualizing different aspects of the Dyvine API system:

1. **[Flowchart](./flowchart.md)**: End-to-end request processing flow
2. **[Sequence Diagram](./sequence.md)**: Component interaction sequence for typical requests
3. **[Control Flow Graph](./control-flow.md)**: Decision points and error paths
4. **[Dependency Graph](./dependency-graph.md)**: Module and package dependencies
5. **[Call Graph](./call-graph.md)**: Function and method call relationships

These diagrams are automatically generated and updated after merges/commits to maintain traceability and single source of truth.

## Key Findings

- Layered architecture with clear separation of concerns (Presentation, Business Logic, Data Access)
- Async/await pattern throughout for high concurrency
- External dependencies on Douyin API and Cloudflare R2 storage
- Comprehensive error handling and correlation tracking
- Kubernetes-ready deployment with multi-environment support

For detailed implementation, refer to project memories in `.serena/memories/`.
