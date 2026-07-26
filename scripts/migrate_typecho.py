#!/usr/bin/env python3
"""Build the public Hugo source tree from a Typecho SQLite backup.

The script deliberately treats the Typecho database and file backup as read-only.
It only clears directories that it owns under the Hugo project before regenerating
them, making repeated migrations deterministic without touching handwritten files.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote


SITE_HOST_PATTERN = re.compile(
    r"https?://blog\.seekdoor\.me(?=/|$)/?",
    flags=re.IGNORECASE,
)
MARKDOWN_MARKER_PATTERN = re.compile(r"<!--\s*markdown\s*-->", flags=re.IGNORECASE)
ASSET_PATTERN = re.compile(
    r"(?:https?://blog\.seekdoor\.me)?(?P<path>/usr/uploads/[^\s\"'<>\\)]+)",
    flags=re.IGNORECASE,
)
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))

MANAGED_DIRECTORIES = (
    Path("content/posts"),
    Path("content/pages"),
    Path("content/category"),
    Path("content/tag"),
    Path("content/blog"),
    Path("content/legacy-archives"),
    Path("static/usr/uploads"),
)
MANAGED_FILES = (
    Path("data/typecho-taxonomies.json"),
    Path("reports/typecho-migration.json"),
    Path("reports/waline-comments.private.json"),
    Path("vercel.json"),
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="Path to the Typecho SQLite backup.",
    )
    parser.add_argument(
        "--files",
        type=Path,
        required=True,
        help="Path to the backed-up Typecho install containing usr/uploads.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("."),
        help="Hugo repository root to generate into.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Leave previously generated managed files in place before generation.",
    )
    return parser.parse_args()


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return json.dumps(as_text(value), ensure_ascii=False)


def front_matter(values: dict[str, Any]) -> str:
    lines = ["+++"]
    for key, value in values.items():
        lines.append(f"{key} = {toml_value(value)}")
    lines.extend(["+++", ""])
    return "\n".join(lines)


def write_markdown(path: Path, values: dict[str, Any], body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_body = body.strip()
    content = front_matter(values)
    if normalized_body:
        content += normalized_body + "\n"
    path.write_text(content, encoding="utf-8", newline="\n")


def datetime_string(timestamp: int | None) -> str:
    value = int(timestamp or 0)
    return datetime.fromtimestamp(value, tz=CHINA_STANDARD_TIME).isoformat()


def content_datetime(timestamp: int | None) -> datetime:
    value = int(timestamp or 0)
    return datetime.fromtimestamp(value, tz=CHINA_STANDARD_TIME)


def safe_segment(value: str, fallback: str) -> str:
    segment = value.strip() or fallback
    if segment in {".", ".."} or "/" in segment or "\\" in segment:
        raise ValueError(f"Unsafe Typecho slug: {value!r}")
    return segment


def normalized_asset_path(value: str) -> str | None:
    raw = unquote(as_text(value)).replace("\\", "/")
    raw = raw.split("?", 1)[0].split("#", 1)[0].rstrip(".,;:!?")
    if not raw.startswith("/usr/uploads/"):
        return None
    parts = [part for part in raw.lstrip("/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    return "/" + "/".join(parts)


def extract_asset_paths(text: str) -> set[str]:
    paths: set[str] = set()
    for match in ASSET_PATTERN.finditer(text):
        path = normalized_asset_path(match.group("path"))
        if path:
            paths.add(path)
    return paths


def sanitize_body(text: str) -> tuple[str, int]:
    without_marker = MARKDOWN_MARKER_PATTERN.sub("", text)
    body, replacements = SITE_HOST_PATTERN.subn("/", without_marker)
    return body, replacements


def clean_generated_output(output_root: Path) -> None:
    for relative_path in MANAGED_DIRECTORIES:
        path = output_root / relative_path
        if path.exists():
            shutil.rmtree(path)
    for relative_path in MANAGED_FILES:
        path = output_root / relative_path
        if path.exists():
            path.unlink()


def read_attachment_path(value: str) -> str | None:
    try:
        attachment = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(attachment, dict):
        return None
    return normalized_asset_path(as_text(attachment.get("path")))


def query_rows(connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return list(connection.execute(query, params).fetchall())


def create_taxonomy_pages(
    output_root: Path,
    taxonomy_rows: Iterable[sqlite3.Row],
) -> dict[str, list[dict[str, Any]]]:
    taxonomies: dict[str, list[dict[str, Any]]] = {"category": [], "tag": []}
    for row in taxonomy_rows:
        kind = as_text(row["type"])
        slug = safe_segment(as_text(row["slug"]), f"{kind}-{row['mid']}")
        item = {
            "id": int(row["mid"]),
            "name": as_text(row["name"]),
            "slug": slug,
            "parent": int(row["parent"] or 0),
            "count": int(row["count"] or 0),
        }
        taxonomies[kind].append(item)
        write_markdown(
            output_root / "content" / kind / slug / "_index.md",
            {
                "title": item["name"],
                "url": f"/{kind}/{slug}/",
                "type": "taxonomy",
                "legacy_slug": slug,
                "legacy_parent": item["parent"],
            },
        )
    return taxonomies


def create_legacy_archive_pages(output_root: Path, posts: Iterable[sqlite3.Row]) -> int:
    dates = sorted({content_datetime(row["created"]).date() for row in posts})
    years = sorted({date.year for date in dates})
    months = sorted({(date.year, date.month) for date in dates})
    page_count = 0

    for year in years:
        write_markdown(
            output_root / "content" / "legacy-archives" / f"year-{year}.md",
            {
                "title": f"{year} 年",
                "url": f"/{year}/",
                "layout": "legacy-date-archive",
                "legacy_year": year,
            },
        )
        page_count += 1

    for year, month in months:
        write_markdown(
            output_root / "content" / "legacy-archives" / f"month-{year}-{month:02d}.md",
            {
                "title": f"{year} 年 {month:02d} 月",
                "url": f"/{year}/{month:02d}/",
                "layout": "legacy-date-archive",
                "legacy_year": year,
                "legacy_month": month,
            },
        )
        page_count += 1

    for date in dates:
        write_markdown(
            output_root
            / "content"
            / "legacy-archives"
            / f"day-{date.year}-{date.month:02d}-{date.day:02d}.md",
            {
                "title": date.isoformat(),
                "url": f"/{date.year}/{date.month:02d}/{date.day:02d}/",
                "layout": "legacy-date-archive",
                "legacy_year": date.year,
                "legacy_month": date.month,
                "legacy_day": date.day,
            },
        )
        page_count += 1

    return page_count


def write_vercel_config(
    output_root: Path,
    attachment_redirects: list[tuple[int, str]],
) -> None:
    redirects: list[dict[str, Any]] = [
        {
            "source": "/feed",
            "destination": "/index.xml",
            "permanent": True,
        },
        {
            "source": "/feed/",
            "destination": "/index.xml",
            "permanent": True,
        },
        {
            "source": "/feed/rss",
            "destination": "/index.xml",
            "permanent": True,
        },
        {
            "source": "/feed/atom",
            "destination": "/index.xml",
            "permanent": True,
        },
        {
            "source": "/search/:keywords*",
            "destination": "/search/?q=:keywords*",
            "permanent": True,
        },
        {
            "source": "/category/:slug/:page",
            "destination": "/category/:slug/page/:page",
            "permanent": True,
        },
        {
            "source": "/tag/:slug/:page",
            "destination": "/tag/:slug/page/:page",
            "permanent": True,
        },
    ]
    for cid, path in attachment_redirects:
        redirects.append(
            {
                "source": f"/attachment/{cid}",
                "destination": path,
                "permanent": True,
            }
        )
        redirects.append(
            {
                "source": f"/attachment/{cid}/",
                "destination": path,
                "permanent": True,
            }
        )

    config = {
        "version": 2,
        "framework": "hugo",
        "buildCommand": "hugo --gc --minify",
        "outputDirectory": "public",
        "env": {
            "HUGO_VERSION": "0.161.1",
            "HUGO_ENV": "production",
        },
        "redirects": redirects,
    }
    target = output_root / "vercel.json"
    target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_arguments()
    database_path = args.database.resolve()
    files_root = args.files.resolve()
    output_root = args.output.resolve()

    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite backup not found: {database_path}")
    if not (files_root / "usr" / "uploads").is_dir():
        raise FileNotFoundError(f"Typecho uploads folder not found under: {files_root}")

    if not args.no_clean:
        clean_generated_output(output_root)

    output_root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    public_rows = query_rows(
        connection,
        """
        SELECT *
        FROM blog_contents
        WHERE (type = 'post' AND status = 'publish')
           OR (type = 'page' AND status = 'publish')
           OR cid = 3
        ORDER BY created, cid
        """,
    )
    public_posts = [row for row in public_rows if row["type"] == "post"]
    public_pages = [row for row in public_rows if row["type"] == "page"]

    taxonomy_rows = query_rows(
        connection,
        """
        SELECT mid, name, slug, type, parent, count
        FROM blog_metas
        WHERE type IN ('category', 'tag')
        ORDER BY type, mid
        """,
    )
    taxonomies = create_taxonomy_pages(output_root, taxonomy_rows)
    (output_root / "data").mkdir(parents=True, exist_ok=True)
    (output_root / "data" / "typecho-taxonomies.json").write_text(
        json.dumps(taxonomies, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    relationships = defaultdict(lambda: {"category": [], "tag": []})
    for row in query_rows(
        connection,
        """
        SELECT r.cid, m.type, m.slug, m.mid
        FROM blog_relationships AS r
        JOIN blog_metas AS m ON m.mid = r.mid
        WHERE m.type IN ('category', 'tag')
        ORDER BY r.cid, m.type, m.mid
        """,
    ):
        relationships[int(row["cid"])][as_text(row["type"])].append(
            safe_segment(as_text(row["slug"]), f"{row['type']}-{row['mid']}")
        )

    catalog_enabled = {
        int(row["cid"]): as_text(row["str_value"]).strip() == "1"
        for row in query_rows(
            connection,
            "SELECT cid, str_value FROM blog_fields WHERE name = 'catalog'",
        )
    }

    canonical_urls: dict[int, str] = {}
    referenced_assets: set[str] = set()
    absolute_rewrite_count = 0
    generated_posts = 0
    generated_pages = 0

    for row in public_rows:
        cid = int(row["cid"])
        is_post = row["type"] == "post"
        slug = as_text(row["slug"])
        canonical_url = f"/archives/{cid}/" if is_post else f"/{slug}.html"
        canonical_urls[cid] = canonical_url

        body, replacements = sanitize_body(as_text(row["text"]))
        absolute_rewrite_count += replacements
        referenced_assets.update(extract_asset_paths(body))
        terms = relationships[cid]
        layout = ""
        if as_text(row["template"]) == "page-archives.php":
            layout = "legacy-archive"
        elif as_text(row["template"]) == "page-whisper.php":
            layout = "whisper"

        values: dict[str, Any] = {
            "title": as_text(row["title"]),
            "date": datetime_string(row["created"]),
            "lastmod": datetime_string(row["modified"] or row["created"]),
            "url": canonical_url,
            "typecho_cid": cid,
            "typecho_slug": slug,
            "typecho_status": as_text(row["status"]),
            "category": terms["category"],
            "categories": terms["category"],
            "tag": terms["tag"],
            "tags": terms["tag"],
            "comments": as_text(row["allowComment"]) == "1",
            "toc": catalog_enabled.get(cid, False),
            "draft": False,
        }
        if layout:
            values["layout"] = layout

        if is_post:
            target = output_root / "content" / "posts" / f"{cid}.md"
            generated_posts += 1
        else:
            target = output_root / "content" / "pages" / f"{cid}.md"
            generated_pages += 1
        write_markdown(target, values, body)

    write_markdown(
        output_root / "content" / "blog" / "_index.md",
        {
            "title": "文章",
            "url": "/blog/",
            "layout": "legacy-blog-list",
        },
    )
    legacy_archive_count = create_legacy_archive_pages(output_root, public_posts)

    attachment_rows = query_rows(
        connection,
        """
        SELECT attachment.cid, attachment.text
        FROM blog_contents AS attachment
        JOIN blog_contents AS parent ON parent.cid = attachment.parent
        WHERE attachment.type = 'attachment'
          AND parent.type = 'post'
          AND parent.status = 'publish'
        ORDER BY attachment.cid
        """,
    )
    copied_assets: set[str] = set()
    missing_attachment_files: list[str] = []
    attachment_redirects: list[tuple[int, str]] = []
    for row in attachment_rows:
        asset_path = read_attachment_path(as_text(row["text"]))
        if not asset_path:
            missing_attachment_files.append(f"attachment:{row['cid']}:invalid-metadata")
            continue
        source = files_root / asset_path.lstrip("/")
        target = output_root / "static" / asset_path.lstrip("/")
        if not source.is_file():
            missing_attachment_files.append(asset_path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied_assets.add(asset_path)
        attachment_redirects.append((int(row["cid"]), asset_path))

    unresolved_asset_references = sorted(referenced_assets - copied_assets)
    write_vercel_config(output_root, attachment_redirects)

    comment_rows = query_rows(
        connection,
        """
        SELECT coid, cid, created, author, mail, url, ip, agent, text, parent, status
        FROM blog_comments
        WHERE status = 'approved'
        ORDER BY coid
        """,
    )
    comment_export: list[dict[str, Any]] = []
    excluded_comment_ids: list[int] = []
    for row in comment_rows:
        cid = int(row["cid"])
        if cid not in canonical_urls:
            excluded_comment_ids.append(int(row["coid"]))
            continue
        comment_export.append(
            {
                "typechoCoid": int(row["coid"]),
                "typechoCid": cid,
                "url": canonical_urls[cid],
                "created": datetime_string(row["created"]),
                "nick": as_text(row["author"]),
                "mail": as_text(row["mail"]),
                "link": as_text(row["url"]),
                "ip": as_text(row["ip"]),
                "ua": as_text(row["agent"]),
                "comment": as_text(row["text"]),
                "parent": int(row["parent"] or 0),
                "status": as_text(row["status"]),
            }
        )

    reports_directory = output_root / "reports"
    reports_directory.mkdir(parents=True, exist_ok=True)
    private_comment_path = reports_directory / "waline-comments.private.json"
    private_comment_path.write_text(
        json.dumps(comment_export, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    excluded_counts = [
        {
            "type": as_text(row["type"]),
            "status": as_text(row["status"]),
            "count": int(row["count"]),
        }
        for row in query_rows(
            connection,
            """
            SELECT type, status, COUNT(*) AS count
            FROM blog_contents
            WHERE NOT (
                (type = 'post' AND status = 'publish')
                OR (type = 'page' AND status = 'publish')
                OR cid = 3
            )
            GROUP BY type, status
            ORDER BY type, status
            """,
        )
    ]
    report = {
        "source_database": database_path.name,
        "source_files": files_root.name,
        "generated_at": datetime.now(tz=CHINA_STANDARD_TIME).isoformat(),
        "content": {
            "posts": generated_posts,
            "pages": generated_pages,
            "reopened_whisper_page": 3 in canonical_urls,
            "legacy_date_archives": legacy_archive_count,
            "urls": len(canonical_urls),
            "excluded": excluded_counts,
        },
        "taxonomy": {
            "categories": len(taxonomies["category"]),
            "tags": len(taxonomies["tag"]),
        },
        "assets": {
            "eligible_attachments": len(attachment_rows),
            "copied": len(copied_assets),
            "referenced_by_public_content": len(referenced_assets),
            "missing_attachment_files": missing_attachment_files,
            "unresolved_references": unresolved_asset_references,
        },
        "comments": {
            "exported": len(comment_export),
            "replies": sum(1 for comment in comment_export if comment["parent"]),
            "excluded_comment_ids": excluded_comment_ids,
        },
        "links": {
            "absolute_internal_rewrites": absolute_rewrite_count,
            "attachment_redirects": len(attachment_redirects),
        },
    }
    (reports_directory / "typecho-migration.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    connection.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if missing_attachment_files or unresolved_asset_references or excluded_comment_ids:
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Migration failed: {error}", file=sys.stderr)
        raise
