# pviz-parser

Generate LLM-optimized dependency analysis bundles from your codebase.

`pviz-parser` analyzes your project's dependency graph and produces a structured JSON bundle designed to fit inside an LLM context window. Instead of pasting files one by one, you give your LLM a complete picture of how your codebase is wired — nodes, edges, import relationships, cycle detection, and per-file metrics — in a single compressed artifact.

## Installation

```bash
pip install pviz-parser
```

## Quickstart

```bash
pviz . -o bundle.json --clean
```

That's it. Point `pviz` at your project root, specify an output path, and get a bundle ready to drop into Claude, ChatGPT, or any LLM that accepts file uploads or large context.

## Usage

```
pviz <scan_root> -o <output.json> [options]

Arguments:
  scan_root             Root directory of the project to analyze

Options:
  -o, --output          Output path for the bundle JSON (required)
  --clean               Nuke the artifact cache before running (recommended when switching repos)
  --store-root PATH     Override the sandbox directory (default: per-user .pviz_store)
  --mode classic|zones  Build mode (default: zones)
  --max-bytes N         Per-file size limit in bytes (default: 100MB)
  --allow-output-in-repo  Allow writing the bundle inside scan_root
```

### Examples

```bash
# Analyze current directory
pviz . -o bundle.json --clean

# Analyze a specific project
pviz ~/projects/myapp -o ~/Desktop/myapp_bundle.json --clean

# Use a custom store root to keep artifacts organized
pviz . -o bundle.json --store-root /tmp/pviz --clean
```

## What's in the bundle

The output is a structured JSON artifact with:

- **Nodes** — one per source file, with LOC, SLOC, import/exporter counts, SCC membership, symbols, and language
- **Edges** — directed import relationships between files
- **Dependency metrics** — which files are most imported, which import the most, hotspots
- **Cycle detection** — strongly connected components (SCCs) flagged at the node level
- **Discovery manifest** — full file inventory with language breakdown
- **Folder index** — per-file import surface and resolution data
- **Summary** — counts, parse status, edge stats, crosstalk candidates

A compressed format is also generated alongside the standard bundle (`.compressed.json`), typically 55–65% smaller, optimized for tight context windows.

## Language support

| Language | CLI (this package) | SaaS ([pvizgenerator.com](https://pvizgenerator.com)) |
|---|---|---|
| Python | YES | YES |
| TypeScript | YES | YES |
| JavaScript | YES | YES |
| Java | YES (partial — pure Python parser) | YES (full resolution) |
| Kotlin | NO | YES |
| Go | NO | YES |
| Rust | NO | YES |

Kotlin, Go, and Rust analysis requires compiled binary dependencies that are part of the hosted SaaS only. Polyglot repos with multiple supported languages are handled automatically — the bundle merges all detected languages into a single artifact.

## CLI vs SaaS

`pviz-parser` is the open source CLI. It runs locally, produces bundles you own, and supports Python, TypeScript, JavaScript, and partial resolution for Java out of the box.

[pvizgenerator.com](https://pvizgenerator.com) is the hosted SaaS layer. It adds full Kotlin, Go, and Rust support, hosted bundle storage, bundle diffing across commits, and MCP delivery for direct LLM tool integration — without running anything locally.

## Requirements

- Python 3.10+
- No other system dependencies for Python/TS/JS/partial-Java analysis

## License

MIT — see [LICENSE](LICENSE)

Built by [Michael McClellan](https://pvizgenerator.com)