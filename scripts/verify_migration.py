#!/usr/bin/env python3
"""Verify that generated Hugo output matches the intended Typecho publication set."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("."))
    return parser.parse_args()


def public_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT cid, slug, type, status
            FROM blog_contents
            WHERE (type = 'post' AND status = 'publish')
               OR (type = 'page' AND status = 'publish')
               OR cid = 3
            ORDER BY cid
            """
        ).fetchall()
    )


def main() -> int:
    args = parse_arguments()
    root = args.output.resolve()
    public = root / "public"
    report = json.loads((root / "reports" / "typecho-migration.json").read_text(encoding="utf-8"))
    placeholders = json.loads(
        (root / "reports" / "missing-media-placeholders.json").read_text(encoding="utf-8")
    )
    comments = json.loads(
        (root / "reports" / "waline-comments.private.json").read_text(encoding="utf-8")
    )

    connection = sqlite3.connect(args.database.resolve())
    connection.row_factory = sqlite3.Row
    rows = public_rows(connection)
    errors: list[str] = []

    for row in rows:
        cid = int(row["cid"])
        if row["type"] == "post":
            target = public / "archives" / str(cid) / "index.html"
        else:
            target = public / f"{row['slug']}.html"
        if not target.is_file():
            errors.append(f"Missing generated route: {target.relative_to(root)}")

    for kind, expected in (("category", 16), ("tag", 94)):
        source_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM blog_metas WHERE type = ?",
                (kind,),
            ).fetchone()[0]
        )
        directories = [item for item in (public / kind).iterdir() if item.is_dir()]
        if source_count != expected:
            errors.append(f"Unexpected {kind} source count: {source_count}")
        if len(directories) != expected:
            errors.append(f"Unexpected generated {kind} count: {len(directories)}")

    for route in ("blog/index.html", "archives.html", "3.html", "index.xml", "sitemap.xml"):
        if not (public / route).is_file():
            errors.append(f"Missing site output: public/{route}")

    for asset_path in report["assets"]["unresolved_references"]:
        if not (public / asset_path.lstrip("/")).is_file():
            errors.append(f"Missing fallback asset: public{asset_path}")

    for asset_path in placeholders["generated"]:
        if not (public / asset_path.lstrip("/")).is_file():
            errors.append(f"Missing generated placeholder: public{asset_path}")

    if len(comments) != 56:
        errors.append(f"Expected 56 exported comments, got {len(comments)}")
    replies = sum(1 for item in comments if item["parent"])
    if replies != 13:
        errors.append(f"Expected 13 exported replies, got {replies}")

    vercel = json.loads((root / "vercel.json").read_text(encoding="utf-8"))
    attachment_redirects = [
        entry
        for entry in vercel["redirects"]
        if entry["source"].startswith("/attachment/")
    ]
    if len(attachment_redirects) != 348:
        errors.append(f"Expected 348 attachment redirect entries, got {len(attachment_redirects)}")

    summary = {
        "content_routes_checked": len(rows),
        "posts": sum(1 for row in rows if row["type"] == "post"),
        "pages": sum(1 for row in rows if row["type"] == "page"),
        "fallback_assets_checked": len(placeholders["generated"]),
        "comments_checked": len(comments),
        "replies_checked": replies,
        "errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
