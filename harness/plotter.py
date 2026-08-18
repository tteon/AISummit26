"""Automated SVG Chart Generator for Benchmark Results (Zero External Dependency)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List


def generate_sql_vs_cypher_plot(data: List[Dict[str, Any]], out_file: Path):
    """Generates a modern dual-bar SVG chart comparing SQL Joins vs Cypher Hops."""
    if not data:
        return

    width = 800
    height = 420
    margin_left = 130
    margin_right = 40
    margin_top = 60
    margin_bottom = 60

    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    n = len(data)
    bar_group_h = plot_h / max(n, 1)
    bar_h = min(14, bar_group_h * 0.35)

    max_val = max(max(d.get("sql_joins", 0), d.get("cypher_hops", 0)) for d in data)
    max_val = max(max_val, 8)

    svg = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="background:#0d1117; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;">',
        f'  <style>',
        f'    .title {{ fill: #f0f6fc; font-size: 18px; font-weight: 600; }}',
        f'    .subtitle {{ fill: #8b949e; font-size: 12px; }}',
        f'    .label {{ fill: #c9d1d9; font-size: 11px; font-family: monospace; }}',
        f'    .val {{ fill: #f0f6fc; font-size: 10px; font-weight: bold; }}',
        f'    .grid {{ stroke: #30363d; stroke-width: 1; stroke-dasharray: 2,2; }}',
        f'    .legend {{ font-size: 12px; font-weight: 500; }}',
        f'  </style>',
        f'  <!-- Header -->',
        f'  <text x="{margin_left}" y="30" class="title">Structural Complexity: SQL Joins vs Cypher Hops</text>',
        f'  <text x="{margin_left}" y="48" class="subtitle">Relational vs Property Graph Model Expressivity across Easy / Medium / Hard AML queries</text>',
        f'  <!-- Legend -->',
        f'  <rect x="{width - 240}" y="20" width="12" height="12" rx="2" fill="#ef4444" />',
        f'  <text x="{width - 222}" y="30" fill="#ef4444" class="legend">SQL Joins</text>',
        f'  <rect x="{width - 130}" y="20" width="12" height="12" rx="2" fill="#3b82f6" />',
        f'  <text x="{width - 112}" y="30" fill="#3b82f6" class="legend">Cypher Hops</text>',
    ]

    # Grid lines & X-ticks
    for tick in range(0, max_val + 1, 2):
        x = margin_left + (tick / max_val) * plot_w
        svg.append(f'  <line x1="{x}" y1="{margin_top}" x2="{x}" y2="{height - margin_bottom}" class="grid" />')
        svg.append(f'  <text x="{x}" y="{height - margin_bottom + 18}" fill="#8b949e" font-size="11" text-anchor="middle">{tick}</text>')

    # Bars
    for i, d in enumerate(data):
        qid = d.get("question_id", f"Q{i+1}")
        sql_j = d.get("sql_joins", 0)
        cyp_h = d.get("cypher_hops", 0)

        y_center = margin_top + i * bar_group_h + bar_group_h / 2
        y1 = y_center - bar_h - 1
        y2 = y_center + 1

        w1 = (sql_j / max_val) * plot_w
        w2 = (cyp_h / max_val) * plot_w

        # Y Label
        svg.append(f'  <text x="{margin_left - 12}" y="{y_center + 4}" class="label" text-anchor="end">{qid}</text>')

        # SQL Bar
        svg.append(f'  <rect x="{margin_left}" y="{y1}" width="{max(w1, 2)}" height="{bar_h}" rx="2" fill="#ef4444" opacity="0.9" />')
        if sql_j > 0:
            svg.append(f'  <text x="{margin_left + w1 + 5}" y="{y1 + bar_h - 3}" class="val">{sql_j}</text>')

        # Cypher Bar
        svg.append(f'  <rect x="{margin_left}" y="{y2}" width="{max(w2, 2)}" height="{bar_h}" rx="2" fill="#3b82f6" opacity="0.9" />')
        if cyp_h > 0:
            svg.append(f'  <text x="{margin_left + w2 + 5}" y="{y2 + bar_h - 3}" class="val">{cyp_h}</text>')

    svg.append('</svg>')
    out_file.write_text("\n".join(svg))


def generate_latency_plot(suite_results: Dict[str, Any], out_file: Path):
    """Generates an SVG chart summarizing latencies across executed suites."""
    width = 700
    height = 300
    svg = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="background:#0d1117; font-family:-apple-system,sans-serif;">',
        f'  <text x="30" y="35" fill="#f0f6fc" font-size="16" font-weight="600">Benchmark Execution Latency by Suite</text>',
        f'  <text x="30" y="55" fill="#8b949e" font-size="12">Total suite runtime duration (seconds)</text>',
    ]

    suites = list(suite_results.items())
    if not suites:
        svg.append('</svg>')
        out_file.write_text("\n".join(svg))
        return

    max_dur = max(s[1].get("duration_seconds", 0.1) for s in suites)
    max_dur = max(max_dur, 1.0)
    bar_w = 400

    for i, (name, sdata) in enumerate(suites):
        dur = sdata.get("duration_seconds", 0.0)
        y = 90 + i * 55
        w = (dur / max_dur) * bar_w
        svg.append(f'  <text x="30" y="{y+16}" fill="#c9d1d9" font-size="13" font-weight="500">{name}</text>')
        svg.append(f'  <rect x="200" y="{y}" width="{max(w, 4)}" height="22" rx="4" fill="#10b981" />')
        svg.append(f'  <text x="{200 + w + 10}" y="{y+16}" fill="#f0f6fc" font-size="12" font-weight="bold">{dur:.2f}s</text>')

    svg.append('</svg>')
    out_file.write_text("\n".join(svg))
