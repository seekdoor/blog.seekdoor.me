#!/usr/bin/env node
/**
 * Import the private comment export created by scripts/migrate_typecho.py.
 *
 * The script is intentionally separate from the public Hugo build. It reads
 * credentials only from process environment or the command line and stores the
 * Typecho source identifiers in MongoDB so reruns are idempotent.
 */

import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve } from "node:path";

const require = createRequire(import.meta.url);
const { MongoClient, ObjectId } = require("mongodb");

function usage() {
  return [
    "Usage:",
    "  node scripts/import-typecho-comments.mjs --input <private-export.json> --mongo-uri <uri> [options]",
    "",
    "Options:",
    "  --database <name>       MongoDB database name (default: waline)",
    "  --collection <name>     Waline comments collection (default: Comment)",
    "  --dry-run               Validate and summarize without connecting to MongoDB",
  ].join("\n");
}

function parseArguments(argv) {
  const values = {
    database: "waline",
    collection: "Comment",
    dryRun: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--dry-run") {
      values.dryRun = true;
      continue;
    }
    if (argument === "--help" || argument === "-h") {
      values.help = true;
      continue;
    }
    if (!argument.startsWith("--")) {
      throw new Error("Unexpected argument: " + argument);
    }
    const key = argument.slice(2).replace(/-([a-z])/gu, (_, letter) => letter.toUpperCase());
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) {
      throw new Error("Missing value for " + argument);
    }
    values[key] = value;
    index += 1;
  }
  return values;
}

function sourceId(comment) {
  return Number(comment.typechoCoid);
}

function validateComments(comments) {
  if (!Array.isArray(comments)) {
    throw new Error("The comment export must be a JSON array.");
  }
  const bySourceId = new Map();
  for (const comment of comments) {
    const id = sourceId(comment);
    if (!Number.isInteger(id) || id <= 0) {
      throw new Error("Each comment must contain a positive typechoCoid.");
    }
    if (bySourceId.has(id)) {
      throw new Error("Duplicate Typecho comment id: " + id);
    }
    if (!comment.url || !comment.comment || !comment.created) {
      throw new Error("Comment " + id + " is missing url, comment, or created.");
    }
    bySourceId.set(id, comment);
  }

  const rootFor = (id, visited = new Set()) => {
    if (visited.has(id)) {
      throw new Error("Comment reply cycle detected at Typecho comment " + id + ".");
    }
    visited.add(id);
    const comment = bySourceId.get(id);
    const parentId = Number(comment.parent || 0);
    if (!parentId) {
      return id;
    }
    if (!bySourceId.has(parentId)) {
      throw new Error("Comment " + id + " references unavailable parent " + parentId + ".");
    }
    return rootFor(parentId, visited);
  };

  for (const comment of comments) {
    rootFor(sourceId(comment));
  }
  return { bySourceId, rootFor };
}

function buildDocument(comment, objectIds, rootFor) {
  const id = sourceId(comment);
  const parentSourceId = Number(comment.parent || 0);
  const rootSourceId = parentSourceId ? rootFor(id) : 0;
  const created = new Date(comment.created);
  if (Number.isNaN(created.getTime())) {
    throw new Error("Comment " + id + " has an invalid timestamp.");
  }

  const document = {
    source: "typecho",
    typechoCoid: id,
    typechoCid: Number(comment.typechoCid),
    url: String(comment.url),
    nick: String(comment.nick || "访客"),
    mail: String(comment.mail || ""),
    link: String(comment.link || ""),
    ip: String(comment.ip || ""),
    ua: String(comment.ua || ""),
    comment: String(comment.comment),
    status: "approved",
    insertedAt: created,
    like: 0,
  };
  if (parentSourceId) {
    document.pid = objectIds.get(parentSourceId).toHexString();
    document.rid = objectIds.get(rootSourceId).toHexString();
  }
  return document;
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  if (options.help) {
    console.log(usage());
    return;
  }
  if (!options.input) {
    throw new Error("--input is required.\n\n" + usage());
  }
  const inputPath = resolve(options.input);
  const comments = JSON.parse(await readFile(inputPath, "utf8"));
  const { bySourceId, rootFor } = validateComments(comments);
  const totalReplies = comments.filter((comment) => Number(comment.parent || 0) > 0).length;
  const summary = {
    comments: comments.length,
    topLevel: comments.length - totalReplies,
    replies: totalReplies,
    uniqueUrls: new Set(comments.map((comment) => comment.url)).size,
    sourceIds: bySourceId.size,
  };

  if (options.dryRun) {
    console.log(JSON.stringify({ dryRun: true, ...summary }, null, 2));
    return;
  }

  const mongoUri = options.mongoUri || process.env.MONGODB_URI;
  if (!mongoUri) {
    throw new Error("--mongo-uri or MONGODB_URI is required for a real import.");
  }

  const client = new MongoClient(mongoUri);
  await client.connect();
  try {
    const collection = client.db(options.database).collection(options.collection);
    const sourceIds = comments.map(sourceId);
    const existing = await collection
      .find(
        {
          source: "typecho",
          typechoCoid: { $in: sourceIds },
        },
        { projection: { _id: 1, typechoCoid: 1 } },
      )
      .toArray();
    const objectIds = new Map(existing.map((item) => [Number(item.typechoCoid), item._id]));
    for (const comment of comments) {
      const id = sourceId(comment);
      if (!objectIds.has(id)) {
        objectIds.set(id, new ObjectId());
      }
    }

    const operations = comments.map((comment) => {
      const id = sourceId(comment);
      return {
        updateOne: {
          filter: { source: "typecho", typechoCoid: id },
          update: {
            $set: buildDocument(comment, objectIds, rootFor),
            $setOnInsert: { _id: objectIds.get(id) },
          },
          upsert: true,
        },
      };
    });
    const result = await collection.bulkWrite(operations, { ordered: true });
    console.log(
      JSON.stringify(
        {
          dryRun: false,
          ...summary,
          matched: result.matchedCount,
          modified: result.modifiedCount,
          inserted: result.upsertedCount,
        },
        null,
        2,
      ),
    );
  } finally {
    await client.close();
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
