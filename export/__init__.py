"""Exporters: turn an enriched :class:`core.models.ExportBundle` into a file.

Each exporter is a thin, side-effect-light function that takes a bundle plus an
output path and returns the path it wrote.  ``html_exporter`` renders a
self-contained, WeChat-style HTML page (inline CSS/JS, media copied alongside).
"""
