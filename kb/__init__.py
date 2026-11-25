"""kb -- local-first YouTube knowledge store.

Curate a must-watch board, follow channels (full upload catalogue with
durations / views / popularity), pull transcripts, and keep everything in
one SQLite file with full-text search over timestamped transcript chunks.

    python -m kb serve            # UI at http://127.0.0.1:8765
    python -m kb --help           # CLI

See kb/README.md.
"""

__version__ = "0.1.0"
